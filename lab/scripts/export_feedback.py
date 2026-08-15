#!/usr/bin/env python3
"""
Выгрузка оценок из Open WebUI: пары chosen/rejected для DPO плюс ЧЕСТНАЯ проверка,
можно ли вообще считать это датасетом.

Почему проверка важнее выгрузки. Ревью нашего сетапа показало: оценка привязана к слагу
OWUI, а не к чекпойнту; пары получаются только через regenerate или арену; размечают
обычно два человека на своих же промптах. Поэтому скрипт сначала печатает диагностику
(сколько пар, сколько разметчиков, разнообразие промптов, есть ли провенанс), и только
потом отдаёт JSONL. Если пар десятки и разметчик один, DPO на этом строить нельзя,
но как очередь на разбор багов поток полезен.

Usage:
  ./export_feedback.py --stats                       # только диагностика
  ./export_feedback.py --out dpo.jsonl               # пары + диагностика
  ./export_feedback.py --out dpo.jsonl --min-pairs 200
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"
# футер, который ставит фильтр run_metadata: единственный носитель провенанса,
# потому что из outlet персистится только content (meta/usage не доезжают)
PROV = re.compile(r"<sub>(slug=[^<]+)</sub>")


def admin_token(container=CONTAINER) -> str:
    return subprocess.check_output([
        "docker", "exec", "-i", container, "python3", "-c",
        "import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
        "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
        "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),algorithm='HS256'))"
    ], text=True).strip()


def fetch(tok, host=HOST):
    out = subprocess.run(["curl", "-s", "--max-time", "60", "-H", f"Authorization: Bearer {tok}",
                          f"{host}/api/v1/evaluations/feedbacks/all/export"],
                         capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except Exception:
        sys.exit(f"не разобрал ответ: {out[:200]}")


def parse_provenance(text: str) -> dict:
    m = PROV.search(text or "")
    if not m:
        return {}
    out = {}
    for part in m.group(1).split("·"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k.strip()] = v.strip().strip("`")
    return out


def text_of(msg: dict) -> str:
    if not msg:
        return ""
    content = msg.get("content") or ""
    if content:
        return PROV.sub("", content).strip()
    # сообщение может нести структурированный output вместо content
    parts = []
    for item in msg.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                parts.append(c.get("text") or "")
        elif item.get("type") == "function_call":
            parts.append(f"[call {item.get('name')} {item.get('arguments')}]")
    return "\n".join(p for p in parts if p).strip()


def prompt_of(hist: dict, msg: dict) -> str:
    """Ближайшее пользовательское сообщение вверх по дереву."""
    cur = hist.get(msg.get("parentId") or "")
    while cur:
        if cur.get("role") == "user":
            return text_of(cur)
        cur = hist.get(cur.get("parentId") or "")
    return ""


def _history(f: dict) -> dict:
    """snapshot.chat это ЦЕЛАЯ строка ChatModel (id/user_id/title/chat/...), а реальный чат лежит
    в snapshot.chat.chat.history.messages. Раньше был предположен путь snapshot.chat.history —
    он пустой. Берём вложенный chat, с фолбэком на старую форму."""
    sc = (f.get("snapshot") or {}).get("chat") or {}
    inner = sc.get("chat") if isinstance(sc.get("chat"), dict) else sc
    return (inner.get("history") or {}).get("messages") or {}


def build(feedbacks):
    by_msg, pairs = {}, []
    for f in feedbacks:
        if f.get("type") != "rating":
            continue
        meta = f.get("meta") or {}
        by_msg[(meta.get("chat_id"), meta.get("message_id"))] = f

    seen = set()
    for f in feedbacks:
        if f.get("type") != "rating":
            continue
        d, meta = f.get("data") or {}, f.get("meta") or {}
        hist = _history(f)
        msg = hist.get(meta.get("message_id") or "")
        if not msg:
            continue
        parent = hist.get(msg.get("parentId") or "")
        if not parent:
            continue
        for sib_id in parent.get("childrenIds") or []:
            if sib_id == msg["id"]:
                continue
            other = by_msg.get((meta.get("chat_id"), sib_id))
            if not other:
                continue
            r1, r2 = d.get("rating"), (other.get("data") or {}).get("rating")
            if r1 is None or r2 is None or r1 == r2:
                continue
            key = tuple(sorted([msg["id"], sib_id]))
            if key in seen:
                continue
            seen.add(key)
            win, lose = (msg, hist.get(sib_id)) if r1 > r2 else (hist.get(sib_id), msg)
            pairs.append({
                "prompt": prompt_of(hist, msg),
                "chosen": text_of(win),
                "rejected": text_of(lose),
                "reason": d.get("reason"), "comment": d.get("comment"),
                "chosen_provenance": parse_provenance((win or {}).get("content", "")),
                "rejected_provenance": parse_provenance((lose or {}).get("content", "")),
                "chat_id": meta.get("chat_id"),
            })
    return pairs


def stats(feedbacks, pairs):
    ratings = [f for f in feedbacks if f.get("type") == "rating"]
    raters = Counter(f.get("user_id") for f in ratings)
    with_sibs = sum(1 for f in ratings if ((f.get("data") or {}).get("sibling_model_ids")))
    prov = sum(1 for p in pairs if p["chosen_provenance"].get("slug"))
    prompts = {p["prompt"][:120] for p in pairs}
    models = Counter((f.get("data") or {}).get("model_id") for f in ratings)

    print("=" * 60)
    print(f"оценок всего:            {len(ratings)}")
    print(f"с соседями (потолок пар): {with_sibs}")
    print(f"собрано пар:             {len(pairs)}")
    print(f"уникальных промптов:     {len(prompts)}")
    print(f"разметчиков:             {len(raters)} {dict(raters.most_common(3))}")
    print(f"пар с провенансом:       {prov}/{len(pairs)}")
    print(f"модели:                  {dict(models.most_common(5))}")
    print("=" * 60)
    verdict = []
    if len(pairs) < 200:
        verdict.append(f"пар мало ({len(pairs)} < 200): для DPO рано, использовать как очередь разбора")
    if len(raters) < 3:
        verdict.append(f"разметчиков {len(raters)}: оценки смещены под одного человека, "
                       "нужно согласие разметчиков на общем golden-наборе")
    if prov < len(pairs):
        verdict.append("не у всех пар есть провенанс: включи глобальный фильтр run_metadata, "
                       "иначе после переливки слага пары молча меняют смысл")
    if len(prompts) < max(10, len(pairs) // 4):
        verdict.append("мало разнообразия промптов: разметка сконцентрирована на узком срезе")
    for v in verdict or ["выборка выглядит пригодной, но всё равно померь согласие разметчиков"]:
        print("  ⚠ " + v if verdict else "  " + v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="куда писать JSONL с парами")
    ap.add_argument("--stats", action="store_true", help="только диагностика")
    ap.add_argument("--min-pairs", type=int, default=0, help="не писать файл, если пар меньше")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--host", default=HOST)
    a = ap.parse_args()

    fb = fetch(admin_token(a.container), a.host)
    pairs = build(fb)
    stats(fb, pairs)
    if a.stats or not a.out:
        return
    if len(pairs) < a.min_pairs:
        sys.exit(f"пар {len(pairs)} < min-pairs {a.min_pairs}, файл не пишу")
    with open(a.out, "w", encoding="utf-8") as fh:
        for p in pairs:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"записано {len(pairs)} пар -> {a.out}")


if __name__ == "__main__":
    main()
