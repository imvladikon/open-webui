"""
title: Context Budget
author: agents-team
version: 0.1.0
description: Куда уходят токены контекста: системный промпт, схемы инструментов, история, текущий вопрос. Меряет по-настоящему (через prompt_tokens бэкенда), а не эвристикой по символам.
"""
# Зачем инженеру. Схемы тулзов стоят дорого и незаметно: замерено, что три тривиальных тулзы
# добавили ~400 токенов к КАЖДОМУ запросу (~130 на инструмент). История и системный промпт тоже
# съедают окно. В UI этого нигде не видно.
#
# Как меряем. Не эвристикой (символы/4 врёт на кириллице в разы), а разностью реальных
# prompt_tokens: шлём максимально короткий запрос с разным составом и вычитаем. Стоит N дешёвых
# вызовов с max_tokens=1.
import json
import os
import time
from pydantic import BaseModel, Field

_ELIZA = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1"
_CANDIDATES = ["qwen38-27b-gate", "qwen35-v7-gate"]
_SLUG_CACHE = {"slug": None, "ts": 0.0}


class Tools:
    class Valves(BaseModel):
        MODEL: str = Field(default="qwen38-27b-gate", description="предпочтительный слаг")
        CONTEXT_WINDOW: int = Field(default=32768, description="окно модели для процентов")

    def __init__(self):
        self.valves = self.Valves()

    def _token(self) -> str:
        return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]

    async def _resolve(self):
        import httpx
        now = time.time()
        if _SLUG_CACHE["slug"] and now - _SLUG_CACHE["ts"] < 90:
            s = _SLUG_CACHE["slug"]
            return s, _ELIZA.format(slug=s)
        order = [self.valves.MODEL] + [c for c in _CANDIDATES if c != self.valves.MODEL]
        async with httpx.AsyncClient(timeout=8, verify=False) as cx:
            for slug in order:
                try:
                    r = await cx.get(_ELIZA.format(slug=slug) + "/models",
                                     headers={"Authorization": f"Bearer {self._token()}"})
                    if r.status_code == 200:
                        _SLUG_CACHE.update(slug=slug, ts=now)
                        return slug, _ELIZA.format(slug=slug)
                except Exception:
                    continue
        return None, None

    async def _count(self, cx, slug, base, messages, tools=None) -> int:
        """Реальные prompt_tokens: генерируем 1 токен, читаем usage."""
        payload = {"model": slug, "messages": messages, "max_tokens": 1, "temperature": 0}
        if tools:
            payload["tools"] = tools
        r = await cx.post(f"{base}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {self._token()}",
                                   "Content-Type": "application/json"})
        r.raise_for_status()
        return (r.json().get("usage") or {}).get("prompt_tokens") or 0

    async def measure_context(self, sample_text: str = "", tools_json: str = "",
                              system_prompt: str = "", __messages__=None,
                              __event_emitter__=None) -> str:
        """
        Показать, из чего складывается контекст запроса и сколько токенов стоит каждая часть: системный промпт, схемы инструментов, история диалога, текущий текст. Использовать, когда надо понять, почему промпт большой или сколько стоят подключённые инструменты.
        :param sample_text: текст, стоимость которого измерить (если пусто, берётся последнее сообщение)
        :param tools_json: JSON-массив инструментов OpenAI-формата, чтобы измерить цену их схем
        :param system_prompt: системный промпт, стоимость которого измерить
        """
        import httpx
        slug, base = await self._resolve()
        if not slug:
            return ("Нет живого серва — измерить нечем. Статус смотри моделью **Serve Status**.")

        history = [m for m in (__messages__ or []) if m.get("role") in ("user", "assistant")]
        text = sample_text or (history[-1].get("content") if history else "") or "тест"
        tools = None
        if tools_json.strip():
            try:
                tools = json.loads(tools_json)
                if isinstance(tools, dict):
                    tools = [tools]
            except Exception as e:
                return f"tools_json не разобрался: {e}"

        async with httpx.AsyncClient(timeout=120, verify=False) as cx:
            if __event_emitter__:
                await __event_emitter__({"type": "status",
                                         "data": {"description": "меряю базовую стоимость", "done": False}})
            base_msgs = [{"role": "user", "content": "x"}]
            n_base = await self._count(cx, slug, base, base_msgs)
            n_text = await self._count(cx, slug, base, [{"role": "user", "content": text}])
            n_sys = n_base
            if system_prompt.strip():
                n_sys = await self._count(cx, slug, base,
                                          [{"role": "system", "content": system_prompt},
                                           {"role": "user", "content": "x"}])
            n_tools = n_base
            if tools:
                n_tools = await self._count(cx, slug, base, base_msgs, tools=tools)
            n_hist = n_base
            if history:
                hist_msgs = [{"role": m["role"], "content": m.get("content") or ""}
                             for m in history if m.get("content")]
                if hist_msgs:
                    n_hist = await self._count(cx, slug, base, hist_msgs + [{"role": "user", "content": "x"}])

        c_text = max(n_text - n_base, 0)
        c_sys = max(n_sys - n_base, 0)
        c_tools = max(n_tools - n_base, 0)
        c_hist = max(n_hist - n_base, 0)
        total = c_text + c_sys + c_tools + c_hist + n_base
        win = self.valves.CONTEXT_WINDOW

        def row(name, v, extra=""):
            pct = 100 * v / max(total, 1)
            bar = "█" * max(1, round(pct / 5)) if v else ""
            return f"| {name} | **{v}** | {pct:.0f}% | {bar} {extra} |"

        rows = [
            row("служебная обвязка", n_base, "chat-template, роли"),
            row("системный промпт", c_sys),
            row("схемы инструментов", c_tools,
                f"~{c_tools // max(len(tools or []), 1)} на инструмент" if tools else ""),
            row("история диалога", c_hist, f"{len(history)} сообщ."),
            row("текущий текст", c_text),
        ]
        notes = []
        if tools and c_tools:
            notes.append(f"Инструменты стоят **{c_tools} токенов в КАЖДОМ запросе**, даже когда "
                         "модель их не зовёт. Неиспользуемые тулзы отключай у пресета.")
        if c_hist > total * 0.5:
            notes.append("История съедает больше половины промпта: пора новый чат или компакция.")
        notes.append(f"Занято ~{total} из {win} токенов окна ({100*total/win:.0f}%).")

        return ("### Бюджет контекста · `" + slug + "`\n\n"
                "| часть | токенов | доля | |\n|---|---|---|---|\n" + "\n".join(rows) +
                f"\n| **итого** | **{total}** | | |\n\n" + "\n\n".join(notes) +
                "\n\nИзмерено реальными `prompt_tokens` бэкенда (не эвристикой по символам: "
                "на кириллице она врёт в разы).")
