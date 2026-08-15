"""
title: LLM Debug
author: agents-team
version: 0.1.0
description: Отладка модели прямо в чате - confidence heatmap по токенам, near-miss альтернативы, уверенность выбора тулзы, валидация аргументов против схемы, детектор циклов, бюджет контекста. Работает на логпробах нашего sglang-серва.
"""
# Проверено на нашем слаге (eliza -> sglang, Qwen3.8-27B):
#   logprobs:true + top_logprobs:N возвращаются, В ТОМ ЧИСЛЕ во время tool-call
#   usage отдаёт prompt_tokens / completion_tokens / reasoning_tokens
#   finish_reason различает stop / length (тихая обрезка) / tool_calls
# Чего НЕТ через eliza: cached_tokens, prefix-cache hit rate, KV/queue (живут на /metrics серва).
import json
import math
import os
import re
from pydantic import BaseModel, Field

# Порог "почти выбрал другое": разница логпробов лидера и второго места
NEAR_MISS_MARGIN = 1.2


def _p(logprob: float) -> float:
    return math.exp(logprob)


def _bucket(p: float) -> str:
    if p >= 0.90:
        return "high"
    if p >= 0.60:
        return "mid"
    if p >= 0.30:
        return "low"
    return "verylow"


_SHADE = {"high": "#e8f0e6", "mid": "#fdf3e0", "low": "#fbe3d4", "verylow": "#f7c9b6"}
_INK = "#2d3142"
_ACCENT = "#eb6c36"


