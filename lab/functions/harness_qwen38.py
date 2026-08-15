"""
title: Harness · Qwen 3.8
author: agents-team
version: 0.1.0
description: Пример СВОЕГО агент-харнесса (ReAct-цикл) поверх eliza-слага qwen38-27b-gate. Свой луп + свои встроенные тулзы, executed внутри pipe. Замени тело _run на своего агента.
requirements: httpx
"""
# Демонстрация "протащить своего агента в Open WebUI": выбираешь эту "модель" в
# селекторе -> внутри крутится наш ReAct-луп, который сам ходит в qwen38-27b-gate,
# сам исполняет свои тулзы и возвращает финал. НЕ используем OWUI-Tools -> нет
# двойного исполнения (тул-луп целиком наш).
import os
import re
import json
import ast
import operator
from pydantic import BaseModel, Field

ELIZA_BASE = "https://api.eliza.yandex.net/raw/internal/zeliboba/qwen38-27b-gate/v1"

# ---- свои тулзы (исполняются ВНУТРИ харнесса) ------------------------------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg,
}

def _safe_eval(node):
    if isinstance(node, ast.Num):
        return node.n
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")

def tool_calculator(expression: str) -> str:
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:
        return f"calc error: {e}"

def tool_current_time() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")

TOOLS_IMPL = {"calculator": tool_calculator, "current_time": tool_current_time}
TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Вычислить арифметическое выражение, напр. '17*23+4'.",
        "parameters": {"type": "object",
                       "properties": {"expression": {"type": "string"}},
                       "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "current_time",
        "description": "Текущие дата и время сервера.",
        "parameters": {"type": "object", "properties": {}}}},
]

_THINK = re.compile(r"<think>.*?</think>", re.S)


def _clean(text: str) -> str:
    text = text or ""
    text = _THINK.sub("", text)
    # модель иногда отдаёт висячий </think> без открывающего -> режем всё до него
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


class Pipe:
    class Valves(BaseModel):
        ELIZA_TOKEN: str = Field(default="", description="Bearer для eliza; пусто = взять из OPENAI_API_KEYS")
        MODEL: str = Field(default="qwen38-27b-gate", description="имя модели у бэкенда (echo)")
        MAX_STEPS: int = Field(default=6, description="потолок шагов ReAct")

    def __init__(self):
        self.valves = self.Valves()

    def _token(self) -> str:
        if self.valves.ELIZA_TOKEN:
            return self.valves.ELIZA_TOKEN
        return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]

    # без self.pipes -> одиночная модель в селекторе (id = id функции)
    async def pipe(self, body: dict, __event_emitter__=None, __user__: dict | None = None):
        import httpx
        headers = {"Authorization": f"Bearer {self._token()}",
                   "Content-Type": "application/json"}
        messages = list(body.get("messages", []))

        async def emit(desc, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status",
                                         "data": {"description": desc, "done": done}})

        async with httpx.AsyncClient(timeout=180, verify=False) as cx:
            for step in range(self.valves.MAX_STEPS):
                await emit(f"harness: шаг {step + 1}/{self.valves.MAX_STEPS}")
                payload = {"model": self.valves.MODEL, "messages": messages,
                           "tools": TOOLS_SPEC, "stream": False, "temperature": 0}
                r = await cx.post(f"{ELIZA_BASE}/chat/completions", json=payload, headers=headers)
                if r.status_code != 200:
                    await emit(f"backend {r.status_code}", done=True)
                    return f"harness: бэкенд вернул {r.status_code}: {r.text[:300]}"
                msg = r.json()["choices"][0]["message"]
                messages.append(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    await emit("harness: готово", done=True)
                    return _clean(msg.get("content"))
                for c in calls:
                    name = c["function"]["name"]
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except Exception:
                        args = {}
                    await emit(f"tool: {name}({args})")
                    impl = TOOLS_IMPL.get(name)
                    result = impl(**args) if impl else f"no such tool: {name}"
                    messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                     "name": name, "content": str(result)})
            await emit("harness: лимит шагов", done=True)
            return _clean(messages[-1].get("content")) or "harness: достигнут лимит шагов"
