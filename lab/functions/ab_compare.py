"""
title: A/B Compare
author: agents-team
version: 0.1.0
description: Один промпт сразу по двум чекпойнтам, ответы рядом с латентностью, длиной и схожестью. Честное сравнение base и RL: у каждого свои параметры, чего side-by-side в UI не умеет.
"""
# Зачем отдельный пайп, если в UI есть мульти-модельный режим.
# В side-by-side OWUI формирует params ОДИН РАЗ на запрос (Chat.svelte:3126) и шлёт их всем
# колонкам. Значит нельзя дать base и RL разные температуры/seed, а для сравнения чекпойнтов это
# принципиально. Здесь каждый слаг вызывается своим запросом со своими параметрами.
#
# Ответы кладём рядом, плюс замер: латентность, токены, finish_reason, длина, схожесть текстов.
import asyncio
import difflib
import os
import re
import time
from pydantic import BaseModel, Field

_ELIZA = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1"
_THINK = re.compile(r"<think>.*?</think>", re.S)
_WORD = re.compile(r"\w+", re.U)
_DRAFT = re.compile(r"^(the user|we need|need to|let me|let's|okay|first,|thinking process|"
                    r"пользователь|нужно |давай )", re.I)


def _clean(t: str) -> str:
    t = _THINK.sub("", t or "")
    if "</think>" in t:
        t = t.split("</think>")[-1]
    return t.strip()


def _sim(a: str, b: str) -> float:
    aw, bw = _WORD.findall(a.lower()), _WORD.findall(b.lower())
    if not aw and not bw:
        return 1.0
    return difflib.SequenceMatcher(None, aw, bw).ratio()


class Pipe:
    class Valves(BaseModel):
        SLUG_A: str = Field(default="qwen38-27b-gate", description="слаг A (база)")
        SLUG_B: str = Field(default="qwen35-v7-gate", description="слаг B (RL)")
        LABEL_A: str = Field(default="base", description="подпись A")
        LABEL_B: str = Field(default="RL v7", description="подпись B")
        TEMP_A: float = Field(default=0.0)
        TEMP_B: float = Field(default=0.0)
        MAX_TOKENS: int = Field(default=1200)
        SYSTEM: str = Field(
            default="Отвечай сразу итоговым результатом, без рассуждений вслух.",
            description="общий системный промпт, чтобы сравнивать ответы, а не черновики")

    def __init__(self):
        self.valves = self.Valves()

    def _token(self) -> str:
        return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]

    async def _ask(self, cx, slug, messages, temp):
        t0 = time.time()
        try:
            r = await cx.post(_ELIZA.format(slug=slug) + "/chat/completions",
                              json={"model": slug, "messages": messages, "temperature": temp,
                                    "max_tokens": self.valves.MAX_TOKENS, "stream": False},
                              headers={"Authorization": f"Bearer {self._token()}",
                                       "Content-Type": "application/json"})
            if r.status_code != 200:
                return {"slug": slug, "err": f"HTTP {r.status_code}", "ms": 0}
            d = r.json()
            ch = d["choices"][0]
            u = d.get("usage") or {}
            return {"slug": slug, "text": _clean(ch["message"].get("content") or ""),
                    "ms": int((time.time() - t0) * 1000),
                    "finish": ch.get("finish_reason"),
                    "tokens": u.get("completion_tokens")}
        except Exception as e:
            return {"slug": slug, "err": f"{type(e).__name__}", "ms": int((time.time() - t0) * 1000)}

    async def pipe(self, body: dict, __event_emitter__=None):
        import httpx
        msgs = [m for m in (body.get("messages") or []) if m.get("role") != "system"]
        if self.valves.SYSTEM:
            msgs = [{"role": "system", "content": self.valves.SYSTEM}] + msgs

        async def st(d, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": d, "done": done}})

        await st(f"спрашиваю {self.valves.LABEL_A} и {self.valves.LABEL_B} параллельно")
        async with httpx.AsyncClient(timeout=420, verify=False) as cx:
            a, b = await asyncio.gather(
                self._ask(cx, self.valves.SLUG_A, msgs, self.valves.TEMP_A),
                self._ask(cx, self.valves.SLUG_B, msgs, self.valves.TEMP_B))

        la, lb = self.valves.LABEL_A, self.valves.LABEL_B
        if a.get("err") and b.get("err"):
            await st("оба серва недоступны", True)
            return (f"Оба серва недоступны ({la}: {a['err']}, {lb}: {b['err']}). "
                    "Статус — модель **Serve Status**.")

        def cell(r):
            if r.get("err"):
                return f"⚠ {r['err']}"
            flags = []
            if r.get("finish") == "length":
                flags.append("обрезан")
            if _DRAFT.match(r.get("text", "")):
                flags.append("черновик вместо ответа")
            return (f"{r['ms']} ms · {r.get('tokens')} ток. · {len(r.get('text',''))} симв."
                    + (" · ⚠ " + ", ".join(flags) if flags else ""))

        sim = _sim(a.get("text", ""), b.get("text", ""))
        verdict = ("ответы почти совпали" if sim > 0.8 else
                   "ответы заметно разошлись" if sim < 0.4 else "ответы частично разошлись")
        faster = la if a.get("ms", 9e9) < b.get("ms", 9e9) else lb
        ratio = (max(a.get("ms", 1), b.get("ms", 1)) / max(min(a.get("ms", 1), b.get("ms", 1)), 1))

        await st(f"готово: схожесть {sim:.2f}", True)
        return (
            f"### A/B: {la} против {lb}\n\n"
            f"| | {la} `{self.valves.SLUG_A}` | {lb} `{self.valves.SLUG_B}` |\n|---|---|---|\n"
            f"| замер | {cell(a)} | {cell(b)} |\n"
            f"| температура | {self.valves.TEMP_A} | {self.valves.TEMP_B} |\n\n"
            f"Схожесть текстов **{sim:.2f}** — {verdict}. Быстрее **{faster}** (в {ratio:.1f}×).\n\n"
            f"---\n\n#### {la}\n\n{a.get('text') or '(нет ответа)'}\n\n"
            f"---\n\n#### {lb}\n\n{b.get('text') or '(нет ответа)'}")
