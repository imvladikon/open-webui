#!/usr/bin/env python3
"""
Смоук кокпита: проверяет, что КАЖДАЯ тулза и функция реально грузится и работает.

Зачем. Больше всего времени сожрали баги вида «в демо не работает draw_svg / debug /
inspect». Все они ловятся до пользователя, если один раз прогнать каждый компонент
тем же загрузчиком, которым его грузит сам Open WebUI.

Что проверяем:
  * тулзы  — грузятся модулем OWUI, отдают функции, и по ним СТРОИТСЯ СПЕКА. Пустое
             описание или аргумент без описания = модель тулзу толком не увидит, это WARN.
  * filter — реально прогоняем `outlet` на синтетическом длинном ответе с рассуждением.
             Это не импорт-чек: сразу видно, свернул ли collapse_reasoning черновик и
             дописал ли run_metadata подвал.
  * action — прогоняем на снапшоте чата правильной формы (`chat.chat.history.messages`,
             на один уровень глубже, чем кажется — на этом уже спотыкались).
  * pipe   — инстанс + Valves + сигнатура. Живой вызов только с --live: он стоит GPU
             и падает, когда серв флапает, а это не дефект кокпита.

Usage:
  ./smoke.py                       # локальный контейнер
  ./smoke.py --container open-webui --host http://[::1]:3000
  ./smoke.py --ssh qwenweb.vla.yp-c.yandex.net   # демо на VM
  ./smoke.py --live                # дополнительно дёрнуть пайпы через живую модель

Код возврата 1, если хоть один компонент упал (годится для CI/провижена).
"""
import argparse
import json
import subprocess
import sys

