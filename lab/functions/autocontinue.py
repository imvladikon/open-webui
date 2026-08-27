"""
title: Auto-continue (unlimited output)
author: agents-team
version: 0.1.0
description: Модель без обрыва на длинных ответах. Если генерация упёрлась в max_tokens, харнесс сам просит продолжить с места обрыва и склеивает куски, пока ответ не завершится или не кончится контекст.
"""
# Зачем. На длинных code-ответах модель упирается в max_tokens (проверено: finish_reason=length,
# completion_tokens ровно равны лимиту), ответ рвётся посреди кода. Поднимать лимит бесконечно
# нельзя: он ограничен окном контекста. Решение - автопродолжение: шлём ещё один запрос с уже
# сгенерированным куском как assistant-сообщением и просьбой продолжить ровно с места обрыва.
#
# Ограничение честное: суммарно prompt + все куски должны влезть в окно контекста модели.
# "Сколько угодно токенов" = сколько угодно ДО окна; дальше нужен summarize/compaction.
import os
from pydantic import BaseModel, Field


_ELIZA_T = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1"
_CANDIDATES = ["qwen38-27b-gate", "qwen35-v7-gate"]
_SLUG_CACHE = {"slug": None, "ts": 0.0}


async def _resolve_live(preferred, token, timeout=8.0):
    """Первый ЖИВОЙ слаг: сервы преемптятся по одному, прибитый слаг = мнимая поломка."""
    import httpx, time as _t
    now = _t.time()
    if _SLUG_CACHE["slug"] and now - _SLUG_CACHE["ts"] < 90:
        s = _SLUG_CACHE["slug"]
        return s, _ELIZA_T.format(slug=s)
    order = [preferred] + [c for c in _CANDIDATES if c != preferred]
    async with httpx.AsyncClient(timeout=timeout, verify=False) as cx:
        for slug in order:
            try:
                r = await cx.get(_ELIZA_T.format(slug=slug) + "/models",
                                 headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 200:
                    _SLUG_CACHE.update(slug=slug, ts=now)
                    return slug, _ELIZA_T.format(slug=slug)
            except Exception:
                continue
    return None, None

CONT_HINT = ("Продолжи ровно с места обрыва. НЕ повторяй уже написанное, не извиняйся, "
             "не начинай заново, не добавляй вступлений. Просто продолжи текст с той же позиции.")


class Pipe:
    class Valves(BaseModel):
        ELIZA_BASE: str = Field(
            default="https://api.eliza.yandex.net/raw/internal/zeliboba/qwen38-27b-gate/v1")
        MODEL: str = Field(default="qwen38-27b-gate")
        CHUNK_TOKENS: int = Field(default=8192, description="лимит на один кусок")
        MAX_ROUNDS: int = Field(default=6, description="сколько раз продолжать (потолок)")
        TEMPERATURE: float = Field(default=0.3)
        SYSTEM: str = Field(
            default=("Отвечай сразу итоговым результатом. НЕ рассуждай вслух, не пиши черновики. "
                     "Для задач на код: короткое вступление, один цельный блок кода, 2-3 строки пояснения."),
            description="системный промпт; для reasoning-моделей без парсера экономит бюджет")

    def __init__(self):
        self.valves = self.Valves()

    def _token(self) -> str:
        return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]

    async def pipe(self, body: dict, __event_emitter__=None):
        import httpx
        msgs = [m for m in (body.get("messages") or []) if m.get("role") != "system"]
        if self.valves.SYSTEM:
            msgs = [{"role": "system", "content": self.valves.SYSTEM}] + msgs

        slug, base = await _resolve_live(self.valves.MODEL, self._token())
        if not slug:
            return ("Сейчас не отвечает ни один наш серв (вытеснение планировщиком GPU, "
                    "подъём ~30-40 мин). Статус — модель **Serve Status** в селекторе.")
        acc, rounds, usage_total = "", 0, {"completion_tokens": 0, "prompt_tokens": 0}
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=600, verify=False) as cx:
            while rounds <= self.valves.MAX_ROUNDS:
                payload = {"model": slug, "messages": msgs, "stream": False,
                           "temperature": self.valves.TEMPERATURE,
                           "max_tokens": self.valves.CHUNK_TOKENS}
                r = await cx.post(f"{base}/chat/completions",
                                  json=payload, headers=headers)
                if r.status_code != 200:
                    return acc + f"\n\n⚠ бэкенд вернул {r.status_code}: {r.text[:200]}"
                d = r.json()
                ch = d["choices"][0]
                piece = ch["message"].get("content") or ""
                acc += piece
                u = d.get("usage") or {}
                usage_total["completion_tokens"] += u.get("completion_tokens") or 0
                usage_total["prompt_tokens"] = u.get("prompt_tokens") or 0

                if ch.get("finish_reason") != "length":
                    break                      # ответ закончился сам

                rounds += 1
                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {
                        "description": f"ответ длиннее лимита, продолжаю (часть {rounds + 1})",
                        "done": False}})
                # ключевое: отдаём модели её же кусок как assistant и просим продолжить
                msgs = msgs + [{"role": "assistant", "content": piece},
                               {"role": "user", "content": CONT_HINT}]

        if acc.count("```") % 2 == 1:          # страховка: не оставляем сломанную разметку
            acc += "\n```"
        note = ""
        if rounds:
            note = (f"\n\n*склеено из {rounds + 1} частей · "
                    f"{usage_total['completion_tokens']} токенов сгенерировано*")
        if rounds > self.valves.MAX_ROUNDS:
            note += "\n\n> ⚠ Достигнут потолок продолжений, ответ может быть неполным."
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"готово: {rounds + 1} частей, "
                               f"{usage_total['completion_tokens']} токенов", "done": True}})
        return acc + note
