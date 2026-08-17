#!/usr/bin/env python3
"""
Прогон РЕАЛЬНЫХ сценариев через настоящий чат — с исполнением инструментов и фильтрами.

Зачем отдельно от smoke.py. Смоук проверяет, что компоненты грузятся и запускаются.
Он НЕ ловит то, на чём мы горели: модель отвечает кодом вместо файла, презентация
схлопывается в два слайда, в чат течёт сырой `</think>`, футер печатается тегами,
длинный код обрывается на середине. Это видно только на живом диалоге.

🔑 КАК ЭТО ВООБЩЕ ГОНЯТЬ БЕЗ БРАУЗЕРА. Обычный POST /api/chat/completions —
passthrough: он возвращает сырой ответ и НЕ исполняет инструменты. Но если добавить
`chat_id` + `id` + `session_id`, ручка отвечает `{"task_ids": [...]}` и обрабатывает
запрос асинхронно, как для UI: с tool-loop, с фильтрами, с записью в чат. Тогда
результат просто дочитывается из `/api/v1/chats/{id}`. Плюс обязателен `tool_ids` —
без него модель инструменты не видит (проверено: та же просьба даёт файл в браузере
и код в API, если tool_ids не передан).

Usage:
  ./usecases.py --list
  ./usecases.py                     # весь набор
  ./usecases.py --only files,render
  ./usecases.py --ssh qwenweb.vla.yp-c.yandex.net --host http://qwenweb.vla.yp-c.yandex.net:3000
"""
import argparse
import json
import re
import subprocess
import sys
import time
import uuid

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"
ASSISTANT_TOOLS = ["make_document", "diagram_tool", "plot_data"]


def C(kind, ok=True):
    """Хелпер для читаемых проверок."""
    return (kind, ok)


