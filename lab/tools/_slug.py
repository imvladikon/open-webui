"""
title: Slug resolver (helper)
author: agents-team
description: Общий резолвер живого eliza-слага. Не тулза, а вставка: OWUI-плагины грузятся
             изолированно (exec модуля), общий импорт между ними невозможен, поэтому этот код
             КОПИРУЕТСЯ в тулзы/пайпы. Здесь он лежит как единственный источник правды.
"""
# ЗАЧЕМ. Наши сервы преемптятся по одному: бывает, что qwen38 лежит, а v7 работает (проверено).
# Если тулза прибита к одному слагу, весь кокпит выглядит сломанным при живой второй модели.
# Резолвер опрашивает кандидатов и берёт первый живой, с коротким кэшем, чтобы не молотить
# /models на каждый вызов.
import os
import time

ELIZA = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1"
CANDIDATES = ["qwen38-27b-gate", "qwen35-v7-gate"]
_CACHE: dict = {"slug": None, "ts": 0.0}
_TTL = 90.0          # сек: слаг падает/встаёт минутами, чаще проверять незачем


def eliza_token() -> str:
    return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]


async def resolve_live_slug(preferred: str = "", candidates=None, timeout: float = 8.0):
    """
    Вернуть (slug, base_url) первого живого слага или (None, None).
    preferred проверяется первым, чтобы не менять модель без нужды.
    """
    import httpx
    now = time.time()
    if _CACHE["slug"] and now - _CACHE["ts"] < _TTL:
        s = _CACHE["slug"]
        return s, ELIZA.format(slug=s)

    order = [s for s in [preferred] if s] + [c for c in (candidates or CANDIDATES) if c != preferred]
    headers = {"Authorization": f"Bearer {eliza_token()}"}
    async with httpx.AsyncClient(timeout=timeout, verify=False) as cx:
        for slug in order:
            try:
                r = await cx.get(ELIZA.format(slug=slug) + "/models", headers=headers)
                if r.status_code == 200:
                    _CACHE.update(slug=slug, ts=now)
                    return slug, ELIZA.format(slug=slug)
            except Exception:
                continue
    return None, None


def no_serve_message() -> str:
    return ("Сейчас не отвечает ни один наш серв (их периодически вытесняет планировщик GPU). "
            "Лончер поднимает их сам, но 27B/35B грузятся ~30-40 минут. "
            "Проверить статус можно моделью **Serve Status** в селекторе.")
