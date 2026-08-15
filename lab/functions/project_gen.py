"""
title: Project Generator (multi-file)
author: agents-team
version: 0.1.0
description: Генерация целого проекта в чате без обрыва: модель делает манифест файлов, затем генерит по файлу за вызов, каждый файл заливается в Files API, в конце собирается zip. Пользователь получает дерево, карточки файлов и ссылку «скачать весь проект». Обход max_tokens по конструкции.
"""
# Дизайн: lab/docs/multi-file-project-generation.md. Ключевое: один вызов = один файл,
# поэтому потолок max_tokens больше не ограничивает ПРОЕКТ (только отдельный файл, где при
# обрыве включается автопродолжение). Файлы кладём в Files API (upload_file_handler,
# process=False), карточки и статусы персистятся событиями (из outlet персистится только content).
import io
import json
import os
import re
import zipfile
from pydantic import BaseModel, Field

MANIFEST_SYS = (
    "Верни ТОЛЬКО один JSON-объект и ничего больше. НЕ рассуждай, не повторяй пример, не пиши "
    "текст до или после. Ключи: project (snake_case строка) и files (массив из 3-8 объектов "
    "{path, purpose}), пути с расширениями. Начни ответ с { и закончи }."
)
FILE_SYS = (
    "Отвечай СРАЗУ содержимым файла. НЕ рассуждай вслух, не пиши черновики, не перечисляй "
    "варианты, не комментируй ход мысли, не пиши план. Выведи ТОЛЬКО код файла: первый символ "
    "ответа это первая строка файла (import, #, def, class и т.п.). Без markdown-заборов ```, "
    "без имени файла, без текста до или после. Код законченный и согласованный с сигнатурами "
    "уже созданных файлов."
)
# первая «кодовая» строка: с неё начинается настоящий файл, всё до неё это протёкшее рассуждение
_CODE_START = re.compile(
    r"^\s*(?:#!|# -\*-|#|import |from |def |class |async |@|\"\"\"|'''|package |using |const |"
    r"import\{|export |function |type |interface |public |private |/\*|//|<\?php|<!DOCTYPE|<html)")
CONT = "Продолжи ровно с места обрыва, не повторяя написанное, без пояснений."
_FENCE = re.compile(r"^```[\w-]*\n(.*)\n```\s*$", re.S)
_SIG = re.compile(r"^\s*(def |class |async def |export |function |const |type |interface )", re.M)