class Tools:
    class Valves(BaseModel):
        ELIZA_BASE: str = Field(
            default="https://api.eliza.yandex.net/raw/internal/zeliboba/qwen38-27b-gate/v1",
            description="OpenAI-эндпоинт отлаживаемой модели")
        MODEL: str = Field(default="qwen38-27b-gate", description="имя модели у бэкенда")
        TOP_LOGPROBS: int = Field(default=4, description="сколько альтернатив тянуть на токен")
        MAX_TOKENS: int = Field(default=400, description="потолок генерации при зонде")

    def __init__(self):
        self.valves = self.Valves()

    def _token(self) -> str:
        return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]

    async def _probe(self, messages, tools=None, temperature=0.0):
        import httpx
        payload = {
            "model": self.valves.MODEL, "messages": messages, "temperature": temperature,
            "max_tokens": self.valves.MAX_TOKENS, "stream": False,
            "logprobs": True, "top_logprobs": self.valves.TOP_LOGPROBS,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=180, verify=False) as cx:
            r = await cx.post(f"{self.valves.ELIZA_BASE}/chat/completions", json=payload,
                              headers={"Authorization": f"Bearer {self._token()}",
                                       "Content-Type": "application/json"})
            r.raise_for_status()
            return r.json()

    # ---------- 1. confidence heatmap + near-miss ----------
    async def inspect_confidence(self, prompt: str, __event_emitter__=None) -> str:
        """
        Прогнать промпт и показать ответ раскрашенным по уверенности модели (heatmap) плюс места, где модель почти выбрала другое слово. Использовать, когда надо понять, где модель "плавает" и откуда берётся галлюцинация.
        :param prompt: запрос, который надо продиагностировать
        """
        d = await self._probe([{"role": "user", "content": prompt}])
        ch = d["choices"][0]
        toks = (ch.get("logprobs") or {}).get("content") or []
        if not toks:
            return "Бэкенд не вернул logprobs. Проверь, что серв их отдаёт."

        near, points = [], []
        for i, t in enumerate(toks):
            p = _p(t["logprob"])
            points.append({"i": i, "p": round(p, 4), "t": (t["token"] or "")[:12]})
            alts = t.get("top_logprobs") or []
            if len(alts) > 1 and (alts[0]["logprob"] - alts[1]["logprob"]) < NEAR_MISS_MARGIN \
                    and (t["token"] or "").strip():
                near.append((t["token"], [(a["token"], _p(a["logprob"])) for a in alts[:3]]))

        u = d.get("usage") or {}
        avg = sum(_p(t["logprob"]) for t in toks) / len(toks)
        low = [t for t in toks if _p(t["logprob"]) < 0.30]

        # Компактный vega-lite: OWUI рендерит его инлайн из ~1 КБ спеки.
        # HTML-артефакт тут НЕ используем: модель физически не дотянет 17 КБ дословного
        # копирования (упирается в max_tokens, блок не закрывается, артефакт не рендерится).
        step = max(1, len(points) // 120)
        vega = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "token confidence",
            "width": 560, "height": 130,
            "data": {"values": points[::step]},
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "i", "type": "quantitative", "title": "позиция токена"},
                "y": {"field": "p", "type": "quantitative", "title": "p", "scale": {"domain": [0, 1]}},
                "color": {"field": "p", "type": "quantitative",
                          "scale": {"scheme": "orangered", "reverse": True}, "legend": None},
                "tooltip": [{"field": "t", "title": "токен"}, {"field": "p", "title": "p"}],
            },
        }
        near_rows = "\n".join(
            f"| `{tok}` | " + " · ".join(f"`{a}` {pp:.2f}" for a, pp in alts) + " |"
            for tok, alts in near[:12])
        low_list = ", ".join(f"`{t['token']}`({_p(t['logprob']):.2f})" for t in low[:10]) or "нет"

        md = f"""**Уверенность модели** · {self.valves.MODEL} · finish_reason `{ch.get('finish_reason')}`\
{'  ⚠ **ОТВЕТ ОБРЕЗАН ПО ЛИМИТУ**' if ch.get('finish_reason') == 'length' else ''}

| средняя p | токенов | неуверенных (p<0.3) | prompt | reasoning | completion |
|---|---|---|---|---|---|
| **{avg:.3f}** | {len(toks)} | **{len(low)}** | {u.get('prompt_tokens')} | {u.get('reasoning_tokens')} | {u.get('completion_tokens')} |

```vega-lite
{json.dumps(vega, ensure_ascii=False)}
```

**Самые неуверенные токены:** {low_list}

**Почти выбрал другое** ({len(near)} мест, показаны первые 12)

| выбрано | альтернативы (p) |
|---|---|
{near_rows or "| нет спорных мест | |"}
"""
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"heatmap готов: средняя {avg:.2f}, неуверенных {len(low)}", "done": True}})
        return ("ГОТОВЫЙ ОТЧЁТ. Выведи его ДОСЛОВНО, целиком (включая блок ```vega-lite — он "
                "отрисуется графиком). После отчёта добавь максимум одну строку вывода.\n\n" + md)

    # ---------- 2. tool choice + args + budget ----------
    async def inspect_tool_choice(self, prompt: str, tools_json: str,
                                  __event_emitter__=None) -> str:
        """
        Проверить, как модель выбирает инструмент: какой позвала, с каким отрывом от следующего кандидата, валидны ли аргументы по схеме, и сколько контекста съели описания тулзов. Использовать при отладке агентного поведения и tool-calling.
        :param prompt: пользовательский запрос
        :param tools_json: JSON-массив инструментов в формате OpenAI (tools=[{"type":"function","function":{...}}])
        """
        try:
            tools = json.loads(tools_json)
            if isinstance(tools, dict):
                tools = [tools]
        except Exception as e:
            return f"Не разобрал tools_json: {e}"

        base = await self._probe([{"role": "user", "content": prompt}])
        with_tools = await self._probe([{"role": "user", "content": prompt}], tools=tools)

        ch = with_tools["choices"][0]
        msg = ch["message"]
        calls = msg.get("tool_calls") or []
        u_b, u_t = base.get("usage") or {}, with_tools.get("usage") or {}
        schema_cost = (u_t.get("prompt_tokens") or 0) - (u_b.get("prompt_tokens") or 0)

        # уверенность на токенах имени функции
        toks = (ch.get("logprobs") or {}).get("content") or []
        name_conf = []
        if calls:
            want = calls[0]["function"]["name"]
            for t in toks:
                frag = (t["token"] or "").strip().strip('"')
                if frag and frag in want and len(frag) > 2:
                    alts = [(a["token"], _p(a["logprob"])) for a in (t.get("top_logprobs") or [])[:4]]
                    name_conf.append((t["token"], _p(t["logprob"]), alts))

        # валидация аргументов против схемы
        problems, used = [], []
        by_name = {t["function"]["name"]: t["function"] for t in tools
                   if isinstance(t, dict) and "function" in t}
        for c in calls:
            fn = c["function"]["name"]
            used.append(fn)
            spec = by_name.get(fn)
            if not spec:
                problems.append(f"вызван неизвестный инструмент <b>{fn}</b> (галлюцинация имени)")
                continue
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except Exception as e:
                problems.append(f"<b>{fn}</b>: аргументы не парсятся как JSON ({e})")
                continue
            params = spec.get("parameters") or {}
            props, req = params.get("properties") or {}, params.get("required") or []
            for r in req:
                if r not in args:
                    problems.append(f"<b>{fn}</b>: пропущено обязательное поле <code>{r}</code>")
            for k in args:
                if props and k not in props:
                    problems.append(f"<b>{fn}</b>: лишнее поле <code>{k}</code> (нет в схеме)")

        offered = list(by_name)
        unused = [t for t in offered if t not in used]
        # parser health: XML утёк в контент вместо структурного вызова
        leaked = bool(re.search(r"<function=|<tool_call>", msg.get("content") or ""))

        rows = "\n".join(
            f"| `{t}` | {p:.3f} | " + " · ".join(f"`{a}` {pp:.2f}" for a, pp in alts) + " |"
            for t, p, alts in name_conf[:8])
        probs = "\n".join(f"- {re.sub(r'</?b>|</?code>', '`', x)}" for x in problems) \
            or "- нарушений схемы нет"
        warns = []
        if leaked:
            warns.append("⚠ **Парсер не отработал:** XML утёк в content вместо структурного tool_call")
        if ch.get("finish_reason") == "length":
            warns.append("⚠ **Ответ обрезан по лимиту токенов**")
        per_tool = int(schema_cost / max(len(offered), 1))

        md = f"""**Отладка выбора инструмента** · {self.valves.MODEL}

| позвала | схемы стоили | на инструмент | finish_reason |
|---|---|---|---|
| **{used[0] if used else 'нет вызова'}** | **{schema_cost}** токенов ({len(offered)} шт) | ~{per_tool} | `{ch.get('finish_reason')}` |

{chr(10).join(warns)}

**Уверенность на имени инструмента**

| токен | p | что почти выбрал вместо |
|---|---|---|
{rows or "| токены имени не выделились | | смотри inspect_confidence |"}

**Аргументы против схемы**
{probs}

**Бюджет инструментов**
- предложено: `{', '.join(offered) or '-'}`
- использовано: `{', '.join(used) or '-'}`
- не пригодилось: `{', '.join(unused) or '-'}`
{f'- неиспользованные схемы это налог на контекст: ~{per_tool} токенов на инструмент в КАЖДОМ запросе' if unused else ''}
"""
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"tool debug: {used[0] if used else 'без вызова'}, схемы {schema_cost} ток.",
                "done": True}})
        return ("ГОТОВЫЙ ОТЧЁТ. Выведи его ДОСЛОВНО и целиком. После отчёта максимум одна строка вывода.\n\n" + md)

    # ---------- 3. loop detector ----------
    def detect_tool_loops(self, trace_json: str) -> str:
        """
        Найти в траектории циклы: повторные вызовы одного инструмента с теми же аргументами, и вызовы без прогресса. Использовать при разборе зависшего или неэффективного роллаута.
        :param trace_json: JSON-массив сообщений траектории (OpenAI-формат с tool_calls)
        """
        try:
            msgs = json.loads(trace_json)
        except Exception as e:
            return f"Не разобрал trace_json: {e}"
        seen, loops, seq = {}, [], []
        for m in msgs:
            for c in (m.get("tool_calls") or []):
                key = (c["function"]["name"], (c["function"].get("arguments") or "").strip())
                seq.append(c["function"]["name"])
                seen[key] = seen.get(key, 0) + 1
                if seen[key] == 3:
                    loops.append(f"{c['function']['name']} с одинаковыми аргументами вызван 3+ раз")
        rep = []
        for i in range(len(seq) - 3):
            if seq[i] == seq[i + 2] and seq[i + 1] == seq[i + 3]:
                rep.append(f"чередование {seq[i]} / {seq[i+1]} (пинг-понг)")
                break
        out = loops + rep
        return ("Циклы: " + "; ".join(out)) if out else \
            f"Циклов нет. Всего вызовов: {len(seq)}, уникальных: {len(seen)}."
