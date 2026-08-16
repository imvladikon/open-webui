#!/usr/bin/env python3
"""
Включить ChatGPT-класс фичи, которые в Open WebUI УЖЕ есть, но выключены.

Контекст: сверка с ChatGPT (docs/chatgpt-parity.md) показала, что память, автодополнение,
компакция и прочее лежат в коде и просто не активированы. Это самые дешёвые улучшения
«продуктовости» чата: ноль нового кода.

Usage:
  ./enable_features.py --list          # что сейчас стоит
  ./enable_features.py --apply         # включить безопасный набор
  ./enable_features.py --apply --with-subagents   # + субагенты (осторожно: до x20 нагрузки)
"""
import argparse
import json
import subprocess
import sys
import tempfile

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"

# ключ конфига -> (значение, зачем)
SAFE = {
    "memory.enable": (True, "память между чатами (главная ChatGPT-фича, у нас была выключена)"),
    "task.autocomplete.enable": (True, "автодополнение ввода"),
    "context_compaction.enable": (True, "сжатие длинного диалога вместо обрыва"),
    "folders.enable": (True, "папки как «проекты» с общим системным промптом"),
    "notes.enable": (True, "заметки"),
    "code_interpreter.enable": (True, "исполнение кода (pyodide в браузере)"),
    "evaluation.arena.enable": (True, "слепая арена чекпойнтов для DPO-пар"),
}
RISKY = {
    # ×20 параллельных генераций на наши преемптимые сервы: включать осознанно
    "subagents.enable": (True, "субагенты (до 20 параллельных генераций — бьёт по сервам)"),
}


def admin_token(container):
    return subprocess.check_output([
        "docker", "exec", "-i", container, "python3", "-c",
        "import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
        "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
        "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),algorithm='HS256'))"
    ], text=True).strip()


def read_config(container):
    out = subprocess.check_output([
        "docker", "exec", "-i", container, "python3", "-c",
        "import sqlite3,json;c=sqlite3.connect('/app/backend/data/webui.db');"
        "print(json.dumps(dict(c.execute('select key,value from config').fetchall())))"
    ], text=True)
    return json.loads(out)


def set_keys(container, pairs):
    """
    Пишем в таблицу config. Через API это раскидано по десятку разных эндпоинтов
    (у каждой подсистемы свой), а ключи в БД единообразны. После записи нужен рестарт
    контейнера, чтобы приложение перечитало конфиг в память.
    """
    script = (
        "import sqlite3,json,sys\n"
        "p=json.load(sys.stdin)\n"
        "c=sqlite3.connect('/app/backend/data/webui.db')\n"
        "for k,v in p.items():\n"
        "    val=json.dumps(v)\n"
        "    cur=c.execute('select 1 from config where key=?',(k,)).fetchone()\n"
        "    if cur: c.execute('update config set value=? where key=?',(val,k))\n"
        "    else:   c.execute('insert into config (key,value) values (?,?)',(k,val))\n"
        "c.commit(); print('written', len(p))\n"
    )
    p = subprocess.run(["docker", "exec", "-i", container, "python3", "-c", script],
                       input=json.dumps(pairs), capture_output=True, text=True)
    return p.stdout.strip() or p.stderr[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--with-subagents", action="store_true")
    ap.add_argument("--container", default=CONTAINER)
    a = ap.parse_args()

    cfg = read_config(a.container)
    plan = dict(SAFE)
    if a.with_subagents:
        plan.update(RISKY)

    print(f"{'ключ':34} {'сейчас':10} {'станет':8} зачем")
    changes = {}
    for k, (want, why) in plan.items():
        cur = cfg.get(k, "(нет)")
        cur_val = json.loads(cur) if isinstance(cur, str) and cur not in ("(нет)",) else cur
        same = cur_val is want
        print(f"{k:34} {str(cur_val):10} {str(want):8} {'= уже' if same else '→ ' + why}")
        if not same:
            changes[k] = want

    if a.list or not a.apply:
        print(f"\nК изменению: {len(changes)}. Запусти с --apply, чтобы применить.")
        return
    if not changes:
        print("\nВсё уже включено.")
        return
    print("\n" + set_keys(a.container, changes))
    print("ВАЖНО: нужен рестарт контейнера, приложение читает config в память при старте:")
    print(f"  docker restart {a.container}")


if __name__ == "__main__":
    main()
