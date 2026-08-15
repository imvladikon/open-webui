# Генерация целого проекта (много файлов) в чате

Разбор по коду 0.11.0. Проблема: чат плохо подходит для проекта, один огромный ответ рвётся по
max_tokens, его нельзя скачать и дифать. Решение не «заставить модель напечатать всё», а
генерировать по файлу и складывать результат в настоящие файлы.

## Ключевые факты по коду (проверено)
- **Pipe может создавать файлы через Files API прямо в процессе**, не по HTTP: рецепт из самого
  OWUI `utils/files.py:135`, хендлер `routers/files.py:315` `upload_file_handler(process=False)`.
  `process=False` обязателен: не гонит RAG и не требует whitelisted-расширение (`files.py:345`).
- **Ссылка на скачивание** = `/api/v1/files/{id}/content/{name}` (`files.py:894`, всегда
  attachment). Кука `token` уходит с кликом (`utils/auth.py:319`), поэтому обычная markdown-ссылка
  скачивает файл без возни с заголовками. Для zip ветка attachment `files.py:812`.
- **Карточка файла на ответе** персистится в БД: событие
  `{"type":"files","data":{"files":[{"type":"file","id":<id>,"name":..,"size":..,"url":"/api/v1/files/<id>"}]}}`
  → `socket/main.py:1055` → `ResponseMessage.svelte:687`. `url` НЕ должен начинаться с http.
  Превью кода в модалке пустое при `process=False` → дописать `Files.update_file_data_by_id(id,{"content":text})`.
- **Прогресс по файлам** = `status`-события (`socket/main.py:1010`, персист), один статус на файл,
  не по токену. Финальный `done:true`.
- **embed от тулзы** (`HTMLResponse` inline → `middleware.py:890`) кладёт HTML в `embeds`
  (персистится), модели отдаёт короткий статус вместо payload — лекарство от «копируй большой текст».
  В embed-iframe есть `allow-popups`+`allow-downloads`, значит ссылки на Files API работают
  по-настоящему (в отличие от ```html-артефакта, где sandbox без popups и без same-origin).
- Из outlet персистится ТОЛЬКО content — файлы/статусы слать событиями из pipe.

## Чего в 0.11 НЕТ (проверено отсутствие)
Сущности «проект»/canvas/multi-file artifact; «скачать все файлы сообщения» и серверной zip-упаковки
чатовых файлов (единственный zip — `knowledge/{id}/export`, admin-only, теряет директории,
переименовывает в .txt); кнопки Download на карточке файла (только через модалку); папок у Files API
для чатовых файлов; версий/diff файлов; доступа из артефакт-iframe к API (нет same-origin, кука не
уходит, в панели артефактов ещё и нет popups); CSP по умолчанию (`IFRAME_CSP=''`).

## Рекомендация: A (ядро) + B как embed-витрина
**A. Files API + генерация по файлу + zip.** Три фазы: (1) короткий вызов → JSON-манифест файлов;
(2) цикл, отдельный вызов на файл со своим бюджетом max_tokens (обрыв проекта в целом невозможен),
в контекст кладём манифест + СИГНАТУРЫ уже готовых файлов, не тела; внутри файла при
finish_reason=length включается autocontinue; (3) каждый файл → Files API, потом zip → Files API.
Пользователь видит статус «7/12 · src/api/router.py», в конце дерево путей + жирная ссылка
«Скачать весь проект» + карточки файлов. Всё переживает F5 и regenerate. Трудозатраты S/M (~1.5 дня).

**B. Вьюер дерева как embed** (не как ```html-артефакт): дерево слева, код справа, «скачать всё».
Как embed есть popups → ссылки на Files API вместо инлайна тел, content не раздувается. Кнопки
«перегенерировать файл» через доверенный `action:submit` (`Chat.svelte:1148`). +1-1.5 дня.

Скелет pipe (фаза заливки):
```python
from open_webui.routers.files import upload_file_handler
from open_webui.models.users import Users
from open_webui.models.files import Files
from fastapi import UploadFile
import io, zipfile

user = await Users.get_user_by_id(__user__["id"])
async def put(name, data, ctype, text=None):
    f = UploadFile(file=io.BytesIO(data), filename=name, headers={"content-type": ctype})
    it = await upload_file_handler(__request__, file=f, metadata={"chat_id": __chat_id__},
                                   process=False, user=user)
    if text is not None:
        await Files.update_file_data_by_id(it.id, {"content": text})
    return it
# ... манифест -> цикл по файлам (autocontinue внутри) -> zip -> событие "files" + ссылка в content
```

Варианты C (Knowledge с директориями) и D (terminal server, реальный запуск кода) — в полном
разборе; C добавлять позже как «сохранить проект», D — когда понадобится прогонять тесты.
Риски A: модель ломает JSON манифеста (ретрай + жёсткий парсер); консистентность импортов между
файлами (сигнатуры в контекст + финальная проверка); `rag.file.max_size` может резать крупный zip;
уборка мусора в Files API по префиксу в meta.
