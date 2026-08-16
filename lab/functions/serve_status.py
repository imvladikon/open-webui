"""
title: Serve Status
author: agents-team
version: 0.1.0
description: Живой статус наших eliza-слагов прямо в чате. Работает, ДАЖЕ когда все модели лежат, потому что ничего у моделей не спрашивает. Отвечает на вопрос «почему Model not found».
"""
# Зачем. Наши сервы живут на преемптимом GPU и периодически падают (abort_reason=preemption).
# В этот момент любой чат отвечает «Model not found», и человек думает, что сломан UI.
# Этот pipe — обычный Python внутри OWUI: он НЕ обращается к модели, а сам опрашивает слаги,
# поэтому доступен всегда и объясняет, что происходит и когда ждать.
import asyncio
import os
import time
from pydantic import BaseModel, Field

SLUGS = {
    "qwen38-27b-gate": "Qwen3.8-27B (база; на ней tools, autocontinue, project_gen)",
    "qwen35-v7-gate": "Qwen3.5-35B RL v7 (booking-RL, прод-ветка)",
}
BASE = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1/models"


class Pipe:
    class Valves(BaseModel):
        TIMEOUT: float = Field(default=12.0, description="таймаут проверки одного слага, сек")
        SHOW_HINT: bool = Field(default=True, description="объяснять причину простоя")

    def __init__(self):
        self.valves = self.Valves()

    def _token(self) -> str:
        return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]

    async def _probe(self, cx, slug: str):
        t0 = time.time()
        try:
            r = await cx.get(BASE.format(slug=slug),
                             headers={"Authorization": f"Bearer {self._token()}"})
            return slug, r.status_code, int((time.time() - t0) * 1000)
        except Exception as e:
            return slug, f"ERR {type(e).__name__}", int((time.time() - t0) * 1000)

    async def pipe(self, body: dict, __event_emitter__=None):
        import httpx
        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": "опрашиваю слаги", "done": False}})
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT, verify=False) as cx:
            results = await asyncio.gather(*(self._probe(cx, s) for s in SLUGS))

        rows, up, down = [], 0, 0
        for slug, code, ms in results:
            ok = code == 200
            up, down = up + int(ok), down + int(not ok)
            mark = "🟢 работает" if ok else ("🔴 лежит" if code in (400, 404) else f"🟠 {code}")
            rows.append(f"| `{slug}` | {mark} | {code} | {ms} ms | {SLUGS[slug]} |")

        hint = ""
        if down and self.valves.SHOW_HINT:
            hint = (
                "\n\n**Почему «Model not found».** Сервы живут на преемптимом GPU: планировщик "
                "забирает их под чужие джобы в том же пуле (в логах `abort_reason=preemption`). "
                "Лончер поднимает серв обратно сам, но 27B/35B грузятся **~30-40 минут** — всё это "
                "время слаг отвечает 400, и чат честно говорит, что модели нет.\n\n"
                "Что делать сейчас: подождать окно и повторить. Что чинит насовсем: гарантийная "
                "квота под сервы либо больший вес в пуле (влияет на джобы коллег, решение за нами)."
            )
        head = ("Все сервы подняты." if not down else
                f"Доступно {up} из {len(SLUGS)}. Часть моделей сейчас недоступна.")
        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": f"{up}/{len(SLUGS)} up", "done": True}})
        return ("### Статус сервов\n\n" + head + "\n\n"
                "| слаг | статус | код | отклик | что это |\n|---|---|---|---|---|\n"
                + "\n".join(rows) + hint)
