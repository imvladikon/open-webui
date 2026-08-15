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

        spans, near = [], []
        for t in toks:
            p = _p(t["logprob"])
            b = _bucket(p)
            txt = (t["token"] or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            spans.append(f'<span class="{b}" title="p={p:.3f}">{txt}</span>')
            alts = t.get("top_logprobs") or []
            if len(alts) > 1 and (alts[0]["logprob"] - alts[1]["logprob"]) < NEAR_MISS_MARGIN \
                    and (t["token"] or "").strip():
                near.append((t["token"], [(a["token"], _p(a["logprob"])) for a in alts[:3]]))

        u = d.get("usage") or {}
        avg = sum(_p(t["logprob"]) for t in toks) / len(toks)
        low = sum(1 for t in toks if _p(t["logprob"]) < 0.30)
        rows = "".join(
            f"<tr><td><code>{tok}</code></td><td>" +
            " ".join(f'<span class="alt">{a}<b>{pp:.2f}</b></span>' for a, pp in alts) +
            "</td></tr>" for tok, alts in near[:25])

        page = f"""<html><head><meta charset="utf-8"><title>Confidence</title><style>
body{{margin:0;padding:24px;background:#f5f5f5;color:{_INK};font-family:Geist,ui-sans-serif,system-ui,sans-serif}}
h1{{font-size:1.4rem;font-weight:400;margin:0 0 4px}} .sub{{color:#4f5d75;font-size:12px;margin-bottom:18px}}
.text{{background:#fff;border:1px solid #bfc0c0;padding:16px;line-height:2;white-space:pre-wrap;
      font-family:Geist Mono,ui-monospace,monospace;font-size:13px;border-radius:6px}}
.high{{background:{_SHADE['high']}}} .mid{{background:{_SHADE['mid']}}}
.low{{background:{_SHADE['low']}}} .verylow{{background:{_SHADE['verylow']};border-bottom:2px solid {_ACCENT}}}
table{{border-collapse:collapse;margin-top:20px;width:100%;font-size:12px}}
td,th{{border-bottom:1px solid #bfc0c0;padding:6px 8px;text-align:left;vertical-align:top}}
.alt{{display:inline-block;margin-right:10px;color:#4f5d75}} .alt b{{color:{_INK};margin-left:4px}}
.legend span{{display:inline-block;padding:2px 8px;margin-right:6px;border-radius:3px;font-size:11px}}
.stat{{display:inline-block;margin-right:18px;font-size:12px;color:#4f5d75}} .stat b{{color:{_INK}}}
</style></head><body>
<h1>Уверенность модели по токенам</h1>
<div class="sub">{self.valves.MODEL} · finish_reason: <b>{ch.get('finish_reason')}</b>
{' · ⚠ ОТВЕТ ОБРЕЗАН ПО ЛИМИТУ' if ch.get('finish_reason') == 'length' else ''}</div>
<div class="legend">
  <span class="high">p ≥ 0.90</span><span class="mid">0.60-0.90</span>
  <span class="low">0.30-0.60</span><span class="verylow">&lt; 0.30 сомнение</span>
</div>
<p><span class="stat">средняя уверенность <b>{avg:.3f}</b></span>
<span class="stat">токенов <b>{len(toks)}</b></span>
<span class="stat">неуверенных <b>{low}</b></span>
<span class="stat">prompt <b>{u.get('prompt_tokens')}</b></span>
<span class="stat">reasoning <b>{u.get('reasoning_tokens')}</b></span>
<span class="stat">completion <b>{u.get('completion_tokens')}</b></span></p>
<div class="text">{''.join(spans)}</div>
<h2 style="font-size:1rem;font-weight:400;margin-top:26px">Почти выбрал другое ({len(near)})</h2>
<table><tr><th>выбрано</th><th>альтернативы (p)</th></tr>{rows or '<tr><td colspan=2>нет спорных мест</td></tr>'}</table>
</body></html>"""
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"heatmap готов: средняя {avg:.2f}, неуверенных {low}", "done": True}})
        return ("ГОТОВЫЙ HTML-ОТЧЁТ. Выведи его ДОСЛОВНО в блоке ```html, целиком, чтобы он открылся "
                "в панели артефактов. После блока добавь одну строку вывода.\n\n```html\n" + page + "\n```")

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

        rows = "".join(
            f"<tr><td><code>{t}</code></td><td>{p:.3f}</td><td>" +
            " ".join(f'<span class="alt">{a}<b>{pp:.2f}</b></span>' for a, pp in alts) +
            "</td></tr>" for t, p, alts in name_conf[:10])
        probs = "".join(f"<li>{x}</li>" for x in problems) or "<li>нарушений схемы нет</li>"

        page = f"""<html><head><meta charset="utf-8"><title>Tool debug</title><style>
body{{margin:0;padding:24px;background:#f5f5f5;color:{_INK};font-family:Geist,ui-sans-serif,system-ui,sans-serif}}
h1{{font-size:1.4rem;font-weight:400;margin:0 0 16px}} h2{{font-size:1rem;font-weight:400;margin:24px 0 8px}}
.card{{background:#fff;border:1px solid #bfc0c0;border-radius:6px;padding:14px 16px;margin-bottom:12px}}
.big{{font-size:1.6rem}} .warn{{border-left:3px solid {_ACCENT};background:rgba(235,108,54,0.06)}}
table{{border-collapse:collapse;width:100%;font-size:12px}}
td,th{{border-bottom:1px solid #bfc0c0;padding:6px 8px;text-align:left}}
.alt{{display:inline-block;margin-right:10px;color:#4f5d75}} .alt b{{color:{_INK};margin-left:4px}}
code{{font-family:Geist Mono,ui-monospace,monospace;font-size:12px}}
.stat{{display:inline-block;margin-right:22px}} .stat span{{display:block;font-size:11px;color:#4f5d75}}
ul{{margin:6px 0 0 18px;padding:0}} li{{margin:3px 0;font-size:13px}}
</style></head><body>
<h1>Отладка выбора инструмента</h1>
<div class="card">
  <span class="stat"><b class="big">{used[0] if used else 'нет вызова'}</b><span>что позвала модель</span></span>
  <span class="stat"><b class="big">{schema_cost}</b><span>токенов съели схемы ({len(offered)} шт)</span></span>
  <span class="stat"><b class="big">{ch.get('finish_reason')}</b><span>finish_reason</span></span>
</div>
{'<div class="card warn">⚠ Парсер не отработал: XML утёк в content вместо структурного tool_call</div>' if leaked else ''}
{'<div class="card warn">⚠ Ответ обрезан по лимиту токенов (finish_reason=length)</div>' if ch.get('finish_reason') == 'length' else ''}
<h2>Уверенность на имени инструмента</h2>
<table><tr><th>токен</th><th>p</th><th>что почти выбрал вместо</th></tr>
{rows or '<tr><td colspan=3>токены имени не выделились, смотри heatmap целиком</td></tr>'}</table>
<h2>Аргументы против схемы</h2><ul>{probs}</ul>
<h2>Бюджет инструментов</h2>
<div class="card">предложено: <code>{', '.join(offered) or '-'}</code><br>
использовано: <code>{', '.join(used) or '-'}</code><br>
не пригодилось: <code>{', '.join(unused) or '-'}</code>
{f'<br><br>Неиспользованные схемы это чистый налог на контекст: ~{int(schema_cost/max(len(offered),1))} токенов на инструмент за каждый запрос.' if unused else ''}
</div>
</body></html>"""
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"tool debug: {used[0] if used else 'без вызова'}, схемы {schema_cost} ток.",
                "done": True}})
        return ("ГОТОВЫЙ HTML-ОТЧЁТ. Выведи его ДОСЛОВНО в блоке ```html, целиком, чтобы он открылся "
                "в панели артефактов. После блока одна строка вывода.\n\n```html\n" + page + "\n```")

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
