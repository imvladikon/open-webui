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
import re
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

    async def draw_svg(self, svg: str, title: str = "", __request__=None, __user__=None,
                       __event_emitter__=None, __chat_id__=None) -> str:
        """
        Сохранить кастомную SVG-диаграмму как файл и показать её карточкой в чате. Использовать, когда нужен точный контроль вёрстки, которого не даёт mermaid.
        :param svg: полный тег <svg ...>...</svg> с инлайн-стилями
        :param title: заголовок диаграммы (пойдёт в имя файла)
        """
        # 🚨 ПОЧЕМУ НЕ ЭХО. Раньше тулза возвращала HTML и просила модель скопировать его дословно
        # в ```html. Это НЕ работает на реальных SVG: модель упирается в max_tokens, копируя
        # разметку, блок ``` не закрывается, артефакт не рендерится (проверено на 17 КБ).
        # Правильный путь тот же, что в project_gen: пишем файл через Files API server-side,
        # модель не копирует НИЧЕГО, пользователь получает карточку + ссылку.
        s = (svg or "").strip()
        if "<svg" not in s.lower():
            return "Ошибка: в svg нет тега <svg>."
        if not s.lower().startswith("<svg"):          # срезаем возможную обёртку/пояснения
            i = s.lower().find("<svg")
            j = s.lower().rfind("</svg>")
            if i >= 0 and j > i:
                s = s[i:j + 6]
        name = re.sub(r"[^\w.-]+", "_", (title or "diagram").strip())[:60] or "diagram"

        # автономная страница в наших токенах: и как .svg, и как просмотрщик
        page = (f'<html><head><meta charset="utf-8"><title>{_html.escape(title or "Diagram")}</title>'
                f'<style>body{{margin:0;padding:24px;background:{T["paper"]};color:{T["ink"]};'
                f'font-family:Geist,ui-sans-serif,system-ui,sans-serif}}'
                f'h1{{font-size:1.4rem;font-weight:400;margin:0 0 16px}}'
                f'svg{{max-width:100%;height:auto}}</style></head><body>'
                + (f"<h1>{_html.escape(title)}</h1>" if title else "") + s + "</body></html>")

        if not __request__ or not __user__:
            # фолбэк для не-UI контура: отдаём инлайном (мелкие SVG рендерятся и так)
            return ("Файловый режим недоступен (нет request/user). Вот SVG инлайном:\n\n"
                    "```html\n" + page + "\n```")
        try:
            import io
            from fastapi import UploadFile
            from open_webui.models.users import Users
            from open_webui.models.files import Files
            from open_webui.routers.files import upload_file_handler

            user = await Users.get_user_by_id(__user__["id"])

            async def put(fname, data: bytes, ctype: str, text=None):
                f = UploadFile(file=io.BytesIO(data), filename=fname,
                               headers={"content-type": ctype})
                it = await upload_file_handler(__request__, file=f,
                                               metadata={"chat_id": __chat_id__, "lab_diagram": True},
                                               process=False, process_in_background=False, user=user)
                if text is not None:
                    await Files.update_file_data_by_id(it.id, {"content": text})
                return it

            svg_file = await put(f"{name}.svg", s.encode("utf-8"), "image/svg+xml", text=s)
            html_file = await put(f"{name}.html", page.encode("utf-8"), "text/html")
        except Exception as e:
            return (f"Не удалось сохранить SVG как файл ({type(e).__name__}: {e}). "
                    "Вот разметка инлайном:\n\n```html\n" + page + "\n```")

        if __event_emitter__:
            await __event_emitter__({"type": "files", "data": {"files": [
                {"type": "file", "id": svg_file.id, "name": f"{name}.svg",
                 "size": len(s.encode()), "url": f"/api/v1/files/{svg_file.id}"},
                {"type": "file", "id": html_file.id, "name": f"{name}.html",
                 "size": len(page.encode()), "url": f"/api/v1/files/{html_file.id}"}]}})
            await __event_emitter__({"type": "status",
                                     "data": {"description": f"SVG сохранён: {name}.svg", "done": True}})
        return (f"Диаграмма сохранена как файл, НЕ копируй разметку в ответ. "
                f"Ответь коротко и дай ссылки:\n\n"
                f"[Открыть {name}.svg](/api/v1/files/{svg_file.id}/content/{name}.svg) · "
                f"[Просмотрщик {name}.html](/api/v1/files/{html_file.id}/content/{name}.html)")

    def diagram_style_guide(self) -> str:
        """
        Получить правила оформления диаграмм (палитра, плотность, соединения, типографика). Вызывать ПЕРЕД рисованием, если нужно оформить диаграмму в фирменном стиле.
        """
        return STYLE_GUIDE
