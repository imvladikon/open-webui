"""
title: Sampling Spread
author: agents-team
version: 0.1.0
description: N сэмплов одного промпта при разных температурах в одной таблице, с метрикой разнообразия. Быстрый способ увидеть mode-collapse у чекпойнта после RL и понять, где начинается деградация.
"""
# Зачем для пострейна. RL часто схлопывает распределение: модель отвечает почти одинаково при
# любой температуре. Глазами по одному сэмплу это не видно, а тут видно сразу: доля уникальных
# ответов и средняя попарная непохожесть по температурам.
#
# Метрика намеренно простая и честная: Jaccard по множествам слов (без эмбеддингов, без лишних
# зависимостей в airgapped-образе). Она грубая, но для «схлопнулось / не схлопнулось» достаточно.
import asyncio
import os
import re
import time
from pydantic import BaseModel, Field

_ELIZA = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1"
_CANDIDATES = ["qwen38-27b-gate", "qwen35-v7-gate"]
_SLUG_CACHE = {"slug": None, "ts": 0.0}
_THINK = re.compile(r"<think>.*?</think>", re.S)
_WORD = re.compile(r"\w+", re.U)


_LEAK = re.compile(
    r"^(we need|need |the user|let's|let me|пользователь просит|нужно |thinking process|"
    r"\*\*?thinking|analyze the request|step \d|first,? |okay,? |хорошо,? нужно)", re.I)


def _clean(t: str) -> str:
    t = _THINK.sub("", t or "")
    if "</think>" in t:
        t = t.split("</think>")[-1]
    t = " ".join(t.split()).strip()
    # если reasoning всё же протёк целиком, помечаем: иначе мерим разброс рассуждений
    return ("[рассуждение вместо ответа] " + t[:120]) if _LEAK.match(t) else t


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class Tools:
    class Valves(BaseModel):
        MODEL: str = Field(default="qwen38-27b-gate")
        MAX_TOKENS: int = Field(default=160, description="ответы держим короткими: это про разброс")
        SYSTEM: str = Field(
            default=("Отвечай СРАЗУ итоговым ответом, без рассуждений вслух, без черновиков, "
                     "без перечисления вариантов. Только результат."),
            description="без этого reasoning-модель отдаёт рассуждение, и мерялся бы его разброс")
        SAMPLES: int = Field(default=3, description="сэмплов на температуру")

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

    async def _one(self, cx, slug, base, prompt, temp):
        try:
            r = await cx.post(f"{base}/chat/completions", json={
                "model": slug,
                "messages": ([{"role": "system", "content": self.valves.SYSTEM}]
                             if self.valves.SYSTEM else []) + [{"role": "user", "content": prompt}],
                "temperature": temp, "max_tokens": self.valves.MAX_TOKENS, "stream": False},
                headers={"Authorization": f"Bearer {self._token()}",
                         "Content-Type": "application/json"})
            r.raise_for_status()
            return _clean(r.json()["choices"][0]["message"].get("content") or "")
        except Exception as e:
            return f"[ошибка: {type(e).__name__}]"

    async def sampling_spread(self, prompt: str, temperatures: str = "0.0,0.7,1.0",
                              __event_emitter__=None) -> str:
        """
        Прогнать один промпт несколько раз при разных температурах и показать разброс ответов с метрикой разнообразия. Использовать, чтобы поймать mode-collapse у чекпойнта или подобрать рабочую температуру.
        :param prompt: промпт для сэмплирования (лучше короткий, с творческой свободой)
        :param temperatures: температуры через запятую, например "0.0,0.7,1.0"
        """
        import httpx
        slug, base = await self._resolve()
        if not slug:
            return "Нет живого серва. Статус — модель **Serve Status**."
        try:
            temps = [float(x) for x in temperatures.split(",") if x.strip()]
        except Exception:
            return f"Не разобрал temperatures: {temperatures!r}"
        temps = temps[:4]

        rows, details = [], []
        async with httpx.AsyncClient(timeout=240, verify=False) as cx:
            for t in temps:
                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {
                        "description": f"t={t}: {self.valves.SAMPLES} сэмплов", "done": False}})
                outs = await asyncio.gather(*[
                    self._one(cx, slug, base, prompt, t) for _ in range(self.valves.SAMPLES)])
                uniq = len(set(outs))
                pairs = [(_jaccard(outs[i], outs[j]))
                         for i in range(len(outs)) for j in range(i + 1, len(outs))]
                overlap = sum(pairs) / len(pairs) if pairs else 1.0
                leaked = sum(1 for o in outs if o.startswith("[рассуждение"))
                verdict = ("схлопнулось" if uniq == 1 else
                           "почти одинаково" if overlap > 0.8 else
                           "разнообразно" if overlap < 0.5 else "умеренно")
                if leaked:
                    verdict += f" ⚠ {leaked}/{len(outs)} рассуждение"
                rows.append(f"| {t} | {uniq}/{len(outs)} | {overlap:.2f} | {verdict} |")
                details.append(f"**t={t}**\n\n" + "\n".join(
                    f"{i+1}. {o[:220]}" for i, o in enumerate(outs)))

        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": "готово", "done": True}})
        return ("### Разброс сэмплов · `" + slug + "`\n\n"
                f"Промпт: _{prompt[:100]}_\n\n"
                "| темп. | уникальных | пересечение слов | вывод |\n|---|---|---|---|\n"
                + "\n".join(rows) + "\n\n"
                "Пересечение около 1.0 при высокой температуре = распределение схлопнуто "
                "(типично после агрессивного RL). Около 0.3-0.5 = здоровое разнообразие.\n\n"
                "Пометка «рассуждение» значит, что модель выдала ход мысли вместо ответа: тогда "
                "метрика мерит разброс РАССУЖДЕНИЙ и ей верить нельзя. Лечится системным "
                "промптом в валвах или reasoning-парсером на серве.\n\n"
                + "\n\n".join(details))
