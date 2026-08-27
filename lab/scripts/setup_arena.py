#!/usr/bin/env python3
"""
Настройка слепой арены для сбора DPO-пар.

Зачем: экспортёр feedback показал, что одиночные лайки почти не дают пар. Пара образуется, когда
под одним промптом есть два ответа и оба размечены с противоположным рейтингом. Арена делает это
штатно: псевдо-модель на каждый запрос СЛУЧАЙНО выбирает одну из реальных под-моделей (слепо, без
anchoring), а feedback автоматически получает `sibling_model_ids` и реальный `selected_model_id`.
Плюс лидерборд Elo.

Механика (по коду): арена-модель = {id, name, meta:{model_ids:[...], filter_mode}}; выбор под-модели
random.choice в middleware.py:2261; конфиг через POST /api/v1/evaluations/config
(ключи evaluation.arena.enable / evaluation.arena.models).

Usage:
  ./setup_arena.py --models ab-base,ab-rl-v7 --name "Checkpoint Arena" --id ckpt-arena
  ./setup_arena.py --list      # показать текущие арены
"""
import argparse
import json
import subprocess
import sys
import tempfile

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"


def admin_token(container=CONTAINER) -> str:
    return subprocess.check_output([
        "docker", "exec", "-i", container, "python3", "-c",
        "import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
        "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
        "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),algorithm='HS256'))"
    ], text=True).strip()


def api(method, path, tok, body=None, host=HOST):
    args = ["curl", "-s", "-w", "\n%{http_code}", "-X", method,
            "-H", f"Authorization: Bearer {tok}", "-H", "Content-Type: application/json", f"{host}{path}"]
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
    ap.add_argument("--models", help="под-модели через запятую, напр. ab-base,ab-rl-v7")
    ap.add_argument("--id", default="ckpt-arena")
    ap.add_argument("--name", default="Checkpoint Arena")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--host", default=HOST)
    a = ap.parse_args()

    tok = admin_token(a.container)
    code, cfg = api("GET", "/api/v1/evaluations/config", tok, host=a.host)
    if not isinstance(cfg, dict):
        sys.exit(f"не прочитал конфиг арены: HTTP={code} {cfg}")
    arenas = cfg.get("EVALUATION_ARENA_MODELS") or []

    if a.list:
        print(f"ENABLE_EVALUATION_ARENA_MODELS = {cfg.get('ENABLE_EVALUATION_ARENA_MODELS')}")
        for m in arenas:
            print(f"  {m['id']:16} {m.get('name','')!r} -> {m.get('meta',{}).get('model_ids')}")
        return

    if not a.models:
        sys.exit("нужен --models (или --list)")
    ids = [s.strip() for s in a.models.split(",") if s.strip()]

    arena = {
        "id": a.id, "name": a.name,
        "meta": {
            "profile_image_url": "/favicon.png",
            "description": "Слепое сравнение чекпойнтов: голос копится как sibling-пары для DPO.",
            "model_ids": ids,
        },
    }
    # upsert по id
    arenas = [m for m in arenas if m.get("id") != a.id] + [arena]
    code, resp = api("POST", "/api/v1/evaluations/config", tok,
                     {"ENABLE_EVALUATION_ARENA_MODELS": True, "EVALUATION_ARENA_MODELS": arenas},
                     host=a.host)
    print(f"config update -> HTTP={code}")
    if code != "200":
        sys.exit(str(resp)[:300])
    api("GET", "/api/models?refresh=true", tok, host=a.host)
    print(f"[ok] арена '{a.id}' сравнивает {ids}. Выбирай её в чате, задавай вопрос, голосуй —")
    print("     каждый голос пишет sibling_model_ids, потом ./export_feedback.py соберёт пары.")


if __name__ == "__main__":
    main()
