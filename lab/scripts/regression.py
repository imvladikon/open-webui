#!/usr/bin/env python3
"""
Регресс-набор промптов: прогнать фиксированный список по чекпойнту и сравнить с прошлым прогоном.

Зачем: базовая нужда пострейна — «стало лучше или сломалось». В OWUI такого нет (Automations это
cron, не сравнение), поэтому гоняем прямо по eliza-слагу и храним прогоны в JSON рядом.

Usage:
  ./regression.py --suite suite.example.json --slug qwen38-27b-gate --run
  ./regression.py --list                       # прошлые прогоны
  ./regression.py --compare run-A.json run-B.json
"""
import argparse
import asyncio
import difflib
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

ELIZA = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1"
RUNS_DIR = Path(__file__).resolve().parent.parent / ".runs"
THINK = re.compile(r"<think>.*?</think>", re.S)
WORD = re.compile(r"\w+", re.U)


def token() -> str:
    p = Path.home() / ".eliza" / "token"
    return (os.environ.get("OPENAI_API_KEYS", "").split(";")[0]
            or (p.read_text().strip() if p.exists() else ""))


def clean(t: str) -> str:
    t = THINK.sub("", t or "")
    if "</think>" in t:
        t = t.split("</think>")[-1]
    return " ".join(t.split()).strip()


def similarity(a: str, b: str) -> float:
    """Насколько ответы похожи: 1.0 = идентичны. Быстрый ratio по словам."""
    aw, bw = WORD.findall(a.lower()), WORD.findall(b.lower())
    if not aw and not bw:
        return 1.0
    return difflib.SequenceMatcher(None, aw, bw).ratio()


async def ask(cx, slug, case, temperature, max_tokens, sys_prompt):
    msgs = ([{"role": "system", "content": sys_prompt}] if sys_prompt else []) + \
           [{"role": "user", "content": case["prompt"]}]
    t0 = time.time()
    try:
        r = await cx.post(ELIZA.format(slug=slug) + "/chat/completions",
                          json={"model": slug, "messages": msgs, "temperature": temperature,
                                "max_tokens": max_tokens, "stream": False},
                          headers={"Authorization": f"Bearer {token()}",
                                   "Content-Type": "application/json"})
        r.raise_for_status()
        d = r.json()
        ch = d["choices"][0]
        text = clean(ch["message"].get("content") or "")
        checks = {}
        for kind, pat in (case.get("expect") or {}).items():
            if kind == "contains":
                checks["contains"] = all(p.lower() in text.lower() for p in
                                         (pat if isinstance(pat, list) else [pat]))
            elif kind == "regex":
                checks["regex"] = bool(re.search(pat, text, re.I | re.S))
            elif kind == "max_chars":
                checks["max_chars"] = len(text) <= int(pat)
        return {"id": case["id"], "prompt": case["prompt"], "text": text,
                "ms": int((time.time() - t0) * 1000),
                "finish": ch.get("finish_reason"),
                "tokens": (d.get("usage") or {}).get("completion_tokens"),
                "checks": checks, "passed": all(checks.values()) if checks else None}
    except Exception as e:
        return {"id": case["id"], "prompt": case["prompt"], "text": "",
                "error": f"{type(e).__name__}: {e}", "passed": False, "checks": {}}


async def run_suite(suite, slug, concurrency=2):
    import httpx
    sem = asyncio.Semaphore(concurrency)     # не долбим преемптимый серв

    async with httpx.AsyncClient(timeout=300, verify=False) as cx:
        async def one(case):
            async with sem:
                print(f"  · {case['id']}", flush=True)
                return await ask(cx, slug, case, suite.get("temperature", 0.0),
                                 suite.get("max_tokens", 400), suite.get("system_prompt", ""))
        results = await asyncio.gather(*[one(c) for c in suite["cases"]])
    return results


def save_run(suite_name, slug, results):
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{suite_name}-{slug}-{stamp}.json"
    path.write_text(json.dumps({"suite": suite_name, "slug": slug, "ts": stamp,
                                "results": results}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def summarize(results):
    graded = [r for r in results if r.get("passed") is not None]
    ok = sum(1 for r in graded if r["passed"])
    lat = [r["ms"] for r in results if r.get("ms")]
    trunc = sum(1 for r in results if r.get("finish") == "length")
    errs = sum(1 for r in results if r.get("error"))
    return {"cases": len(results), "graded": len(graded), "passed": ok,
            "median_ms": int(statistics.median(lat)) if lat else None,
            "truncated": trunc, "errors": errs}


def compare(a_path, b_path):
    a = json.loads(Path(a_path).read_text(encoding="utf-8"))
    b = json.loads(Path(b_path).read_text(encoding="utf-8"))
    ai = {r["id"]: r for r in a["results"]}
    bi = {r["id"]: r for r in b["results"]}
    print(f"A: {a['slug']} {a['ts']}   B: {b['slug']} {b['ts']}\n")
    print(f"{'case':22} {'A':>6} {'B':>6} {'схожесть':>9}  изменение")
    regressions = []
    for cid in sorted(set(ai) | set(bi)):
        ra, rb = ai.get(cid), bi.get(cid)
        if not ra or not rb:
            print(f"{cid:22} {'—' if not ra else 'есть':>6} {'—' if not rb else 'есть':>6}")
            continue
        pa = "ok" if ra.get("passed") else ("fail" if ra.get("passed") is False else "-")
        pb = "ok" if rb.get("passed") else ("fail" if rb.get("passed") is False else "-")
        sim = similarity(ra.get("text", ""), rb.get("text", ""))
        mark = ""
        if pa == "ok" and pb == "fail":
            mark, _ = "РЕГРЕСС", regressions.append(cid)
        elif pa == "fail" and pb == "ok":
            mark = "починилось"
        elif sim < 0.5:
            mark = "ответ сильно изменился"
        print(f"{cid:22} {pa:>6} {pb:>6} {sim:>9.2f}  {mark}")
    sa, sb = summarize(a["results"]), summarize(b["results"])
    print(f"\nA: {sa}\nB: {sb}")
    if regressions:
        print(f"\nРЕГРЕССЫ: {', '.join(regressions)}")
    return regressions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite")
    ap.add_argument("--slug", default="qwen38-27b-gate")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--compare", nargs=2)
    ap.add_argument("--concurrency", type=int, default=2)
    a = ap.parse_args()

    if a.list:
        RUNS_DIR.mkdir(exist_ok=True)
        for p in sorted(RUNS_DIR.glob("*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            print(f"  {p.name:52} {summarize(d['results'])}")
        return
    if a.compare:
        sys.exit(1 if compare(*a.compare) else 0)
    if not a.suite:
        sys.exit("нужен --suite (или --list / --compare)")

    suite = json.loads(Path(a.suite).read_text(encoding="utf-8"))
    print(f"прогон {suite['name']} по {a.slug}: {len(suite['cases'])} кейсов")
    results = asyncio.run(run_suite(suite, a.slug, a.concurrency))
    path = save_run(suite["name"], a.slug, results)
    s = summarize(results)
    print(f"\n{s}\nсохранено: {path}")
    for r in results:
        if r.get("passed") is False:
            print(f"  FAIL {r['id']}: {r.get('error') or (r['text'][:80] + '...')}")


if __name__ == "__main__":
    main()
