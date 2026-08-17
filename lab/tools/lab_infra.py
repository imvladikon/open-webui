"""
title: Lab Infra
author: agents-team
version: 0.1.0
description: Инфраструктура лаборатории прямо в диалоге: статус сервов и слагов, свободные GPU в пуле, сравнение двух чекпойнтов на одном промпте. Работает из любой модели, отдельный пункт в списке моделей для этого не нужен.
"""
# ЗАЧЕМ ЭТОТ ФАЙЛ. Раньше это были три ОТДЕЛЬНЫЕ модели-пайпа (Serve Status, GPU Status,
# A/B Compare). В селекторе они стояли вперемешку с настоящими моделями, и получалось
# меню из псевдо-моделей: чтобы спросить «живы ли сервы», надо было бросить диалог и
# переключиться на другую «модель», а потом переключиться обратно.
#
# Тулза — правильная форма для такого: она доступна ЛЮБОЙ модели, не рвёт диалог и
# комбинируется с остальным («проверь сервы и, если v7 жив, сравни его с базой»).
# Заодно у пайпов был неустранимый недостаток: пайп НЕ умеет вызывать инструменты,
# поэтому из «модели» Serve Status нельзя было ничего больше сделать.
#
# Все три метода — только чтение, ничего не запускают и не останавливают.
import asyncio
import difflib
import json
import os
import re
import subprocess

_ELIZA = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1"
SLUGS = {
    "qwen38-27b-gate": "база Qwen3.8-27B (dense, 1 GPU)",
    "qwen35-v7-gate": "RL v7 agentsml215-v7-300 (MoE, TP2)",
}
_WORD = re.compile(r"\w+", re.U)


def _token() -> str:
    return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]


def _clean(t: str) -> str:
    t = re.sub(r"<think>.*?</think>", "", t or "", flags=re.S)
    if "</think>" in t:
        t = t.split("</think>")[-1]
    return t.strip()


