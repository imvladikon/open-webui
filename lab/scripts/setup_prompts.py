#!/usr/bin/env python3
"""
Библиотека промптов: наши повторяющиеся запросы как слэш-команды в чате.

Штатная фича OWUI (`routers/prompts.py`): у промпта есть `command` (вызов через `/имя`),
`content` с плейсхолдерами и версионирование с историей. Мы просто наполняем её нашими
рабочими сценариями, чтобы не печатать одно и то же и чтобы у команды были одинаковые
формулировки (сравнимые результаты).

Плейсхолдеры OWUI: `{{CLIPBOARD}}`, `{{CURRENT_DATE}}` и типизированные пользовательские
переменные вида `{{имя}}` — при вызове команды UI спросит значения.

Usage:
  ./setup_prompts.py --list
  ./setup_prompts.py --apply
"""
import argparse
import json
import subprocess
import tempfile

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"

PROMPTS = [
    {
        "command": "/rca",
        "name": "RCA траектории",
        "content": (
            "Разбери траекторию бронирования и определи ВЛАДЕЛЬЦА ошибки: DATA (кривой таргет "
            "или среда), MODEL (модель ошиблась), REWARD (награда мимо), REVIEW (спорно).\n\n"
            "Дай: первый неверный шаг, доказательство из трейса, вердикт владельца, "
            "и что проверить дальше. Без рассуждений вслух, сразу разбор.\n\n"
            "Траектория:\n{{trace}}"),
        "tags": ["booking", "rca"],
    },
    {
        "command": "/bench",
        "name": "Объяснить число бенча",
        "content": (
            "Объясни, что означает результат бенча и стоит ли ему верить.\n"
            "Проверь: размер выборки, с чем сравнивается, попадает ли разница в шум, "
            "нет ли протечки судьи или контаминации.\n"
            "Ответ: вердикт одной строкой, затем 3-5 пунктов обоснования.\n\n"
            "Результат:\n{{result}}"),
        "tags": ["benchmarks"],
    },
    {
        "command": "/serve",
        "name": "Поднять серв чекпойнта",
        "content": (
            "Составь команду instance_launcher для подъёма чекпойнта как eliza-слага.\n"
            "Учти наши правила: образ sglang:latest, --tool-call-parser qwen3_coder, "
            "БЕЗ --reasoning-parser без гейт-теста, --rundir //tmp/gpu_distributed_launcher, "
            "пин здоровых IB-сегментов, dense 27B = 1 GPU, MoE 35B = TP2.\n\n"
            "Чекпойнт: {{checkpoint}}\nСлаг: {{slug}}"),
        "tags": ["infra"],
    },
    {
        "command": "/diag",
        "name": "Диагностика по шагам",
        "content": (
            "Проведи рефлективную диагностику проблемы: сначала 5-7 гипотез из разных категорий "
            "(код, данные, конфиг, интеграция, инфра), затем отранжируй топ-3 по "
            "вероятность×дешевизна проверки, для каждой дай МИНИМАЛЬНЫЙ тест, который её "
            "подтвердит или опровергнет. Фикс не предлагай, пока причина не подтверждена.\n\n"
            "Симптом:\n{{symptom}}"),
        "tags": ["debug"],
    },
    {
        "command": "/short",
        "name": "Сжать до сути",
        "content": (
            "Перепиши текст короче в 3-4 раза, сохранив все факты и числа. Живой инженерный "
            "русский, без канцелярита, без длинных тире, без вводных фраз. Только суть.\n\n"
            "{{text}}"),
        "tags": ["текст"],
    },
    {
        "command": "/review",
        "name": "Ревью кода",
        "content": (
            "Проверь код и найди реальные дефекты: ошибки логики, краевые случаи, гонки, "
            "утечки, небезопасные места. Для каждой находки: файл и строка, конкретный сценарий "
            "поломки, severity. Стилистику не трогай. Если дефектов нет, так и скажи.\n\n"
            "```\n{{code}}\n```"),
        "tags": ["код"],
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--host", default=HOST)
    a = ap.parse_args()

    tok = admin_token(a.container)
    code, existing = api("GET", "/api/v1/prompts/", tok, host=a.host)
    have = {p.get("command") for p in existing} if isinstance(existing, list) else set()

    if a.list:
        for p in (existing if isinstance(existing, list) else []):
            print(f"  {p.get('command'):12} {p.get('name','')}")
        print(f"\nвсего: {len(have)}")
        return
    if not a.apply:
        for p in PROMPTS:
            mark = "есть" if p["command"] in have else "новый"
            print(f"  [{mark}] {p['command']:10} {p['name']}")
        print(f"\n{len(PROMPTS)} промптов. Запусти с --apply.")
        return

    for p in PROMPTS:
        body = {"command": p["command"], "name": p["name"], "content": p["content"],
                "tags": p.get("tags") or [], "meta": {}, "data": {}}
        if p["command"] in have:
            # id в путях OWUI без слэша
            pid = p["command"].lstrip("/")
            code, _ = api("POST", f"/api/v1/prompts/id/{pid}/update", tok, body, host=a.host)
            print(f"[upd] {p['command']:10} HTTP={code}")
        else:
            code, _ = api("POST", "/api/v1/prompts/create", tok, body, host=a.host)
            print(f"[new] {p['command']:10} HTTP={code}")
    print("\nГотово. В чате набери «/» — команды появятся в списке.")


if __name__ == "__main__":
    main()
