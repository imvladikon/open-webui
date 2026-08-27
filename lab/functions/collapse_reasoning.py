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
#
# 🚨 ДВА РЕЖИМА, И ЭТО ВАЖНО. В 0.11 сообщение хранит `output` — список блоков (message,
# reasoning, function_call, ...), и UI рисует ИМЕННО ЕГО, а `content` держит как плоский
# слепок. Поэтому правка одного `content` до экрана НЕ доезжает, как только был вызван
# инструмент (сам на это попался: фильтр «работал», а в чате всё та же простыня).
# Но outlet получает `output` deepcopy-ей и, если его изменить, middleware сохраняет блоки
# и шлёт `chat:outlet` — правка доезжает и в базу, и в живой UI.
#
# Поэтому:
#   * есть `output` → перекладываем черновик в РОДНОЙ блок `reasoning`. Это лучше спойлера:
#     фронт сам рисует «Thought for N seconds» и вырезает блок при копировании ответа.
#     Заодно чинится висящий `</think>` без открывающего тега — наши сервы шлют только
#     закрывающий, а `tag_output_handler` открывает блок строго по стартовому и потому
#     оставляет черновик простым текстом.
#   * нет `output` (обычный чат без тулзов) → старый путь с эвристикой и `<details>`.
import re
import time
import uuid
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

    def _fold(self, draft: str) -> str:
        """
        Свернуть черновик в РОДНОЙ блок рассуждений Open WebUI.

        Ключевое — `type="reasoning"`. У обычного `<details>` вид ровно как у обычного
        текста, поэтому в чате было не отличить, где рассуждение, а где ответ (на это и
        пожаловались). Для `type="reasoning"` фронт даёт отдельный рендер:
        приглушённая строка «Thought for N seconds» со стрелкой (`Collapsible.svelte`),
        а `removeDetails(content, ['reasoning', …])` вырезает блок при копировании
        ответа и при вытаскивании артефактов — то есть ответ отделён и логически.

        Свой `<summary>` фронт для этого типа игнорирует и рисует свой заголовок,
        поэтому длину черновика кладём в него только как фолбэк для не-OWUI рендера.
        """
        return (f'<details type="reasoning" done="true" duration="0">\n'
                f"<summary>{self.valves.label} · {len(draft)} символов</summary>\n\n"
                f"{draft}\n\n</details>")

    # ---------- режим блоков `output` (чаты с инструментами и не только) ----------
    @staticmethod
    def _item_text(item):
        return "".join(p.get("text", "") for p in (item.get("content") or [])
                       if p.get("type") == "output_text")

    @staticmethod
    def _set_item_text(item, text):
        parts = [p for p in (item.get("content") or []) if p.get("type") == "output_text"]
        if parts:
            parts[0]["text"] = text
            item["content"] = [parts[0]]
        else:
            item["content"] = [{"type": "output_text", "text": text}]

    def _reasoning_item(self, text):
        """Собрать блок ровно той формы, что делает сам middleware (см. output_id('r'))."""
        now = time.time()
        return {"type": "reasoning", "id": "r_" + uuid.uuid4().hex[:24],
                "status": "completed", "start_tag": "<think>", "end_tag": "</think>",
                "attributes": {"type": "reasoning_content"},
                "content": [{"type": "output_text", "text": text}],
                "summary": None, "started_at": now, "ended_at": now, "duration": 0}

    def _fix_output(self, output) -> bool:
        """Разложить `message`-блоки с висящим `</think>` на reasoning + ответ."""
        changed = False
        new_items = []
        for item in output:
            if item.get("type") != "message":
                new_items.append(item)
                continue
            text = self._item_text(item)
            if "</think>" not in text:
                new_items.append(item)
                continue
            # Берём ПОСЛЕДНИЙ закрывающий: в цикле с инструментами их бывает несколько.
            head, _, tail = text.rpartition("</think>")
            head = head.replace("<think>", "").strip()
            tail = tail.strip()
            if not head:
                self._set_item_text(item, tail)
                new_items.append(item)
                changed = True
                continue
            new_items.append(self._reasoning_item(head))
            if tail:
                self._set_item_text(item, tail)
                new_items.append(item)
            # Пустой хвост = модель не дошла до ответа. Пустой message-блок не добавляем:
            # пусть в чате будет честный «Thought …» без выдуманного ответа.
            changed = True
        if changed:
            output[:] = new_items
        return changed

    async def outlet(self, body: dict, __event_emitter__=None) -> dict:
        msgs = body.get("messages") or []
        if not msgs:
            return body
        msg = msgs[-1]

        output = msg.get("output")
        if output:
            if self._fix_output(output):
                # content держим согласованным с блоками, иначе копирование ответа
                # и экспорт чата отдадут старый текст вместе с черновиком.
                msg["content"] = "\n\n".join(
                    self._item_text(i) for i in output if i.get("type") == "message").strip()
                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {
                        "description": "рассуждение свёрнуто в блок", "done": True}})
            return body

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
                    + self._fold(text))
            return body            # не уверены — не трогаем

        pct = round(100 * len(draft) / len(text))
        msg["content"] = self._fold(draft) + "\n\n" + answer
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"черновик свёрнут ({pct}% текста)", "done": True}})
        return body
