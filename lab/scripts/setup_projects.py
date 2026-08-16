#!/usr/bin/env python3
"""
«Проекты» как в ChatGPT — на штатных папках Open WebUI.

Механика (по коду 0.11): у папки есть `data.system_prompt` и `data.files`, и они применяются ко
ВСЕМ чатам внутри неё (`utils/middleware.py`, ветка сборки контекста). То есть папка = проект с
собственным системным промптом. Нового кода не нужно, нужно завести папки и прописать промпты.

Usage:
  ./setup_projects.py --list
  ./setup_projects.py --apply                 # создать наш набор проектов
  ./setup_projects.py --apply --file my.json  # свой набор
"""
import argparse
import json
import subprocess
import sys
import tempfile

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"

# Набор под наши задачи. Системный промпт задаёт роль на весь проект, чтобы не повторять её
# в каждом сообщении.
DEFAULT_PROJECTS = [
    {
        "name": "Booking RL",
        "system_prompt": (
            "Ты помогаешь в работе над booking-агентом (RL post-training, Qwen). "
            "Контекст: reward composite + LLM-судья, оси task/truth/product/eff, "
            "чекпойнты вида agentsml215-v7-300. Отвечай конкретно и коротко, числа приводи с "
            "источником. Не рассуждай вслух."),
    },
    {
        "name": "Бенчмарки",
        "system_prompt": (
            "Ты помогаешь с бенчмарками Тир-0 (tau2, bfcl, agentvista, swe, mcp_mark, toolathlon, "
            "concierge) через кубик model_bench_eval и eliza-слаги. Помни: слаг должен быть жив, "
            "серв держать весь прогон, парсер тулзов qwen3_coder. Отвечай по делу."),
    },
    {
        "name": "Инфра и сервы",
        "system_prompt": (
            "Ты помогаешь с инфраструктурой: YT, Nirvana, GPU-пулы, instance_launcher, eliza. "
            "Ключевое: наши сервы преемптимы (вес задаёт долю, не неприкосновенность), "
            "27B/35B поднимаются 30-40 минут. Предлагай проверяемые шаги, не догадки."),
    },
    {
        "name": "Черновики и тексты",
        "system_prompt": (
            "Ты помогаешь писать документы и письма по-русски. Живой инженерный язык, без "
            "канцелярита, без длинных тире, без пустых вводных фраз. Сразу по делу."),
    },
]


def admin_token(container):
    return subprocess.check_output([
        "docker", "exec", "-i", container, "python3", "-c",
        "import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
        "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
        "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),algorithm='HS256'))"
    ], text=True).strip()


def api(method, path, tok, body=None, host=HOST):
    args = ["curl", "-s", "-w", "\n%{http_code}", "-X", method,
            "-H", f"Authorization: Bearer {tok}", "-H", "Content-Type: application/json",
            f"{host}{path}"]
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(body, tmp, ensure_ascii=False)
        tmp.close()
        args += ["--data", "@" + tmp.name]
    out = subprocess.run(args, capture_output=True, text=True).stdout
    code = out.rsplit("\n", 1)[-1].strip()
    payload = out.rsplit("\n", 1)[0]
    try:
        payload = json.loads(payload)
    except Exception:
        pass
    return code, payload


def list_folders(container):
    out = subprocess.check_output([
        "docker", "exec", "-i", container, "python3", "-c",
        "import sqlite3,json;c=sqlite3.connect('/app/backend/data/webui.db');"
        "print(json.dumps([{'id':i,'name':n,'data':(json.loads(d) if d else None) or {}} "
        "for i,n,d in c.execute('select id,name,data from folder')], ensure_ascii=False))"
    ], text=True)
    return json.loads(out)


def set_prompt(container, folder_id, prompt):
    """
    Пишем system_prompt в data папки. Через API есть /folders/{id}/update, но он ждёт полную
    форму; для идемпотентного скрипта проще дописать JSON-поле в БД (это чистые данные,
    без вычисляемых полей).
    """
    script = (
        "import sqlite3,json,sys\n"
        "p=json.load(sys.stdin)\n"
        "c=sqlite3.connect('/app/backend/data/webui.db')\n"
        "r=c.execute('select data from folder where id=?',(p['id'],)).fetchone()\n"
        "d=(json.loads(r[0]) if r and r[0] else None) or {}\n"
        "d['system_prompt']=p['prompt']\n"
        # PK у folder составной (id,user_id), но id уникален на практике; обновляем по нему
        "n=c.execute('update folder set data=?, updated_at=strftime(\\'%s\\',\\'now\\') "
        "where id=?',(json.dumps(d,ensure_ascii=False),p['id'])).rowcount\n"
        "c.commit(); print('ok' if n else 'norows')\n"
    )
    p = subprocess.run(["docker", "exec", "-i", container, "python3", "-c", script],
                       input=json.dumps({"id": folder_id, "prompt": prompt}),
                       capture_output=True, text=True)
    return "ok" in p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--file", help="JSON со списком [{name, system_prompt}]")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--host", default=HOST)
    a = ap.parse_args()

    if a.list:
        for f in list_folders(a.container):
            sp = ((f.get("data") or {}).get("system_prompt") or "")[:60]
            print(f"  {f['name']:22} {'[промпт] ' + sp if sp else '(без промпта)'}")
        return

    projects = DEFAULT_PROJECTS
    if a.file:
        projects = json.loads(open(a.file, encoding="utf-8").read())
    if not a.apply:
        for p in projects:
            print(f"  [dry] {p['name']:22} {p['system_prompt'][:60]}...")
        print(f"\n{len(projects)} проектов. Запусти с --apply.")
        return

    tok = admin_token(a.container)
    existing = {f["name"]: f["id"] for f in list_folders(a.container)}
    for p in projects:
        fid = existing.get(p["name"])
        if not fid:
            code, resp = api("POST", "/api/v1/folders/", tok, {"name": p["name"]}, host=a.host)
            fid = resp.get("id") if isinstance(resp, dict) else None
            print(f"[new] {p['name']:22} HTTP={code}")
        else:
            print(f"[upd] {p['name']:22} (уже есть)")
        if fid and p.get("system_prompt"):
            ok = set_prompt(a.container, fid, p["system_prompt"])
            print(f"      системный промпт: {'записан' if ok else 'НЕ записан'}")
    print("\nГотово. В сайдбаре появятся папки-проекты: чаты внутри наследуют промпт проекта.")


if __name__ == "__main__":
    main()
