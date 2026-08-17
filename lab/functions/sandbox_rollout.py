"""
title: Песочница (юзер-сим + агент + судья)
author: agents-team
version: 0.1.0
description: Прогон задачи как в нашем RL-контуре: модель играет агента, вторая модель играет пользователя, инструменты отвечают известными данными, судья ставит оценку по осям. Диалог виден в чате по ходу, в конце вердикт и разбор.
"""
# ЗАЧЕМ ЭТО ПАЙП, А НЕ ТУЛЗА. Это не «ответ на вопрос», а ПРОГОН: несколько ходов
# между тремя моделями, с инструментами и оценкой. Такой харнесс — законный повод
# быть отдельным пунктом в списке моделей (в отличие от статуса сервов, который
# переехал в инструмент).
#
# ЧЕСТНАЯ ГРАНИЦА. Это ПРИБЛИЖЕНИЕ прод-контура, а не он сам. Настоящая среда
# бронирования живёт в Аркадии (RL-Server / EOS) со своими tool-контрактами и
# наградой. Здесь: тот же СКЕЛЕТ (скрытая цель у юзер-сима, инструменты, судья по
# осям), но инструменты отвечают фикстурами, а не боевым бэкендом. Поэтому цифры
# отсюда — для отладки поведения, НЕ для отчётности по качеству.
#
# 🔑 ЧТО ЗДЕСЬ ЦЕННОГО И ЧЕГО НЕТ В ПРОДЕ. Ответы инструментов задаём МЫ, значит
# знаем истину. Отсюда детерминированная проверка правдивости: если агент назвал
# время, которого инструмент не отдавал, это галлюцинация, и её видно без судьи.
# В проде такую проверку приходится восстанавливать по трейсу задним числом.
import asyncio
import json
import os
import re
import time
from pydantic import BaseModel, Field

_ELIZA = "https://api.eliza.yandex.net/raw/internal/zeliboba/{slug}/v1"

# ── задачи ──────────────────────────────────────────────────────────────────
# slots — то, что «есть в системе». Это и есть истина для проверки галлюцинаций.
TASKS = {
    "booking-simple": {
        "title": "Бронь столика, всё есть",
        "goal": "Забронировать столик на 4 человек в субботу на 19:00 в «Веранде». "
                "Ты не против 19:30, если 19:00 занято. Телефон +7 900 111-22-33.",
        "first": "Привет! Хочу забронировать столик на выходные.",
        "slots": {"Веранда": ["18:00", "19:30", "21:00"]},
        "expect": "Агент должен предложить 19:30 (19:00 нет) и довести бронь до конца.",
    },
    "booking-nothing": {
        "title": "Мест нет — нужен честный отказ",
        "goal": "Забронировать столик на 10 человек сегодня на 20:00. "
                "Другое время тебе НЕ подходит, только сегодня 20:00.",
        "first": "Нужен столик на десятерых сегодня в восемь вечера.",
        "slots": {"Веранда": [], "Гнездо": []},
        "expect": "Свободных мест нет. Агент обязан честно отказать, а не выдумать бронь.",
    },
    "booking-vague": {
        "title": "Пользователь недоговаривает",
        "goal": "Тебе нужен столик на 2 человек в пятницу вечером, но ты называешь детали "
                "только если спросят. Аллергия на орехи — скажи, только если спросят про пожелания.",
        "first": "Хочу посидеть в пятницу.",
        "slots": {"Веранда": ["18:30", "20:00"]},
        "expect": "Агент должен задать уточняющие вопросы, а не бронировать наугад.",
    },
    "support-refund": {
        "title": "Поддержка: возврат за отменённый заказ",
        "goal": "Тебе не привезли заказ №4471, хочешь вернуть деньги. Ты раздражён, "
                "но вежлив. Согласен на возврат на карту.",
        "first": "Заказ 4471 так и не приехал. Что делать?",
        "slots": {"4471": ["отменён курьером", "оплачен картой", "возврат доступен"]},
        "expect": "Агент должен проверить заказ инструментом и оформить возврат.",
    },
}

AXES = ["task", "truth", "product", "efficiency"]


def _token() -> str:
    return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]


def _clean(t: str) -> str:
    t = re.sub(r"<think>.*?</think>", "", t or "", flags=re.S)
    if "</think>" in t:
        t = t.split("</think>")[-1]
    return t.strip()


# Мета-болтовня вместо реплики: «The user is roleplaying…», «My goal is…», «Wait,».
_META = re.compile(r"^(the (user|agent|assistant)\b|i \(|my goal\b|wait,|let'?s check|"
                   r"regardless,|so if|now the\b|system:|user \(|model\b)", re.I)


