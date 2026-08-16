"""
title: Export
author: agents-team
version: 0.1.0
description: Кнопки под ответом: выгрузить диалог как markdown или JSONL. Markdown — в тикет и отчёт, JSONL — в датасет (формат OpenAI messages). Файл создаётся сервером, модель ничего не копирует.
"""
# Механика Actions: список `actions` даёт несколько кнопок из одного модуля
# (id вида "<function_id>.<sub_id>", utils/models.py). Файл пишем через Files API, как во всех
# остальных наших фичах: правило «модель не копирует большой payload» (см. lab/README).
import io
import json
import re
import time

actions = [
    {"id": "md", "name": "Экспорт: диалог в Markdown", "icon": "📄"},
    {"id": "jsonl", "name": "Экспорт: диалог в JSONL (датасет)", "icon": "🗃"},
]
PROV = re.compile(r"\n*<sub>slug=[^<]*</sub>")     # футер run_metadata в выгрузку не тащим
THINK = re.compile(r"<think>.*?</think>", re.S)    # протёкшее рассуждение в датасет не тащим


def _clean(t: str) -> str:
    t = THINK.sub("", t or "")
    if "</think>" in t:                            # висячий закрывающий без открытия
        t = t.split("</think>")[-1]
    return PROV.sub("", t).strip()


def _text(msg: dict) -> str:
    if msg.get("content"):
        return _clean(msg["content"])
    parts = []
    for item in msg.get("output") or []:
        t = item.get("type")
        if t == "message":
            for c in item.get("content") or []:
                parts.append(c.get("text") or "")
        elif t == "function_call":
            parts.append(f"[tool call] {item.get('name')}({item.get('arguments')})")
        elif t == "function_call_output":
            out = (item.get("output") or [{}])[0].get("text", "")
            parts.append(f"[tool result] {out[:2000]}")
    return _clean("\n".join(p for p in parts if p))


class Action:
    def __init__(self):
        pass

    async def action(self, body: dict, __id__: str = "", __request__=None, __user__=None,
                     __event_emitter__=None, __chat_id__=None) -> None:
        sub = (__id__ or "").rsplit(".", 1)[-1]
        msgs = body.get("messages") or []
        if not msgs:
            await self._say(__event_emitter__, "Пустой диалог, нечего выгружать.")
            return

        stamp = time.strftime("%Y%m%d-%H%M")
        if sub == "jsonl":
            # формат для датасета: одна строка = один диалог в OpenAI-виде
            payload = json.dumps({"messages": [
                {"role": m.get("role"), "content": _text(m)} for m in msgs if _text(m)
            ]}, ensure_ascii=False) + "\n"
            fname, ctype = f"chat-{stamp}.jsonl", "application/x-ndjson"
        else:
            lines = [f"# Диалог {stamp}", ""]
            for m in msgs:
                t = _text(m)
                if not t:
                    continue
                who = {"user": "Пользователь", "assistant": "Ассистент",
                       "system": "Система"}.get(m.get("role"), m.get("role"))
                model = m.get("model") or m.get("modelName")
                head = f"## {who}" + (f" · `{model}`" if model and m.get("role") == "assistant" else "")
                lines += [head, "", t, ""]
            payload = "\n".join(lines)
            fname, ctype = f"chat-{stamp}.md", "text/markdown"

        link = await self._save(fname, payload, ctype, __request__, __user__,
                                __chat_id__, __event_emitter__)
        if link:
            await self._say(__event_emitter__,
                            f"**Выгружено:** [{fname}]({link}) · {len(payload.encode())} Б, "
                            f"{len([m for m in msgs if _text(m)])} сообщений")
        else:
            await self._say(__event_emitter__,
                            "Не удалось создать файл (нет request/user). "
                            "Экспорт работает только из UI.")

    async def _save(self, fname, text, ctype, __request__, __user__, __chat_id__, emitter):
        if not __request__ or not __user__:
            return None
        try:
            from fastapi import UploadFile
            from open_webui.models.users import Users
            from open_webui.models.files import Files
            from open_webui.routers.files import upload_file_handler
            user = await Users.get_user_by_id(__user__["id"])
            f = UploadFile(file=io.BytesIO(text.encode("utf-8")), filename=fname,
                           headers={"content-type": ctype})
            it = await upload_file_handler(__request__, file=f,
                                           metadata={"chat_id": __chat_id__, "lab_export": True},
                                           process=False, process_in_background=False, user=user)
            await Files.update_file_data_by_id(it.id, {"content": text[:20000]})
            if emitter:
                await emitter({"type": "files", "data": {"files": [
                    {"type": "file", "id": it.id, "name": fname,
                     "size": len(text.encode()), "url": f"/api/v1/files/{it.id}"}]}})
            return f"/api/v1/files/{it.id}/content/{fname}"
        except Exception:
            return None

    async def _say(self, emitter, text: str):
        if emitter:
            await emitter({"type": "message", "data": {"content": "\n\n" + text + "\n"}})
