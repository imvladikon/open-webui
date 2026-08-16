"""
title: Collapse Reasoning
author: agents-team
version: 0.1.0
description: Прячет ход мысли модели под спойлер и показывает сразу ответ. Работает даже когда рассуждение идёт БЕЗ тегов <think> — а именно так ведут себя наши сервы (reasoning_content=null, всё валится в content).
"""
# ПРОБЛЕМА (реальный баг с демо). Пользователь видел 42 КБ черновика про SVG-координаты вместо
# ответа. Разбор: наш серв запущен БЕЗ --reasoning-parser, поэтому `reasoning_content` = null,
# а рассуждение идёт в `content` СПЛОШНЫМ ТЕКСТОМ БЕЗ тегов <think>. Значит все прежние фиксы
# (срезать <think>...</think>) на этом не срабатывают: резать нечего.
#
# Серверный фикс (--reasoning-parser) у нас уже пробовали, и он ПРЯЧЕТ ответ: content становится
# пустым, бенчи видят пустоту (предупреждение в нашем же serve-скрипте). Поэтому чиним на уровне
# UI: эвристически находим границу «черновик | ответ» и прячем черновик в <details>.
#
# Ничего не удаляем: рассуждение остаётся доступно по клику. Если границу найти не удалось,
# оставляем текст как есть — лучше показать лишнее, чем срезать ответ.
import re
from pydantic import BaseModel, Field

# Маркеры того, что абзац — это размышление, а не ответ пользователю.
_THINK_MARK = re.compile(
    r"^(the user (is )?(ask|want|say)|we need|need to|let me|let's|i should|i need|"
    r"first,? |okay,? |hmm,? |wait,? |thinking process|analyze the request|"
    r"пользователь (просит|спрашивает|хочет)|нужно |надо |давай |хм,? |итак,? |"
    r"сначала |проверим |попробую )", re.I)
# Явная граница: модели часто отбивают финальный ответ этими словами
_ANSWER_MARK = re.compile(
    r"^(итак[,:]|ответ[:.]|финальн|вот (готов|итог|схема|код|результат)|"
    r"final answer|answer:|here('s| is) the)", re.I)
_THINK_TAG = re.compile(r"<think>.*?</think>", re.S)


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=5, description="раньше run_metadata (10), чтобы тот считал уже чистый текст")
        min_chars: int = Field(default=1200, description="ниже этого объёма не трогаем: короткий ответ не черновик")
        min_ratio: float = Field(default=0.45, description="минимальная доля текста, похожая на рассуждение")
        label: str = Field(default="Ход мысли модели", description="подпись спойлера")

    def __init__(self):
        self.valves = self.Valves()

    def _split(self, text: str):
        """Вернуть (черновик, ответ) или (None, text), если границу не нашли."""
        # 1) явные теги, если вдруг есть
        if "</think>" in text:
            head, _, tail = text.rpartition("</think>")
            return _THINK_TAG.sub("", head).replace("<think>", "").strip(), tail.strip()

        paras = [p for p in re.split(r"\n{2,}", text) if p.strip()]
        if len(paras) < 3:
            return None, text

        # 2) явный маркер финального ответа — берём последний такой абзац
        for i in range(len(paras) - 1, 0, -1):
            if _ANSWER_MARK.match(paras[i].strip()):
                return "\n\n".join(paras[:i]).strip(), "\n\n".join(paras[i:]).strip()

        # 3) эвристика: голова состоит из «размышляющих» абзацев, хвост — нет.
        #    Ищем последний размышляющий абзац; всё после него считаем ответом.
        last_think = -1
        for i, p in enumerate(paras):
            if _THINK_MARK.match(p.strip()):
                last_think = i
        if last_think < 0 or last_think >= len(paras) - 1:
            return None, text
        think_share = sum(len(paras[i]) for i in range(last_think + 1)) / max(len(text), 1)
        if think_share < self.valves.min_ratio:
            return None, text
        return "\n\n".join(paras[:last_think + 1]).strip(), "\n\n".join(paras[last_think + 1:]).strip()

    def _all_draft(self, text: str) -> bool:
        """Весь ответ — сплошное размышление: много абзацев и почти все начинаются как черновик."""
        paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        if len(paras) < 4:
            return False
        marked = sum(1 for p in paras if _THINK_MARK.match(p))
        return marked / len(paras) >= 0.35

    async def outlet(self, body: dict, __event_emitter__=None) -> dict:
        msgs = body.get("messages") or []
        if not msgs:
            return body
        msg = msgs[-1]
        text = msg.get("content") or ""
        if len(text) < self.valves.min_chars or "<details" in text:
            return body

        draft, answer = self._split(text)
        if not draft or not answer:
            # Границы нет. Если текст ЦЕЛИКОМ похож на черновик — модель так и не дошла до
            # ответа (обычно упёрлась в max_tokens посреди размышлений). Честно говорим об этом,
            # а не делаем вид, что ответ есть.
            if self._all_draft(text):
                msg["content"] = (
                    "> ⚠ **Модель не дошла до ответа.** Весь вывод — черновик рассуждений "
                    f"({len(text)} символов), скорее всего упёрлась в лимит токенов.\n>\n"
                    "> Что делать: переспросить с явным «сразу дай результат, не рассуждай», "
                    "или взять пресет с системным промптом (папки-проекты), или поднять "
                    "`max_tokens`.\n\n"
                    f"<details>\n<summary>Показать черновик ({len(text)} символов)</summary>\n\n"
                    f"{text}\n\n</details>")
            return body            # не уверены — не трогаем

        pct = round(100 * len(draft) / len(text))
        msg["content"] = (
            f"<details>\n<summary>{self.valves.label} · {len(draft)} символов ({pct}% ответа)"
            f"</summary>\n\n{draft}\n\n</details>\n\n{answer}")
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"черновик свёрнут ({pct}% текста)", "done": True}})
        return body