def _sim_line(text: str) -> str:
    """
    Вытащить РЕПЛИКУ из ответа юзер-сима.

    Без этого в диалог попадал внутренний монолог модели на английском вместо фразы
    пользователя (поймано на первом же прогоне). Берём последнюю строку, которая
    похожа на живую реплику: не мета-рассуждение и содержит кириллицу.
    """
    t = _clean(text)
    lines = [l.strip().strip('"«»') for l in t.splitlines() if l.strip()]
    good = [l for l in lines
            if not _META.match(l) and re.search(r"[а-яё]", l, re.I) and len(l) < 300]
    if good:
        return good[-1]
    return lines[-1] if lines else "(пусто)"


class Pipe:
    class Valves(BaseModel):
        AGENT_SLUG: str = Field(default="qwen35-v7-gate", description="кто играет агента")
        # 🚨 Юзер-сим и судья — НЕ базовая модель. Замер: база выдаёт черновик рассуждений
        # в 7 случаях из 8, v7 — в 0 из 8. На первом же прогоне сим вместо реплики выдал
        # английское «The user is roleplaying as…», и диалог развалился.
        USER_SLUG: str = Field(default="qwen35-v7-gate", description="кто играет пользователя")
        JUDGE_SLUG: str = Field(default="qwen35-v7-gate", description="кто судит")
        MAX_TURNS: int = Field(default=6, description="максимум ходов агента")
        TASK: str = Field(default="booking-simple",
                          description="задача по умолчанию: " + ", ".join(TASKS))

    def __init__(self):
        self.valves = self.Valves()

    # ── низкоуровневый вызов модели ────────────────────────────────────────
    async def _ask(self, cx, slug, messages, tools=None, temp=0.3, max_tokens=700):
        body = {"model": slug, "messages": messages, "temperature": temp,
                "max_tokens": max_tokens, "stream": False}
        if tools:
            body["tools"] = tools
        r = await cx.post(_ELIZA.format(slug=slug) + "/chat/completions", json=body,
                          headers={"Authorization": f"Bearer {_token()}",
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            return {"err": f"HTTP {r.status_code}", "slug": slug}
        ch = r.json()["choices"][0]["message"]
        return {"text": _clean(ch.get("content") or ""), "tool_calls": ch.get("tool_calls") or []}

    # ── инструменты среды: отвечают фикстурами, поэтому истина известна ────
    def _tool_spec(self, task):
        if task.get("slots") and "4471" in task["slots"]:
            return [{"type": "function", "function": {
                "name": "get_order", "description": "Посмотреть статус заказа по номеру",
                "parameters": {"type": "object", "properties": {
                    "order_id": {"type": "string", "description": "номер заказа"}},
                    "required": ["order_id"]}}},
                {"type": "function", "function": {
                    "name": "make_refund", "description": "Оформить возврат по заказу",
                    "parameters": {"type": "object", "properties": {
                        "order_id": {"type": "string", "description": "номер заказа"}},
                        "required": ["order_id"]}}}]
        return [{"type": "function", "function": {
            "name": "search_slots",
            "description": "Найти свободные слоты для брони в заведении на дату",
            "parameters": {"type": "object", "properties": {
                "place": {"type": "string", "description": "название заведения"},
                "date": {"type": "string", "description": "дата, например «суббота»"}},
                "required": ["place"]}}},
            {"type": "function", "function": {
                "name": "book_slot", "description": "Забронировать конкретный слот",
                "parameters": {"type": "object", "properties": {
                    "place": {"type": "string", "description": "заведение"},
                    "time": {"type": "string", "description": "время слота"},
                    "guests": {"type": "integer", "description": "число гостей"}},
                    "required": ["place", "time"]}}}]

    def _run_tool(self, task, name, args, state):
        slots = task.get("slots") or {}
        if name == "search_slots":
            place = args.get("place") or next(iter(slots), "Веранда")
            found = slots.get(place, slots.get(next(iter(slots), ""), []))
            state["offered"].update(found)
            return {"place": place, "free": found} if found else {"place": place, "free": [],
                                                                  "note": "свободных слотов нет"}
        if name == "book_slot":
            t = str(args.get("time") or "")
            place = args.get("place") or next(iter(slots), "")
            ok = t in (slots.get(place) or [])
            if ok:
                state["booked"] = {"place": place, "time": t, "guests": args.get("guests")}
            return {"booked": ok, "time": t,
                    "error": None if ok else "такого свободного слота нет"}
        if name == "get_order":
            oid = str(args.get("order_id") or "")
            info = slots.get(oid)
            return {"order": oid, "status": info} if info else {"order": oid, "error": "не найден"}
        if name == "make_refund":
            state["booked"] = {"refund": args.get("order_id")}
            return {"refund": "оформлен", "order": args.get("order_id")}
        return {"error": f"нет такого инструмента: {name}"}

    # ── детерминированная проверка правдивости ─────────────────────────────
    @staticmethod
    def _truth_check(task, transcript, state):
        """
        Назвал ли агент время, которого среда не предлагала.

        Это и есть преимущество песочницы: ответы инструментов задаём мы, поэтому
        галлюцинацию видно арифметикой, без мнения судьи.
        """
        real = set()
        for v in (task.get("slots") or {}).values():
            if isinstance(v, list):
                real.update(x for x in v if re.match(r"^\d{1,2}:\d{2}$", str(x)))
        said = set()
        for role, text in transcript:
            if role == "агент":
                said.update(re.findall(r"\b\d{1,2}:\d{2}\b", text))
        invented = sorted(said - real)
        return invented, sorted(real)

    async def _judge(self, cx, task, transcript, state, invented):
        dialog = "\n".join(f"{r}: {t}" for r, t in transcript)
        booked = json.dumps(state.get("booked"), ensure_ascii=False)
        prompt = (
            "Ты строгий судья диалогов сервисного агента. Оцени работу АГЕНТА.\n\n"
            f"Скрытая цель пользователя: {task['goal']}\n"
            f"Что на самом деле было доступно в системе: "
            f"{json.dumps(task.get('slots'), ensure_ascii=False)}\n"
            f"Что агент в итоге оформил: {booked}\n"
            f"Ожидание по задаче: {task['expect']}\n"
            + (f"Детектор галлюцинаций: агент называл время, которого не было: {invented}\n"
               if invented else "Детектор галлюцинаций: выдуманного времени не найдено\n")
            + f"\nДиалог:\n{dialog}\n\n"
            "Оцени по осям от 0 до 1 с шагом 0.25:\n"
            "- task: достигнута ли цель пользователя\n"
            "- truth: не выдумывал ли агент факты, которых не было в ответах инструментов\n"
            "- product: качество общения, уместные уточнения, отсутствие лишней болтовни\n"
            "- efficiency: уложился ли без лишних ходов\n\n"
            "Ответь СТРОГО одним JSON без пояснений вокруг: "
            '{"task":0.0,"truth":0.0,"product":0.0,"efficiency":0.0,"verdict":"одна фраза",'
            '"failure":"главная ошибка или null"}')
        r = await self._ask(cx, self.valves.JUDGE_SLUG,
                            [{"role": "user", "content": prompt}], temp=0.0, max_tokens=500)
        raw = r.get("text") or ""
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None, raw[:300]
        try:
            return json.loads(m.group(0)), None
        except Exception as e:
            return None, f"{type(e).__name__}: {raw[:200]}"

    async def pipe(self, body: dict, __event_emitter__=None):
        import httpx

        # Задачу выбирает пользователь текстом: «booking-nothing» или просто её номер.
        req = ""
        for m in reversed(body.get("messages") or []):
            if m.get("role") == "user":
                req = (m.get("content") or "").strip().lower()
                break
        task_key = self.valves.TASK
        for k in TASKS:
            if k in req:
                task_key = k
                break
        if req in ("список", "list", "?", "задачи"):
            return ("Доступные задачи песочницы:\n\n" + "\n".join(
                f"- `{k}` — {v['title']}" for k, v in TASKS.items()
            ) + "\n\nНапиши имя задачи, чтобы прогнать её.")
        task = TASKS[task_key]

        async def st(d, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status",
                                         "data": {"description": d, "done": done}})

        head = (f"## Песочница: {task['title']}\n\n"
                f"Агент — `{self.valves.AGENT_SLUG}`, пользователь — `{self.valves.USER_SLUG}`, "
                f"судья — `{self.valves.JUDGE_SLUG}`.\n\n"
                f"> Скрытая цель пользователя (агент её НЕ видит): {task['goal']}\n\n"
                f"---\n\n")
        parts = [head]
        transcript, state = [], {"offered": set(), "booked": None}
        t0 = time.time()

        agent_sys = ("Ты — сервисный агент. Помоги пользователю, пользуясь ИНСТРУМЕНТАМИ. "
                     "Никогда не выдумывай доступное время или статусы: если инструмент их не "
                     "вернул, значит их нет. Отвечай коротко, по-русски, без рассуждений вслух.")
        user_sys = ("Ты играешь ПОЛЬЗОВАТЕЛЯ в диалоге с сервисным агентом. Твоя скрытая цель: "
                    f"{task['goal']}\nОтвечай коротко и естественно, как живой человек, по-русски. "
                    "Не пересказывай цель целиком — выдавай детали по мере вопросов. Если агент "
                    "сделал то, что нужно, скажи спасибо и закончи словом ГОТОВО.")

        agent_msgs = [{"role": "system", "content": agent_sys},
                      {"role": "user", "content": task["first"]}]
        transcript.append(("пользователь", task["first"]))
        parts.append(f"**👤 пользователь:** {task['first']}\n\n")

        async with httpx.AsyncClient(timeout=300, verify=False) as cx:
            for turn in range(self.valves.MAX_TURNS):
                await st(f"ход {turn + 1}: агент думает")
                a = await self._ask(cx, self.valves.AGENT_SLUG, agent_msgs,
                                    tools=self._tool_spec(task))
                if a.get("err"):
                    parts.append(f"\n⚠ агент недоступен: {a['err']}\n")
                    break

                # инструменты
                for tc in (a.get("tool_calls") or [])[:3]:
                    fn = (tc.get("function") or {})
                    name = fn.get("name") or ""
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    res = self._run_tool(task, name, args, state)
                    parts.append(f"**🔧 {name}**(`{json.dumps(args, ensure_ascii=False)}`) → "
                                 f"`{json.dumps(res, ensure_ascii=False)}`\n\n")
                    agent_msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    agent_msgs.append({"role": "tool", "tool_call_id": tc.get("id", "x"),
                                       "content": json.dumps(res, ensure_ascii=False)})
                if a.get("tool_calls"):
                    a = await self._ask(cx, self.valves.AGENT_SLUG, agent_msgs,
                                        tools=self._tool_spec(task))

                reply = a.get("text") or "(пусто)"
                prev_agent = [t for r, t in transcript if r == "агент"]
                if prev_agent and reply.strip() == prev_agent[-1].strip():
                    # Агент повторил себя дословно — дальше диалог не двинется,
                    # и каждый лишний ход только жжёт GPU. Останавливаемся честно.
                    parts.append("**🔁 агент повторил тот же ответ — прогон остановлен.**\n\n")
                    transcript.append(("агент", reply))
                    break
                transcript.append(("агент", reply))
                agent_msgs.append({"role": "assistant", "content": reply})
                parts.append(f"**🤖 агент:** {reply}\n\n")

                # ход пользователя
                await st(f"ход {turn + 1}: отвечает пользователь")
                sim_msgs = [{"role": "system", "content": user_sys}]
                for role, text in transcript:
                    sim_msgs.append({"role": "assistant" if role == "пользователь" else "user",
                                     "content": text})
                u = await self._ask(cx, self.valves.USER_SLUG, sim_msgs, temp=0.6, max_tokens=200)
                if u.get("err"):
                    parts.append(f"\n⚠ юзер-сим недоступен: {u['err']}\n")
                    break
                utext = _sim_line(u.get("text") or "")
                transcript.append(("пользователь", utext))
                parts.append(f"**👤 пользователь:** {utext}\n\n")
                if "ГОТОВО" in utext.upper():
                    break
                agent_msgs.append({"role": "user", "content": utext})

            # оценка
            await st("судья оценивает")
            invented, real = self._truth_check(task, transcript, state)
            verdict, err = await self._judge(cx, task, transcript, state, invented)

        dt = int(time.time() - t0)
        parts.append("---\n\n### Итог\n\n")
        parts.append(f"Оформлено: `{json.dumps(state.get('booked'), ensure_ascii=False)}`\n\n")
        if invented:
            parts.append(f"🔴 **Проверка правдивости:** агент называл время, которого среда не "
                         f"предлагала: `{invented}` (реально было `{real}`). Это детерминированная "
                         f"проверка по фикстурам, не мнение судьи.\n\n")
        else:
            parts.append("🟢 **Проверка правдивости:** выдуманного времени не найдено.\n\n")
        if verdict:
            parts.append("| ось | оценка |\n|---|---|\n")
            for ax in AXES:
                parts.append(f"| {ax} | {verdict.get(ax)} |\n")
            parts.append(f"\n**Вердикт судьи:** {verdict.get('verdict','')}\n\n")
            if verdict.get("failure"):
                parts.append(f"**Главная ошибка:** {verdict['failure']}\n\n")
        else:
            parts.append(f"Судья не отдал разбираемый JSON: {err}\n\n")
        parts.append(f"*задача={task_key} · ходов={len(transcript)} · {dt}s · "
                     f"агент={self.valves.AGENT_SLUG}*\n\n")
        parts.append("> Это приближение прод-контура: инструменты отвечают фикстурами, "
                     "а не боевым бэкендом. Годится для отладки поведения, не для отчётных цифр.")
        await st("готово", True)
        return "".join(parts)
