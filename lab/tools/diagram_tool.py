"""
title: Diagram Design
author: agents-team
version: 0.1.0
description: Рисование диаграмм прямо в чате. mermaid рендерится инлайн, кастомный SVG уходит в artifact-панель. Стиль из скилла diagram-design (editorial: white-smoke paper, jet-black ink, atomic-tangerine accent).
"""
# Как это работает в Open WebUI 0.11:
#   ```mermaid ...```  -> рендерится инлайн как диаграмма (CodeBlock.svelte:368)
#   ```html ...```     -> уходит в artifact-панель сбоку (utils/index.ts getCodeBlockContents)
# Тулза ЭМИТИТ блок прямо в сообщение через __event_emitter__ type="message",
# поэтому рендер не зависит от того, повторит ли модель код дословно.
import json
import html as _html
from pydantic import BaseModel, Field

# Токены дизайн-системы (references/style-guide.md скилла diagram-design)
T = {
    "paper": "#f5f5f5", "paper2": "#ececec", "ink": "#2d3142",
    "muted": "#4f5d75", "soft": "#7a8399", "rule": "#bfc0c0",
    "accent": "#eb6c36", "accent_tint": "rgba(235,108,54,0.08)", "link": "#2e5aa8",
}

# Тема mermaid в наших токенах: нейтральные узлы, акцент только на focal
_MERMAID_INIT = (
    "%%{init: {'theme':'base','themeVariables':{"
    f"'background':'{T['paper']}','primaryColor':'{T['paper']}',"
    f"'primaryTextColor':'{T['ink']}','primaryBorderColor':'{T['ink']}',"
    f"'lineColor':'{T['muted']}','secondaryColor':'{T['paper2']}',"
    f"'tertiaryColor':'{T['paper2']}','noteBkgColor':'{T['accent_tint']}',"
    f"'noteBorderColor':'{T['accent']}','fontFamily':'Geist, ui-sans-serif, system-ui',"
    "'fontSize':'13px'}}}%%"
)

STYLE_GUIDE = f"""Дизайн-система диаграмм (editorial, не «AI-slop»):
- Палитра: paper {T['paper']}, ink {T['ink']}, muted {T['muted']} (стрелки),
  accent {T['accent']} (ТОЛЬКО 1-2 фокальных узла), link {T['link']} (внешние/HTTP).
- Плотность 4/10: каждый узел = отдельная идея. Больше 9 узлов = это две диаграммы.
- Соединения: только прямоугольные (ортогональные) изгибы, никаких диагоналей.
- Подпись стрелки не лежит на линии: зазор 6-10px, под подписью непрозрачная плашка.
- Никаких теней, никаких скруглений больше 6-10px, никаких эмодзи в узлах.
- Моноширинный шрифт только для технического (порты, URL), имена узлов обычным.
- Если удаление элемента ничего не ломает, удали его."""


class Tools:
    class Valves(BaseModel):
        THEME: bool = Field(default=True, description="Подмешивать нашу тему в mermaid")
        MAX_NODES_HINT: int = Field(default=9, description="Мягкий потолок узлов")

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, block: str, note: str):
        if emitter:
            await emitter({"type": "message", "data": {"content": "\n\n" + block + "\n\n"}})
            await emitter({"type": "status", "data": {"description": note, "done": True}})

    async def draw_diagram(self, mermaid_code: str, title: str = "",
                           __event_emitter__=None) -> str:
        """
        Нарисовать диаграмму в чате из кода mermaid. Использовать для схем архитектуры, флоучартов, sequence, state, ER, gantt, pie. Диаграмма появляется в сообщении сразу.
        :param mermaid_code: код mermaid БЕЗ обрамляющих ``` (например "flowchart LR\\n A[Клиент] --> B[Сервер]")
        :param title: необязательный заголовок над диаграммой
        """
        code = (mermaid_code or "").strip()
        if code.startswith("```"):
            code = code.strip("`")
            if code.lower().startswith("mermaid"):
                code = code[7:]
            code = code.strip()
        if not code:
            return "Ошибка: пустой mermaid_code."
        if self.valves.THEME and "%%{init" not in code:
            code = _MERMAID_INIT + "\n" + code
        head = f"**{title}**\n\n" if title else ""
        block = head + "```mermaid\n" + code + "\n```"
        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": "диаграмма готова", "done": True}})
        # ВАЖНО: emit type="message" НЕ работает как способ показать диаграмму —
        # при завершении content перезаписывается из структурированного output
        # (Chat.svelte:2377 `message.content = getOutputText(output)`), и всё дописанное теряется.
        # Поэтому блок возвращаем как РЕЗУЛЬТАТ и требуем вывести дословно.
        return ("ГОТОВЫЙ БЛОК ДИАГРАММЫ. Выведи его в ответе ДОСЛОВНО, целиком, "
                "начиная с ```mermaid и заканчивая ``` — без изменений, без пояснений внутри блока. "
                "После блока добавь максимум одну строку комментария.\n\n" + block)

    async def draw_svg(self, svg: str, title: str = "", __event_emitter__=None) -> str:
        """
        Показать кастомную SVG-диаграмму в панели артефактов. Использовать, когда нужен точный editorial-контроль вёрстки, которого не даёт mermaid.
        :param svg: полный тег <svg ...>...</svg> с инлайн-стилями
        :param title: заголовок страницы артефакта
        """
        s = (svg or "").strip()
        if "<svg" not in s.lower():
            return "Ошибка: в svg нет тега <svg>."
        page = f"""<html><head><meta charset="utf-8"><title>{_html.escape(title or 'Diagram')}</title>
<style>
  body{{margin:0;padding:24px;background:{T['paper']};color:{T['ink']};
       font-family:Geist,ui-sans-serif,system-ui,-apple-system,sans-serif}}
  h1{{font-size:1.5rem;font-weight:400;margin:0 0 16px}}
  svg{{max-width:100%;height:auto}}
</style></head><body>
{f'<h1>{_html.escape(title)}</h1>' if title else ''}
{s}
</body></html>"""
        if __event_emitter__:
            await __event_emitter__({"type": "status",
                                     "data": {"description": "артефакт готов", "done": True}})
        return ("ГОТОВЫЙ HTML-АРТЕФАКТ. Выведи его в ответе ДОСЛОВНО, целиком, "
                "начиная с ```html и заканчивая ``` — тогда он откроется в панели артефактов.\n\n"
                + "```html\n" + page + "\n```")

    def diagram_style_guide(self) -> str:
        """
        Получить правила оформления диаграмм (палитра, плотность, соединения, типографика). Вызывать ПЕРЕД рисованием, если нужно оформить диаграмму в фирменном стиле.
        """
        return STYLE_GUIDE