def _extract_manifest(text: str):
    """Достаём JSON с ключом files из зашумлённого ответа reasoning-модели.
    Проблема: модель эхом повторяет пример из системного промпта и рассуждает прозой, поэтому
    брать первый {...} нельзя. Скан по балансу скобок, берём ПОСЛЕДНИЙ объект, который парсится
    И содержит непустой files."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    best = None
    depth = start = 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                frag = text[start:i + 1]
                try:
                    obj = json.loads(frag)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("files"):
                    best = obj          # последний валидный с files выигрывает
    return best


class Pipe:
    class Valves(BaseModel):
        ELIZA_BASE: str = Field(
            default="https://api.eliza.yandex.net/raw/internal/zeliboba/qwen38-27b-gate/v1")
        MODEL: str = Field(default="qwen38-27b-gate")
        MANIFEST_MAX_TOKENS: int = Field(default=1200)
        FILE_MAX_TOKENS: int = Field(default=6000)
        MAX_FILES: int = Field(default=5, description="потолок числа файлов (скорость демо)")
        CONT_ROUNDS: int = Field(default=2, description="автопродолжений на файл")

    def __init__(self):
        self.valves = self.Valves()

    def _token(self) -> str:
        return os.environ.get("OPENAI_API_KEYS", os.environ.get("OPENAI_API_KEY", "")).split(";")[0]

    async def _llm(self, cx, messages, max_tokens):
        r = await cx.post(f"{self.valves.ELIZA_BASE}/chat/completions",
                          json={"model": self.valves.MODEL, "messages": messages,
                                "temperature": 0.2, "max_tokens": max_tokens, "stream": False},
                          headers={"Authorization": f"Bearer {self._token()}",
                                   "Content-Type": "application/json"})
        r.raise_for_status()
        d = r.json()["choices"][0]
        return d["message"].get("content") or "", d.get("finish_reason")

    async def _gen_file(self, cx, manifest, spec, prior_sigs):
        ctx = (f"Проект: {manifest['project']}\nМанифест: "
               f"{json.dumps([f['path'] for f in manifest['files']], ensure_ascii=False)}\n"
               f"Сигнатуры готовых файлов:\n{prior_sigs or '(пока нет)'}\n\n"
               f"Сгенерируй файл {spec['path']} — {spec.get('purpose','')}.")
        msgs = [{"role": "system", "content": FILE_SYS}, {"role": "user", "content": ctx}]
        acc = ""
        for _ in range(self.valves.CONT_ROUNDS + 1):
            piece, fr = await self._llm(cx, msgs, self.valves.FILE_MAX_TOKENS)
            acc += piece
            if fr != "length":
                break
            msgs += [{"role": "assistant", "content": piece}, {"role": "user", "content": CONT}]
        return self._clean_code(acc)

    def _clean_code(self, text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        if "</think>" in text:
            text = text.split("</think>")[-1]
        m = _FENCE.match(text.strip())       # если всё же обернул в ```
        if m:
            return m.group(1).strip()
        # срезаем протёкшее рассуждение до первой кодовой строки
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if _CODE_START.match(ln):
                return "\n".join(lines[i:]).strip()
        return text.strip()

    async def pipe(self, body: dict, __request__=None, __user__=None,
                   __event_emitter__=None, __chat_id__=None):
        import httpx
        from open_webui.routers.files import upload_file_handler
        from open_webui.models.users import Users
        from open_webui.models.files import Files
        from fastapi import UploadFile

        async def emit(desc, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": desc, "done": done}})

        if not __request__ or not __user__:
            return "Ошибка: этот генератор работает только из UI-контура (нужны request и user)."
        user = await Users.get_user_by_id(__user__["id"])

        async def put(name, data: bytes, ctype: str, text: str | None = None):
            f = UploadFile(file=io.BytesIO(data), filename=name,
                           headers={"content-type": ctype})
            it = await upload_file_handler(__request__, file=f,
                                           metadata={"chat_id": __chat_id__, "lab_project": True},
                                           process=False, process_in_background=False, user=user)
            if text is not None:
                await Files.update_file_data_by_id(it.id, {"content": text})
            return it

        req = (body.get("messages") or [{}])[-1].get("content", "")
        async with httpx.AsyncClient(timeout=600, verify=False) as cx:
            await emit("Планирую структуру проекта")
            manifest = None
            for attempt in range(2):        # reasoning-модель со второго раза обычно чище
                umsg = req if attempt == 0 else (
                    req + "\n\nВыведи ТОЛЬКО JSON-объект {project, files:[{path,purpose}]}, без слов.")
                raw, _ = await self._llm(cx, [{"role": "system", "content": MANIFEST_SYS},
                                              {"role": "user", "content": umsg}],
                                         self.valves.MANIFEST_MAX_TOKENS)
                manifest = _extract_manifest(raw)
                if manifest:
                    break
            if not manifest:
                return ("Не удалось получить манифест проекта (модель не вернула валидный JSON "
                        "с files). Попробуй переформулировать запрос.\n\n```\n" + raw[:500] + "\n```")
            files = manifest.get("files", [])[: self.valves.MAX_FILES]
            manifest["files"] = files
            project = re.sub(r"[^\w.-]", "_", manifest.get("project", "project"))

            cards, tree, sigs = [], [], []
            for i, spec in enumerate(files, 1):
                await emit(f"{i}/{len(files)} · {spec['path']}")
                code = await self._gen_file(cx, manifest, spec, "\n".join(sigs))
                data = code.encode("utf-8")
                it = await put(spec["path"].replace("/", "__"), data, "text/plain", text=code)
                cards.append({"type": "file", "id": it.id, "name": spec["path"],
                              "size": len(data), "url": f"/api/v1/files/{it.id}"})
                tree.append((spec["path"], code))
                sig_lines = [l for l in code.splitlines() if _SIG.match(l)][:8]
                sigs.append(f"# {spec['path']}\n" + "\n".join(sig_lines))

            await emit("Собираю zip")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for path, code in tree:
                    z.writestr(f"{project}/{path}", code)
            zf = await put(f"{project}.zip", buf.getvalue(), "application/zip")

        if __event_emitter__:
            await __event_emitter__({"type": "files", "data": {"files": cards + [
                {"type": "file", "id": zf.id, "name": f"{project}.zip",
                 "size": buf.tell(), "url": f"/api/v1/files/{zf.id}"}]}})
        await emit(f"{len(tree)} файлов · {buf.tell()} Б", done=True)

        lines = "\n".join(f"- `{p}` ({len(c.encode())} Б)" for p, c in tree)
        return (f"### {project}\n\n"
                f"**[Скачать весь проект ({len(tree)} файлов)]"
                f"(/api/v1/files/{zf.id}/content/{project}.zip)**\n\n{lines}\n\n"
                f"Файлы также прикреплены карточками ниже — клик открывает просмотрщик с подсветкой.")