# Пробник исполняется ВНУТРИ контейнера: там установлен open_webui и все зависимости.
PROBE = r'''
import asyncio, inspect, json, sys, traceback
res = {"tools": [], "functions": []}

def cap(e):
    return f"{type(e).__name__}: {e}".split("\n")[0][:200]

# В 0.11 загрузчики и модели асинхронные (get_tools/get_functions/load_*_module_by_id),
# поэтому весь пробник живёт в одном asyncio.run.
from open_webui.utils.tools import (load_tool_module_by_id, get_functions_from_tool,
                                    get_tool_specs)
from open_webui.models.tools import Tools as ToolsTable
from open_webui.utils.plugin import load_function_module_by_id
from open_webui.models.functions import Functions as FnTable

# Длинный синтетический ответ: голова — черновик рассуждения БЕЗ тегов <think>
# (наши сервы отдают именно так), хвост — настоящий ответ.
DRAFT = ("The user is asking about GRPO. We need to explain the difference from PPO.\n\n"
         "Let me think about what matters here. PPO uses a learned value network as the "
         "baseline, which costs a second model in memory and can be badly calibrated early "
         "in training when the reward scale is still moving around.\n\n"
         "First, I should recall how the advantage is computed in each. Need to be careful "
         "not to conflate the clipping with the baseline choice, those are separate.\n\n"
         "Okay, I also want to mention dead groups because that is the practical failure "
         "mode people hit. If every sample in a group gets the same reward the advantage is "
         "identically zero and the group contributes no gradient at all.\n\n"
         "Wait, I should also consider the KL penalty, since both objectives keep a leash on "
         "the reference policy and the way that leash is applied differs between "
         "implementations. Some put it in the reward, some in the loss.\n\n"
         "Let me also think about group size. Too small and the mean baseline is noisy, too "
         "large and each optimizer step costs a lot of sampling. In our booking runs this is "
         "the knob that actually moves wall-clock per step.\n\n"
         "Итак, ответ. GRPO убирает value-сеть: базовой линией служит средняя награда по "
         "группе сэмплов на один промпт. Дешевле по памяти и устойчивее в начале обучения, "
         "но появляется своя болячка: если вся группа получила одинаковую награду, "
         "преимущество равно нулю и группа не даёт градиента.")
SNAP = {"chat": {"chat": {"history": {"messages": {
    "m1": {"id": "m1", "role": "user", "content": "чем GRPO отличается от PPO"},
    "m2": {"id": "m2", "role": "assistant", "content": DRAFT,
           "usage": {"completion_tokens": 420, "prompt_tokens": 51},
           "model": "qwen38-27b-gate"}}}}}}

async def call(fn, kwargs):
    sig = inspect.signature(fn)
    ok = {k: v for k, v in kwargs.items() if k in sig.parameters}
    out = fn(**ok)
    return await out if inspect.isawaitable(out) else out

async def emit(ev):
    return None

async def main():
    for tid in [t.id for t in await ToolsTable.get_tools()]:
        r = {"id": tid, "ok": False, "n": 0, "warn": [], "err": None}
        try:
            mod, _ = await load_tool_module_by_id(tid)
            specs = get_tool_specs(mod)
            r["n"] = len(specs)
            for s in specs:
                if not (s.get("description") or "").strip():
                    r["warn"].append(f"{s['name']}: нет описания")
                props = (s.get("parameters") or {}).get("properties") or {}
                miss = [p for p, v in props.items() if not (v.get("description") or "").strip()]
                if miss:
                    r["warn"].append(f"{s['name']}: аргументы без описания {miss}")
            if not specs:
                r["err"] = "не отдала ни одной функции (нет публичных методов?)"
            else:
                r["ok"] = True
        except Exception as e:
            r["err"] = cap(e)
        res["tools"].append(r)

    for f in await FnTable.get_functions():
        r = {"id": f.id, "type": f.type, "active": bool(f.is_active),
             "ok": False, "err": None, "note": ""}
        try:
            mod, _, _ = await load_function_module_by_id(f.id)
            if f.type == "filter":
                body = {"messages": [SNAP["chat"]["chat"]["history"]["messages"]["m1"],
                                     dict(SNAP["chat"]["chat"]["history"]["messages"]["m2"])],
                        "model": "qwen38-27b-gate"}
                # Фильтр может быть ТОЛЬКО inlet (например переключатель длины ответа) —
                # это законно, и раньше смоук считал такой фильтр упавшим.
                if not hasattr(mod, "outlet"):
                    out = await call(mod.inlet, {"body": body, "__event_emitter__": emit,
                                                 "__user__": {"id": "u", "role": "admin"},
                                                 "__metadata__": {},
                                                 "__model__": {"id": "qwen38-27b-gate"}})
                    r["note"] = ("только inlet, max_tokens="
                                 + str((out or body).get("max_tokens", "не задан")))
                    r["changed"] = True
                    r["ok"] = True
                    res["functions"].append(r)
                    continue
                before = body["messages"][-1]["content"]
                out = await call(mod.outlet, {"body": body, "__event_emitter__": emit,
                                              "__user__": {"id": "u", "role": "admin"},
                                              "__model__": {"id": "qwen38-27b-gate"}})
                after = (out or body)["messages"][-1]["content"]
                r["changed"] = after != before
                r["details"] = "<details" in after
                d = len(after) - len(before)
                r["note"] = (f"текст изменён ({d:+d} симв.)" if r["changed"]
                             else "текст НЕ изменён")
                if r["details"]:
                    r["note"] += ", черновик свёрнут"
                r["ok"] = True
            elif f.type == "action":
                out = await call(mod.action, {"body": SNAP, "__event_emitter__": emit,
                                              "__user__": {"id": "u", "role": "admin"},
                                              "__id__": "raw", "__request__": None})
                r["note"] = "вернула " + (type(out).__name__ if out is not None else "None")
                r["ok"] = True
            else:  # pipe
                # load_function_module_by_id отдаёт УЖЕ СОЗДАННЫЙ экземпляр класса,
                # а не сам класс: mod и есть Pipe(). Обращение к mod.Pipe падает.
                p = mod
                p.valves  # Valves должны инстанцироваться, иначе пайп не стартует
                if not callable(getattr(p, "pipe", None)):
                    raise RuntimeError("нет метода pipe()")
                names = []
                if hasattr(p, "pipes"):
                    got = p.pipes()
                    names = [x.get("id") for x in (got or [])]
                r["note"] = ("варианты: " + ",".join(map(str, names))) if names else "один пайп"
                r["ok"] = True
        except Exception as e:
            r["err"] = cap(e)
            r["tb"] = traceback.format_exc().strip().split("\n")[-3:]
        res["functions"].append(r)

asyncio.run(main())
print("@@SMOKE@@" + json.dumps(res, ensure_ascii=False))
'''


# Чего мы ЖДЁМ от фильтров на синтетическом ответе выше. Без этого смоук зелёный
# даже когда фильтр тихо ничего не делает.
EXPECT = {
    "run_metadata": ("changed", "не дописал подвал с метаданными"),
    "collapse_reasoning": ("details", "не свернул черновик рассуждения в <details>"),
}