class Tools:
    def __init__(self):
        self.citation = False

    async def serve_status(self, __event_emitter__=None) -> str:
        """
        Проверить, живы ли наши модели-слаги в eliza (база и RL-чекпойнт). Вызывать, когда спрашивают про статус сервов, доступность модели, или когда в чате появилась ошибка «Model not found».
        """
        import httpx
        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": "опрашиваю слаги", "done": False}})

        async def probe(cx, slug):
            import time
            t0 = time.time()
            try:
                r = await cx.post(_ELIZA.format(slug=slug) + "/chat/completions",
                                  json={"model": slug, "max_tokens": 1,
                                        "messages": [{"role": "user", "content": "ok"}]},
                                  headers={"Authorization": f"Bearer {_token()}",
                                           "Content-Type": "application/json"})
                return slug, r.status_code, int((time.time() - t0) * 1000)
            except Exception as e:
                return slug, f"{type(e).__name__}", int((time.time() - t0) * 1000)

        async with httpx.AsyncClient(timeout=60, verify=False) as cx:
            rows = await asyncio.gather(*(probe(cx, s) for s in SLUGS))

        out, down = [], 0
        for slug, code, ms in rows:
            ok = code == 200
            down += int(not ok)
            mark = "🟢 работает" if ok else ("🔴 лежит" if code in (400, 404) else f"🟠 {code}")
            out.append(f"| `{slug}` | {mark} | {code} | {ms} ms | {SLUGS[slug]} |")
        head = "Все сервы подняты." if not down else f"Недоступно слагов: {down}."
        hint = ""
        if down:
            hint = ("\n\nСервы живут на преемптимом GPU: планировщик забирает их под чужие "
                    "джобы того же пула. Лончер поднимает серв обратно сам, но 27B/35B "
                    "грузятся 30-40 минут, и всё это время слаг честно отвечает 400. "
                    "Насовсем это чинит гарантийная квота либо больший вес в пуле.")
        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": head, "done": True}})
        return (f"{head}\n\n| слаг | статус | код | задержка | что это |\n|---|---|---|---|---|\n"
                + "\n".join(out) + hint)

    async def gpu_status(self, __event_emitter__=None) -> str:
        """
        Показать свободные GPU в нашем пуле YT и здоровые сегменты InfiniBand. Вызывать на вопросы про свободные карты, очередь за GPU, куда пинить сегменты, почему не стартует обучение.
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": "спрашиваю YT", "done": False}})
        try:
            # ya есть не в каждом контуре — отвечаем честно, а не выдумываем цифры
            p = subprocess.run(["ya", "tool", "yt", "--proxy", "watt", "get",
                                "//sys/scheduler/orchid/scheduler/pool_trees/gpu/pools/"
                                "alice-nlp-agents/resource_usage"],
                               capture_output=True, text=True, timeout=90)
            raw = (p.stdout or p.stderr or "").strip()
        except Exception as e:
            raw = f"{type(e).__name__}: {e}"
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "готово",
                                                                "done": True}})
        if not raw or "not found" in raw.lower() or "Traceback" in raw:
            return ("Из контейнера чата нет доступа к YT (нет `ya` или прав), поэтому цифры по "
                    "свободным GPU отсюда не достать. Смотри пульт бенчей или скилл gpu-watch "
                    "с рабочей машины. Не выдумывай числа в ответе — так и скажи пользователю.")
        return f"Занятость пула (сырой ответ YT):\n\n```json\n{raw[:1500]}\n```"

    async def compare_checkpoints(self, prompt: str, __event_emitter__=None) -> str:
        """
        Задать один и тот же вопрос базовой модели и RL-чекпойнту и сравнить ответы по времени, длине и схожести. Вызывать, когда просят сравнить чекпойнты, посмотреть разницу base против RL, проверить регресс.
        :param prompt: Вопрос, который будет задан обеим моделям одновременно.
        """
        import time
        import httpx

        async def ask(cx, slug):
            t0 = time.time()
            try:
                r = await cx.post(_ELIZA.format(slug=slug) + "/chat/completions",
                                  json={"model": slug, "temperature": 0, "max_tokens": 1200,
                                        "messages": [{"role": "user", "content": prompt}]},
                                  headers={"Authorization": f"Bearer {_token()}",
                                           "Content-Type": "application/json"})
                if r.status_code != 200:
                    return {"slug": slug, "err": f"HTTP {r.status_code}"}
                d = r.json()["choices"][0]
                return {"slug": slug, "text": _clean(d["message"].get("content") or ""),
                        "ms": int((time.time() - t0) * 1000),
                        "finish": d.get("finish_reason")}
            except Exception as e:
                return {"slug": slug, "err": type(e).__name__}

        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": "спрашиваю оба чекпойнта",
                                              "done": False}})
        async with httpx.AsyncClient(timeout=300, verify=False) as cx:
            a, b = await asyncio.gather(ask(cx, "qwen38-27b-gate"), ask(cx, "qwen35-v7-gate"))
        if a.get("err") and b.get("err"):
            return f"Оба серва недоступны (база: {a['err']}, v7: {b['err']})."

        aw, bw = _WORD.findall((a.get("text") or "").lower()), _WORD.findall((b.get("text") or "").lower())
        sim = difflib.SequenceMatcher(None, aw, bw).ratio() if (aw or bw) else 1.0
        def cell(r):
            if r.get("err"):
                return f"⚠ {r['err']}"
            flag = " · ⚠ обрезан" if r.get("finish") == "length" else ""
            return f"{r['ms']} ms · {len(r.get('text',''))} симв.{flag}"
        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": f"схожесть {sim:.2f}", "done": True}})
        return (f"Сравнение на промпте: «{prompt[:80]}»\n\n"
                f"| | база `qwen38-27b-gate` | RL v7 `qwen35-v7-gate` |\n|---|---|---|\n"
                f"| замер | {cell(a)} | {cell(b)} |\n\n"
                f"Схожесть текстов **{sim:.2f}**.\n\n"
                f"---\n\n**База:**\n\n{a.get('text') or '(нет ответа)'}\n\n"
                f"---\n\n**RL v7:**\n\n{b.get('text') or '(нет ответа)'}")
