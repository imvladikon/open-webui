"""
title: Длинный вывод
author: agents-team
version: 0.1.0
description: Кнопка у поля ввода: снимает потолок длины ответа для этого чата. Нужна на больших генерациях — прототип на пол-экрана, длинный разбор, много файлов.
"""
# ЗАЧЕМ. «Auto-continue (unlimited output)» был ОТДЕЛЬНОЙ моделью в списке. Из-за этого
# длинный вывод и выбор модели оказались одним рычагом: включить длинный ответ = уйти с
# нужного чекпойнта. Хуже того, пайп физически не умеет вызывать инструменты, поэтому
# сидя на нём нельзя было получить файл — ровно на этом человек и застрял, прося pptx.
#
# Правильная форма — переключатель: `self.toggle = True` рисует кнопку у поля ввода
# (`resolve_filter_pipeline` в utils/filter.py), и режим включается для ЛЮБОЙ модели,
# не трогая ни диалог, ни инструменты.
#
# Что делаем: поднимаем `max_tokens` в inlet. У наших слагов контекст 262144, так что
# 32k выходных токенов упираются не в модель, а в здравый смысл. Многораундовой
# склейки, как в старом пайпе, здесь нет намеренно: она давала «склеено из N частей»
# со швами посреди кода, а обрыв всё равно виден по предупреждению run_metadata.
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="раньше остальных: правит запрос")
        max_tokens: int = Field(default=32768, description="потолок длины ответа при включённой кнопке")

    def __init__(self):
        self.valves = self.Valves()
        # Кнопка, а не всегда-включённый фильтр: длинный потолок нужен не в каждом чате.
        self.toggle = True
        self.icon = ("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmci"
                     "IGZpbGw9Im5vbmUiIHZpZXdCb3g9IjAgMCAyNCAyNCIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9r"
                     "ZT0iY3VycmVudENvbG9yIj48cGF0aCBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5l"
                     "am9pbj0icm91bmQiIGQ9Ik0zLjc1IDYuNzVoMTYuNU0zLjc1IDEyaDE2LjVtLTE2LjUgNS4yNWgx"
                     "Ni41Ii8+PC9zdmc+")

    async def inlet(self, body: dict, __event_emitter__=None) -> dict:
        cur = body.get("max_tokens") or 0
        if cur < self.valves.max_tokens:
            body["max_tokens"] = self.valves.max_tokens
        # Модель иначе экономит и сама себя обрывает «для краткости».
        msgs = body.get("messages") or []
        note = ("Отвечай так подробно, как требует задача, не сокращай ради краткости. "
                "Если это код — приводи файл целиком, без пропусков вида «...».")
        if msgs and msgs[0].get("role") == "system":
            if note not in (msgs[0].get("content") or ""):
                msgs[0]["content"] = (msgs[0].get("content") or "") + "\n\n" + note
        else:
            body["messages"] = [{"role": "system", "content": note}] + msgs
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"длинный вывод: до {self.valves.max_tokens} токенов",
                "done": True}})
        return body