def run_probe(container, ssh):
    cmd = ["docker", "exec", "-i", container, "python3", "-"]
    if ssh:
        # На VM docker под sudo; кормим пробник через stdin ssh.
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", ssh,
               f"sudo docker exec -i {container} python3 -"]
    p = subprocess.run(cmd, input=PROBE, capture_output=True, text=True, timeout=300)
    for line in p.stdout.splitlines():
        if line.startswith("@@SMOKE@@"):
            return json.loads(line[len("@@SMOKE@@"):])
    print("Пробник не отдал результат. stderr:\n" + (p.stderr or "")[-2000:], file=sys.stderr)
    return None


def live_check(host, tok, model, ssh=None, timeout=180):
    body = {"model": model, "stream": False,
            "messages": [{"role": "user", "content": "ответь одним словом: тест"}]}
    curl = ["curl", "-s", "-m", str(timeout), "-X", "POST",
            "-H", f"Authorization: Bearer {tok}",
            "-H", "Content-Type: application/json",
            "--data-binary", json.dumps(body),
            f"{host}/api/chat/completions"]
    if ssh:
        # Токен взят из БД на VM, значит и запрос должен идти с VM: локальный
        # контейнер его не признает и вернёт 401 (уже наступали).
        curl = ["ssh", "-o", "StrictHostKeyChecking=no", ssh,
                " ".join(__import__("shlex").quote(x) for x in curl)]
    p = subprocess.run(curl, capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
    except Exception:
        return False, (p.stdout or "пусто")[:120]
    if "choices" in d:
        txt = (d["choices"][0].get("message") or {}).get("content") or ""
        return bool(txt.strip()), txt.strip()[:80] or "пустой content"
    return False, str(d.get("detail") or d)[:120]


def admin_token(container, ssh):
    inner = ("import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
             "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
             "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),"
             "algorithm='HS256'))")
    if ssh:
        out = subprocess.check_output(
            ["ssh", "-o", "StrictHostKeyChecking=no", ssh,
             f"sudo docker exec -i {container} python3 -c {json.dumps(inner)}"], text=True)
    else:
        out = subprocess.check_output(
            ["docker", "exec", "-i", container, "python3", "-c", inner], text=True)
    return out.strip().splitlines()[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="open-webui")
    ap.add_argument("--ssh", default=None, help="хост VM, если контейнер не локальный")
    ap.add_argument("--host", default="http://[::1]:3000")
    ap.add_argument("--live", action="store_true", help="дёрнуть пайпы через живую модель")
    a = ap.parse_args()

    where = a.ssh or "локально"
    print(f"Смоук кокпита · {where} · контейнер {a.container}\n")
    res = run_probe(a.container, a.ssh)
    if res is None:
        return 2
    if res.get("fatal"):
        print("Пробник упал на старте:", res["fatal"])
        return 2

    bad = 0
    print("ТУЛЗЫ")
    for t in res["tools"]:
        mark = "ok  " if t["ok"] else "ПАД "
        bad += 0 if t["ok"] else 1
        print(f"  [{mark}] {t['id']:<18} функций: {t['n']}"
              + (f"   ← {t['err']}" if t["err"] else ""))
        for w in t["warn"]:
            print(f"          warn: {w}")

    print("\nФУНКЦИИ")
    for f in res["functions"]:
        mark = "ok  " if f["ok"] else "ПАД "
        bad += 0 if f["ok"] else 1
        act = "" if f["active"] else "  (выключена)"
        print(f"  [{mark}] {f['id']:<18} {f['type']:<7} {f['note']}{act}"
              + (f"   ← {f['err']}" if f["err"] else ""))
        # Мало знать, что фильтр не упал: он должен что-то сделать с текстом.
        # Иначе он тихо стоит выключенным по логике и никто этого не замечает.
        if f["ok"] and f["id"] in EXPECT:
            key, why = EXPECT[f["id"]]
            if not f.get(key):
                bad += 1
                print(f"          ПАД: {why}")
        for line in f.get("tb", []) if f["err"] else []:
            print(f"          {line}")

    if a.live:
        print("\nЖИВОЙ ВЫЗОВ (нужен поднятый серв)")
        tok = admin_token(a.container, a.ssh)
        for m in ["debug-base", "ab-base", "ab-rl-v7"]:
            ok, msg = live_check(a.host, tok, m, a.ssh)
            print(f"  [{'ok  ' if ok else 'нет '}] {m:<18} {msg}")
            # серв лежит — это не дефект кокпита, в код возврата не считаем

    print(f"\nИтог: {'всё зелёное' if not bad else str(bad) + ' компонент(ов) упало'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
