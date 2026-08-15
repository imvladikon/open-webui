"""
title: Run Metadata
author: agents-team
version: 0.1.0
description: Фильтр: показывает латентность, число тул-итераций и какой слаг ответил, чистит протечку reasoning, и штампует в сообщение run/step/params/seed - без этого собранные оценки непригодны как датасет.
required_open_webui_version: 0.11.0
"""
# Три задачи фильтра:
#  1. НАБЛЮДАЕМОСТЬ: сколько шло, сколько тул-итераций, какой бэкенд реально ответил.
#  2. ГИГИЕНА: снять хвосты reasoning (<think>, висячий </think>), которые текут в content,
#     потому что в серве нет --reasoning-parser (ставить его рискованно: прячет content).
#  3. ПРОВЕНАНС: записать в message.meta слаг/params/seed. Красный флаг из ревью: фидбек
#     привязан к слагу OWUI, а не к чекпойнту, и после переливки слага все прошлые оценки
#     молча меняют смысл. Штамп делает выгрузку feedback пригодной как датасет.
#
# Порядок фильтров задаётся self.valves.priority (utils/filter.py:97).
# outlet может переписать content, и это персистится (middleware.py:3528).
import json
import re
import sqlite3
import time
from pydantic import BaseModel, Field

DB = "/app/backend/data/webui.db"
_PARAMS_CACHE: dict = {}


def _model_params(model_id: str) -> dict:
    """
    Параметры workspace-модели (seed/temperature/...).
    🚨 В dict, который приходит фильтру как __model__, поля `params` НЕТ: `info` несёт
    base_model_id и meta, но не params (то же самое видно в ответе /api/models). Поэтому
    читаем их из таблицы model напрямую, с кэшем в памяти процесса.
    """
    if not model_id:
        return {}
    if model_id in _PARAMS_CACHE:
        return _PARAMS_CACHE[model_id]
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
        row = con.execute("select params from model where id=?", (model_id,)).fetchone()
        con.close()
        params = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        params = {}
    _PARAMS_CACHE[model_id] = params
    return params

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
STRAY_OPEN = re.compile(r"<think>.*$", re.S)


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=10, description="порядок среди фильтров")
        show_footer: bool = Field(default=True, description="дописывать строку метаданных в ответ")
        clean_reasoning: bool = Field(default=True, description="чистить протечку <think>")
        stamp_provenance: bool = Field(default=True, description="штамповать слаг/params в meta")

    def __init__(self):
        self.valves = self.Valves()
        # 🚨 НЕ ставить self.toggle = True: в resolve_filter_pipeline (utils/filter.py)
        # ветка `if getattr(function_module, 'toggle', None): return filter_id in
        # enabled_filter_ids` превращает фильтр в РУЧНУЮ кнопку. Глобальный фильтр
        # с toggle НЕ применяется, пока пользователь не нажмёт её в конкретном чате.

    async def inlet(self, body: dict, __metadata__: dict = None, __model__: dict = None) -> dict:
        if __metadata__ is not None:
            __metadata__["_lab_t0"] = time.time()
        return body

    async def outlet(self, body: dict, __metadata__: dict = None, __model__: dict = None,
                     __event_emitter__=None, __user__: dict = None) -> dict:
        msgs = body.get("messages") or []
        if not msgs:
            return body
        msg = msgs[-1]

        t0 = (__metadata__ or {}).get("_lab_t0")
        dt_ms = int((time.time() - t0) * 1000) if t0 else None

        # сколько раз модель вызывала инструменты в этом ответе
        out_items = msg.get("output") or []
        tool_iters = sum(1 for i in out_items if i.get("type") == "function_call")

        # какой бэкенд реально отвечал: workspace-модель -> base_model_id (это наш eliza-слаг)
        model_id = (__model__ or {}).get("id")
        info = (__model__ or {}).get("info") or {}
        slug = info.get("base_model_id") or model_id
        params = info.get("params") or _model_params(model_id)

        # 1. чистка протечки reasoning
        content = msg.get("content") or ""
        if self.valves.clean_reasoning and content:
            cleaned = THINK_BLOCK.sub("", content)
            if "</think>" in cleaned:              # висячий закрывающий тег без открывающего
                cleaned = cleaned.split("</think>")[-1]
            cleaned = STRAY_OPEN.sub("", cleaned)  # открытый think без закрытия
            cleaned = cleaned.strip()
            if cleaned and cleaned != content:
                msg["content"] = cleaned
                content = cleaned

        # 2. провенанс.
        # 🚨 ПРОВЕРЕНО: из outlet персистится ТОЛЬКО `content`. Правки `msg["meta"]` и
        # `msg["usage"]` в БД не доезжают (остаются null/пустыми), сколько бы их ни писать.
        # Поэтому провенанс несёт сама строка футера: она попадает в content, а значит и в
        # `snapshot.chat` при выгрузке feedback. Формат держим машиночитаемым (k=v через ·),
        # чтобы скрипт сборки датасета мог его распарсить регуляркой.
        # Более чистый путь на будущее - Event-функция на `chat.finished` (events.py), она
        # получает chat_id/message_id/model_id и может писать провенанс в свой стор.

        # 3. видимая и машиночитаемая строка метаданных (она же носитель провенанса)
        if self.valves.show_footer and content:
            bits = [f"slug={slug}"]
            if dt_ms is not None:
                bits.append(f"ms={dt_ms}")
            bits.append(f"tools={tool_iters}")
            if params.get("temperature") is not None:
                bits.append(f"t={params['temperature']}")
            if params.get("seed") is not None:
                bits.append(f"seed={params['seed']}")
            if model_id and model_id != slug:
                bits.append(f"preset={model_id}")
            footer = "\n\n<sub>" + " · ".join(bits) + "</sub>"
            if "<sub>slug=" not in content[-300:]:   # не дублировать при повторном outlet
                msg["content"] = content + footer

        if __event_emitter__ and dt_ms is not None:
            await __event_emitter__({"type": "status", "data": {
                "description": f"{dt_ms} ms · {tool_iters} tool-iter · {slug}",
                "done": True, "hidden": False}})
        return body