# ── проверки, которые умеет применять раннер ────────────────────────────────
def check(name, msg, files):
    text = msg.get("content") or ""
    out = msg.get("output") or []
    tool_calls = sum(1 for i in out if i.get("type") == "function_call")
    if name == "файл приложен":
        return bool(files), f"файлов {len(files)}"
    if name == "инструмент вызван":
        return tool_calls > 0, f"вызовов {tool_calls}"
    if name == "нет сырого think":
        bad = "</think>" in text or "<think>" in text
        return not bad, "найден тег" if bad else "чисто"
    if name == "нет тегов футера":
        bad = "<sub>" in text
        return not bad, "литеральный <sub>" if bad else "чисто"
    if name == "футер есть":
        return bool(re.search(r"\*slug=[^*\n]+\*", text)), "нет строки slug="
    if name == "код не оборван":
        return text.count("```") % 2 == 0, f"``` штук {text.count('```')}"
    if name == "есть код":
        return "```" in text, "нет блока кода"
    if name == "есть mermaid":
        return "```mermaid" in text, "нет блока mermaid"
    if name == "есть формула":
        return bool(re.search(r"\$\$?[^$]+\$\$?|\\\(|\\\[", text)), "нет latex"
    if name == "есть источник":
        return bool(msg.get("sources")), "источников нет"
    if name == "не отказ":
        bad = re.search(r"не могу (отправить|создать|сгенерировать) (бинарн|файл)", text, re.I)
        return not bad, "модель отказалась делать файл"
    if name == "есть ответ":
        return len(text.strip()) > 20, f"символов {len(text.strip())}"
    if name == "по-русски":
        cyr = sum(ch.isalpha() and ch.lower() in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя" for ch in text)
        lat = sum(ch.isalpha() and ch.lower() in "abcdefghijklmnopqrstuvwxyz" for ch in text)
        return cyr > lat, f"кириллицы {cyr}, латиницы {lat}"
    return True, "?"


# ── сценарии ────────────────────────────────────────────────────────────────
CASES = [
    # group, name, model, turns, checks
    ("files", "презентация из одного запроса", "assistant",
     ["сгенерируй pptx про пользу утренней зарядки"],
     ["файл приложен", "инструмент вызван", "не отказ", "нет сырого think", "футер есть"]),

    ("files", "длинная презентация (проверка лимита)", "assistant",
     ["Сделай pptx на 25 слайдов про историю авиации, на каждом слайде 4-5 подробных пунктов"],
     ["файл приложен", "инструмент вызван", "не отказ"]),

    ("files", "дописать разделы в тот же файл", "assistant",
     ["сгенерируй pptx про пользу плавания",
      "добавь в эту же презентацию ещё три слайда: для детей, после травм, техника дыхания"],
     ["файл приложен", "инструмент вызван", "не отказ"]),

    ("files", "pdf по тому же тексту", "assistant",
     ["сделай короткий pdf про пользу сна, 5 разделов"],
     ["файл приложен", "инструмент вызван", "не отказ"]),

    ("files", "таблица в xlsx", "assistant",
     ["сделай xlsx: колонки Модель, Задача, Успех; строки qwen38/booking/72, v7/booking/81"],
     ["файл приложен", "инструмент вызван"]),

    ("files", "документ Word", "assistant",
     ["сделай docx: краткая памятка по запуску RL-рана, три раздела"],
     ["файл приложен", "инструмент вызван"]),

    ("render", "длинный код (старый баг с обрывом)", "assistant",
     ["Напиши прототип на питоне: харнесс графового FSM-агента с узлами, переходами, "
      "валидацией и примером запуска. Один цельный файл."],
     ["есть код", "код не оборван", "нет сырого think"]),

    ("render", "mermaid-диаграмма", "assistant",
     ["нарисуй mermaid-схему пайплайна: сбор данных, обучение, оценка, деплой"],
     ["есть mermaid", "нет сырого think"]),

    ("render", "формулы LaTeX", "assistant",
     ["выпиши формулу градиента политики и objective GRPO в LaTeX"],
     ["есть формула", "нет сырого think"]),

    ("render", "график из данных", "assistant",
     ["построй график по данным: шаг,reward = 10,1.2 20,1.5 30,1.9 40,2.1"],
     ["есть ответ", "нет сырого think"]),

    ("chat", "обычный вопрос без инструментов", "assistant",
     ["чем GRPO отличается от PPO, коротко"],
     ["есть ответ", "нет сырого think", "нет тегов футера", "футер есть", "по-русски"]),

    ("chat", "многоходовый контекст", "assistant",
     ["запомни число 42", "какое число я просил запомнить?"],
     ["есть ответ", "нет сырого think"]),

    ("wiki", "вопрос по нашей вике", "wiki-helper",
     ["почему ган падает с exit code 37?"],
     ["есть ответ", "есть источник", "по-русски"]),

    ("wiki", "чего в вике нет", "wiki-helper",
     ["какая максимальная зарплата в команде?"],
     ["есть ответ"]),
]


def sh(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout


def admin_token(container, ssh):
    inner = ("import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
             "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
             "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),"
             "algorithm='HS256'))")
    cmd = (["ssh", "-o", "StrictHostKeyChecking=no", ssh,
            f"sudo docker exec -i {container} python3 -c {json.dumps(inner)}"] if ssh
           else ["docker", "exec", "-i", container, "python3", "-c", inner])
    return subprocess.check_output(cmd, text=True).strip().splitlines()[-1]


class Runner:
    def __init__(self, tok, host):
        self.tok, self.host = tok, host

    def api(self, method, path, body=None, timeout=180):
        args = ["curl", "-s", "-m", str(timeout), "-X", method,
                "-H", f"Authorization: Bearer {self.tok}",
                "-H", "Content-Type: application/json", f"{self.host}{path}"]
        if body is not None:
            args += ["--data-binary", json.dumps(body, ensure_ascii=False)]
        out = sh(args, timeout + 30)
        try:
            return json.loads(out)
        except Exception:
            return out

    def run_case(self, model, turns, wait=300):
        """Провести многоходовый диалог и вернуть последнее сообщение ассистента."""
        chat_id, history, last_assistant, parent = None, {}, None, None
        for prompt in turns:
            uid_u, uid_a = str(uuid.uuid4()), str(uuid.uuid4())
            now = int(time.time())
            history[uid_u] = {"id": uid_u, "parentId": parent, "childrenIds": [uid_a],
                              "role": "user", "content": prompt, "timestamp": now}
            history[uid_a] = {"id": uid_a, "parentId": uid_u, "childrenIds": [],
                              "role": "assistant", "content": "", "model": model,
                              "timestamp": now}
            if parent:
                history[parent]["childrenIds"] = [uid_u]
            payload = {"chat": {"title": "usecase", "models": [model],
                                "messages": list(history.values()),
                                "history": {"currentId": uid_a, "messages": history}}}
            if chat_id is None:
                chat = self.api("POST", "/api/v1/chats/new", payload)
                chat_id = chat.get("id") if isinstance(chat, dict) else None
                if not chat_id:
                    return None, f"не создался чат: {str(chat)[:120]}"
            else:
                self.api("POST", f"/api/v1/chats/{chat_id}", payload)

            msgs = [{"role": m["role"], "content": m["content"]}
                    for m in history.values() if m["content"] or m["role"] == "user"]
            r = self.api("POST", "/api/chat/completions",
                         {"model": model, "stream": True, "messages": msgs,
                          "chat_id": chat_id, "id": uid_a,
                          "session_id": "uc" + uuid.uuid4().hex[:8],
                          # 🚨 без tool_ids модель инструментов НЕ видит и отвечает кодом
                          "tool_ids": ASSISTANT_TOOLS})
            if not (isinstance(r, dict) and r.get("task_ids")):
                return None, f"запуск не принят: {str(r)[:120]}"

            deadline = time.time() + wait
            msg = None
            while time.time() < deadline:
                time.sleep(4)
                ch = self.api("GET", f"/api/v1/chats/{chat_id}")
                mm = ((ch.get("chat") or {}).get("history") or {}).get("messages", {})
                msg = mm.get(uid_a) or {}
                if msg.get("done"):
                    break
            else:
                return None, f"не дождался ответа за {wait}s"
            history[uid_a] = {**history[uid_a], "content": msg.get("content") or ""}
            last_assistant, parent = msg, uid_a
        return last_assistant, chat_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="", help="группы через запятую: files,render,chat,wiki")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--ssh", default=None)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--wait", type=int, default=300)
    a = ap.parse_args()

    groups = {g.strip() for g in a.only.split(",") if g.strip()}
    cases = [c for c in CASES if not groups or c[0] in groups]
    if a.list:
        for g, n, m, turns, checks in cases:
            print(f"  [{g}] {n}  ({m}, ходов {len(turns)})")
        print(f"\n{len(cases)} сценариев")
        return 0

    tok = admin_token(a.container, a.ssh)
    run = Runner(tok, a.host)
    print(f"Реальные сценарии · {a.host} · {len(cases)} шт\n")
    failed = 0
    for g, name, model, turns, checks in cases:
        t0 = time.time()
        msg, info = run.run_case(model, turns, a.wait)
        dt = int(time.time() - t0)
        if msg is None:
            failed += 1
            print(f"  [ПАД ] [{g}] {name}  ← {info}")
            continue
        files = msg.get("files") or []
        bad = []
        for c in checks:
            ok, why = check(c, msg, files)
            if not ok:
                bad.append(f"{c} ({why})")
        mark = "ok  " if not bad else "ПАД "
        failed += bool(bad)
        print(f"  [{mark}] [{g}] {name}  {dt}s")
        for b in bad:
            print(f"          не прошло: {b}")
    print(f"\nИтог: {len(cases) - failed}/{len(cases)} сценариев прошли")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
