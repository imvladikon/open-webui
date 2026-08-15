#!/usr/bin/env python3
"""
Реестр workspace-моделей: описываешь чекпойнты в JSON, скрипт создаёт их в Open WebUI
с зашитыми параметрами, тулзами, фильтрами и грантами.

Зачем: в UI параметры (seed, temperature) задаются ОДИН раз на запрос, а не на колонку
side-by-side (`Chat.svelte:3126`). Значит сравнивать два чекпойнта честно можно только
если параметры зашиты в саму модель. Плюс реестр = единый источник правды для команды:
у всех одинаковые пресеты, никто не сравнивает на разных температурах.

Usage:
  ./model_registry.py registry.json --apply          # создать/обновить всё
  ./model_registry.py registry.json --dry-run        # показать, что будет сделано
  ./model_registry.py registry.json --defaults       # + выставить глобальные params инстанса
  ./model_registry.py --list                         # что сейчас заведено

Формат registry.json — см. registry.example.json рядом.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"


def admin_token(container=CONTAINER) -> str:
    return subprocess.check_output([
        "docker", "exec", "-i", container, "python3", "-c",
        "import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
        "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
        "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),algorithm='HS256'))"
    ], text=True).strip()


def api(method: str, path: str, tok: str, body=None, host=HOST):
    args = ["curl", "-s", "-w", "\n%{http_code}", "-X", method,
            "-H", f"Authorization: Bearer {tok}", "-H", "Content-Type: application/json",
            f"{host}{path}"]
    tmp = None
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


def build_model(entry: dict, defaults: dict) -> dict:
    params = dict(defaults.get("params") or {})
    params.update(entry.get("params") or {})
    meta = {
        "description": entry.get("description", ""),
        "toolIds": entry.get("tools", defaults.get("tools", [])),
        "filterIds": entry.get("filters", defaults.get("filters", [])),
        "tags": [{"name": t} for t in entry.get("tags", [])],
    }
    body = {
        "id": entry["id"],
        "base_model_id": entry["slug"],
        "name": entry.get("name", entry["id"]),
        "params": params,
        "meta": meta,
    }
    if entry.get("grants"):
        body["access_grants"] = entry["grants"]
    return body


def upsert(entry: dict, defaults: dict, tok: str, dry: bool):
    body = build_model(entry, defaults)
    label = f"{body['id']:22} -> {body['base_model_id']}"
    if dry:
        print(f"[dry] {label}  params={body['params']} tools={body['meta']['toolIds']}")
        return True
    # модель может уже существовать: сперва пробуем создать, при конфликте пишем meta/params в БД
    code, _ = api("POST", "/api/v1/models/create", tok, body)
    if code == "200":
        print(f"[new] {label}")
        return True
    ok = patch_in_db(body)
    print(f"[upd] {label}" if ok else f"[ERR] {label} (create HTTP={code}, db-patch failed)")
    return ok


def patch_in_db(body: dict, container=CONTAINER) -> bool:
    """
    Обновление существующей модели: /models/model/update в 0.11 отдаёт 500 на частичном
    теле, поэтому правим meta/params прямо в таблице model (это чистые JSON-поля без
    вычисляемых значений, в отличие от tool.specs).
    """
    script = (
        "import sqlite3,json,sys\n"
        "b=json.load(sys.stdin)\n"
        "c=sqlite3.connect('/app/backend/data/webui.db')\n"
        "r=c.execute('select meta from model where id=?',(b['id'],)).fetchone()\n"
        "if not r: print('NOMODEL'); raise SystemExit(1)\n"
        "meta=json.loads(r[0]) if r[0] else {}\n"
        "meta.update(b['meta'])\n"
        "c.execute('update model set meta=?, params=?, base_model_id=?, name=? where id=?',"
        "(json.dumps(meta,ensure_ascii=False),json.dumps(b['params'],ensure_ascii=False),"
        "b['base_model_id'],b['name'],b['id']))\n"
        "c.commit(); print('OK')\n"
    )
    p = subprocess.run(["docker", "exec", "-i", container, "python3", "-c", script],
                       input=json.dumps(body), capture_output=True, text=True)
    return "OK" in p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("registry", nargs="?", help="путь к registry.json")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--defaults", action="store_true",
                    help="выставить models.default_params на весь инстанс")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--host", default=HOST)
    a = ap.parse_args()

    tok = admin_token(a.container)

    if a.list:
        # читаем таблицу model напрямую: /api/models не отдаёт params/meta для workspace-моделей
        script = (
            "import sqlite3,json\n"
            "c=sqlite3.connect('/app/backend/data/webui.db')\n"
            "for i,b,n,p,m in c.execute('select id,base_model_id,name,params,meta from model'):\n"
            "  p=json.loads(p) if p else {}; m=json.loads(m) if m else {}\n"
            "  print(json.dumps({'id':i,'base':b,'name':n,'temp':p.get('temperature'),"
            "'seed':p.get('seed'),'fc':p.get('function_calling'),'tools':m.get('toolIds') or [],"
            "'tags':[t.get('name') for t in (m.get('tags') or [])]},ensure_ascii=False))\n"
        )
        out = subprocess.run(["docker", "exec", "-i", a.container, "python3", "-c", script],
                             capture_output=True, text=True).stdout
        for line in out.strip().splitlines():
            r = json.loads(line)
            print(f"{r['id']:16} base={str(r['base']):20} temp={str(r['temp']):5} "
                  f"seed={str(r['seed']):6} tools={r['tools']} {'/'.join(r['tags'])}")
        return

    if not a.registry:
        sys.exit("нужен registry.json (или --list)")
    reg = json.loads(Path(a.registry).expanduser().read_text(encoding="utf-8"))
    defaults = reg.get("defaults", {})

    if a.defaults and not a.dry_run:
        code, _ = api("POST", "/api/v1/configs/models", tok,
                      {"DEFAULT_MODEL_PARAMS": defaults.get("params", {})}, host=a.host)
        print(f"[cfg] models.default_params -> HTTP={code}: {defaults.get('params')}")

    ok = 0
    for entry in reg.get("models", []):
        ok += bool(upsert(entry, defaults, tok, a.dry_run or not a.apply))
    if a.apply and not a.dry_run:
        api("GET", "/api/models?refresh=true", tok, host=a.host)  # без refresh -> Model not found
        print(f"[ok] обработано {ok}, кэш моделей обновлён")


if __name__ == "__main__":
    main()
