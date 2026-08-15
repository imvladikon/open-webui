"""
title: Inspect
author: agents-team
version: 0.1.0
description: Кнопки под ответом: raw (исходный текст без рендера), expand (продолжить обрезанный ответ), provenance (чем и с какими параметрами сгенерировано), stats (объём, код, обрезка).
"""
# Механика Actions (utils/actions.py, роут POST /api/chat/actions/{id}):
#   - модуль с функцией `action(body, ...)`;
#   - список `actions = [{id, name, icon}]` даёт НЕСКОЛЬКО кнопок из одного файла,
#     их id становятся "<function_id>.<sub_id>" (utils/models.py);
#   - спец-параметры прокидываются ТОЛЬКО те, что объявлены в сигнатуре;
#   - привязка к модели через model.meta.actionIds либо is_global.
# Показываем результат через __event_emitter__ type="message": для КНОПКИ это работает
# (в отличие от тулзы), потому что после действия нет генерации, которая перезапишет content.
import json
import os
import re

actions = [
    {"id": "raw", "name": "Raw: исходный текст", "icon": "🧾"},
    {"id": "expand", "name": "Expand: продолжить ответ", "icon": "⏩"},
    {"id": "provenance", "name": "Provenance: чем сгенерено", "icon": "🔎"},
    {"id": "stats", "name": "Stats: объём и код", "icon": "📐"},
]

PROV = re.compile(r"<sub>(slug=[^<]+)</sub>")
FENCE = re.compile(r"```(\w+)?\n(.*?)```", re.S)


def _last_assistant(body: dict) -> dict:
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "assistant":
            return m
    return {}


def _text(msg: dict) -> str:
    if msg.get("content"):
        return msg["content"]
    parts = []
    for item in msg.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                parts.append(c.get("text") or "")
    return "\n".join(parts)


class Action:
    def __init__(self):
        pass

    async def action(self, body: dict, __id__: str = "", __event_emitter__=None,
                     __model__: dict = None, __user__: dict = None) -> None:
        sub = (__id__ or "").rsplit(".", 1)[-1]
        msg = _last_assistant(body)
        text = _text(msg)
        if not text:
            await self._say(__event_emitter__, "Нечего показывать: пустой ответ.")
            return

        if sub == "raw":
            # исходник как есть: видно markdown, footer, невидимые артефакты разметки
            body_txt = text.replace("`", "ˋ")     # не даём фенсам сломать вывод
            await self._say(__event_emitter__,
                            f"**Raw ({len(text)} символов)**\n\n```text\n{body_txt[:6000]}\n```"
                            + ("\n\n_показаны первые 6000 символов_" if len(text) > 6000 else ""))

        elif sub == "expand":
            trunc = text.count("```") % 2 == 1 or not text.rstrip().endswith((".", "!", "?", "`", ")", ":", "»", '"'))
            hint = ("Ответ выглядит обрезанным. " if trunc else "Ответ выглядит целым. ")
            await self._say(__event_emitter__, hint +
                            "Чтобы дописать, отправь следующим сообщением: "
                            "**«продолжи с места обрыва, не повторяя уже написанное»**. "
                            "Если обрывается регулярно, подними `max_tokens` у пресета "
                            "(`lab/scripts/registry.example.json`, сейчас в дефолте 16384).")

        elif sub == "provenance":
            m = PROV.search(text)
            info = m.group(1) if m else None
            model_id = (__model__ or {}).get("id")
            base = ((__model__ or {}).get("info") or {}).get("base_model_id")
            lines = [f"- модель в чате: `{model_id}`", f"- бэкенд-слаг: `{base or model_id}`"]
            if info:
                lines.append("- провенанс из футера: `" + info.strip() + "`")
            else:
                lines.append("- футера нет: включи глобальный фильтр `run_metadata`, "
                             "иначе оценка этого ответа не будет привязана к чекпойнту")
            await self._say(__event_emitter__, "**Provenance**\n" + "\n".join(lines))

        elif sub == "stats":
            blocks = FENCE.findall(text)
            langs = {(l or "text") for l, _ in blocks}
            code_chars = sum(len(c) for _, c in blocks)
            trunc = text.count("```") % 2 == 1 or not text.rstrip().endswith((".", "!", "?", "`", ")", ":", "»", '"'))
            await self._say(__event_emitter__,
                            "**Stats**\n"
                            f"- символов: {len(text)} (в коде {code_chars}, "
                            f"{round(100 * code_chars / max(len(text), 1))}%)\n"
                            f"- блоков кода: {len(blocks)} ({', '.join(sorted(langs)) or '-'})\n"
                            f"- строк: {text.count(chr(10)) + 1}\n"
                            f"- обрезан: {'ДА, см. кнопку Expand' if trunc else 'нет'}")

    async def _say(self, emitter, text: str):
        if emitter:
            await emitter({"type": "message", "data": {"content": "\n\n" + text + "\n"}})
