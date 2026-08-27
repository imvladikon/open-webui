#!/usr/bin/env python3
"""
Импорт роллаутов (траекторий бенча) в Open WebUI как обычных чатов.

Зачем: смотреть, ГДЕ модель сломалась, в нормальном UI со сворачиваемыми тул-вызовами,
а не листать JSONL глазами. Сверху бесплатно работает всё остальное: оценки, теги, папки,
ветвление и контрфактический реплей (правишь результат тула и жмёшь regenerate).

🔑 Главное про формат. Фронт матчит `function_call_output` с `function_call` по `call_id`
ВНУТРИ `output` ОДНОГО сообщения (`Messages/structuredOutput.ts`). Значит вся цепочка
одного хода (мысли, вызовы, результаты, финальный текст) кладётся в ОДНО assistant-сообщение
плоским списком `output`. Если разложить по разным сообщениям, тул-вызовы навсегда зависнут
в состоянии "Executing...".

Поддерживает два входных формата:
  1. наш step-attribution: {trace_id, history:[{role,type,content_short,tool_name,tool_args,...}], reward, target, outcome}
  2. обычный OpenAI: {id, messages:[{role, content, tool_calls, tool_call_id}], ...}

Usage:
  ./import_rollout.py traces.jsonl --limit 5 --folder "bench/tau2" --tag rl-v7
  ./import_rollout.py traces.jsonl --limit 1 --dry-run
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"


def oid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def admin_token(container=CONTAINER) -> str:
    return subprocess.check_output([
        "docker", "exec", "-i", container, "python3", "-c",
        "import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
        "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
        "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),algorithm='HS256'))"
    ], text=True).strip()


def api(method, path, tok, body=None, host=HOST):
    args = ["curl", "-s", "-w", "\n%{http_code}", "-X", method,
            "-H", f"Authorization: Bearer {tok}", "-H", "Content-Type: application/json", f"{host}{path}"]
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(body, tmp, ensure_ascii=False)
        tmp.close()
        args += ["--data", "@" + tmp.name]      # тело в файл: большие трейсы не влезают в argv
    out = subprocess.run(args, capture_output=True, text=True).stdout
    code = out.rsplit("\n", 1)[-1].strip()
    payload = out.rsplit("\n", 1)[0]
    try:
        payload = json.loads(payload)
    except Exception:
        pass
    return code, payload


# ---------- построение output-элементов одного хода ----------
def item_message(text):
    return {"type": "message", "id": oid("msg"), "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def item_call(call_id, name, args):
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    return {"type": "function_call", "id": oid("fc"), "call_id": call_id,
            "name": name, "arguments": args, "status": "completed"}


def item_result(call_id, text):
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return {"type": "function_call_output", "id": oid("fco"), "call_id": call_id,
            "status": "completed", "output": [{"type": "input_text", "text": text}]}


# ---------- нормализация наших форматов в (role, payload) ----------
def steps_from_internal(trace):
    """step-attribution / booking-трейсы: history со step_idx, type, content_short, tool_name."""
    out = []
    for s in trace.get("history", []):
        role, stype = s.get("role"), (s.get("type") or "")
        text = s.get("content_short") or ""
        if role == "system":
            out.append(("system", text))
        elif role == "user":
            out.append(("user", text))
        elif stype.startswith("tool_call:") or s.get("tool_name"):
            out.append(("call", {"name": s.get("tool_name") or stype.split(":", 1)[-1],
                                 "args": s.get("tool_args") or {}}))
        elif stype.startswith("tool_resp") or role == "tool":
            out.append(("result", {"text": text,
                                   "status": s.get("tool_resp_status")}))
        elif role == "assistant":
            out.append(("assistant", text))
    return out


def steps_from_openai(trace):
    out = []
    for m in trace.get("messages", []):
        role = m.get("role")
        if role in ("system", "user"):
            out.append((role, m.get("content") or ""))
        elif role == "assistant":
            if m.get("content"):
                out.append(("assistant", m["content"]))
            for c in m.get("tool_calls") or []:
                fn = c.get("function", {})
                out.append(("call", {"name": fn.get("name"), "args": fn.get("arguments"),
                                     "id": c.get("id")}))
        elif role == "tool":
            out.append(("result", {"text": m.get("content"), "id": m.get("tool_call_id")}))
    return out


def build_chat(trace, steps, model_id, title, footer):
    msgs, order, parent = {}, [], None

    def add(msg):
        nonlocal parent
        msg["parentId"] = parent
        msg["childrenIds"] = []          # заполняем вручную: merge_history зовётся на update, не на import
        if parent:
            msgs[parent]["childrenIds"].append(msg["id"])
        msgs[msg["id"]] = msg
        order.append(msg["id"])
        parent = msg["id"]

    pending, open_call = [], None        # накопитель output-элементов текущего хода
    ts = int(time.time())

    def flush():
        nonlocal pending
        if not pending:
            return
        add({"id": str(uuid.uuid4()), "role": "assistant", "done": True,
             "model": model_id, "modelName": model_id, "modelIdx": 0,
             "content": "", "output": pending, "timestamp": ts})
        pending = []

    system_text = None
    for kind, payload in steps:
        if kind == "system":
            system_text = system_text or payload
        elif kind == "user":
            flush()
            add({"id": str(uuid.uuid4()), "role": "user", "content": payload, "timestamp": ts})
        elif kind == "assistant":
            if payload.strip():
                pending.append(item_message(payload))
        elif kind == "call":
            open_call = payload.get("id") or oid("call")
            pending.append(item_call(open_call, payload.get("name") or "tool", payload.get("args")))
        elif kind == "result":
            cid = payload.get("id") or open_call or oid("call")
            pending.append(item_result(cid, payload.get("text") or ""))
    flush()

    # футер с провенансом трейса в последнее сообщение (content виден в UI и попадает в feedback)
    if order and footer:
        last = msgs[order[-1]]
        if last["role"] == "assistant":
            last["output"] = (last.get("output") or []) + [item_message(footer)]

    chat = {"title": title, "models": [model_id], "files": [], "tags": [],
            "history": {"currentId": order[-1] if order else None, "messages": msgs},
            "messages": [msgs[i] for i in order]}   # плоский список тоже нужен (models/chats.py)
    if system_text:
        chat["system"] = system_text
    return chat


def summarize(trace):
    """Заголовок и теги: чтобы в списке чатов сразу читалось, что за трейс и чем кончился."""
    tid = trace.get("trace_id") or trace.get("id") or trace.get("task_id") or "trace"
    reward = trace.get("reward")
    outcome = trace.get("outcome") or {}
    ok = outcome.get("grounded_success")
    mark = "OK" if ok else ("FAIL" if ok is False else "?")
    n_calls = trace.get("n_tool_calls")
    title = f"[{mark}] {tid}"
    if reward is not None:
        title += f" · r={reward}"
    tags = [t for t in [
        "rollout",
        f"outcome:{'success' if ok else 'fail'}" if ok is not None else None,
        f"reward:{'zero' if reward == 0 else 'nonzero'}" if reward is not None else None,
        trace.get("action") and f"action:{trace['action']}",
    ] if t]
    footer_bits = [f"trace_id={tid}"]
    if reward is not None:
        footer_bits.append(f"reward={reward}")
    if n_calls is not None:
        footer_bits.append(f"tool_calls={n_calls}")
    if trace.get("target"):
        footer_bits.append("target=" + json.dumps(trace["target"], ensure_ascii=False)[:200])
    if outcome:
        footer_bits.append("outcome=" + json.dumps(outcome, ensure_ascii=False)[:300])
    return title, tags, "\n\n---\n" + " · ".join(footer_bits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--model", default="imported-rollout", help="какой model_id проставить сообщениям")
    ap.add_argument("--folder", default=None, help="имя папки-прогона")
    ap.add_argument("--tag", action="append", default=[], help="доп. тег (можно несколько)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--host", default=HOST)
    a = ap.parse_args()

    lines = [l for l in Path(a.jsonl).expanduser().read_text(encoding="utf-8").splitlines() if l.strip()]
    traces = [json.loads(l) for l in lines[:a.limit]]
    tok = None if a.dry_run else admin_token(a.container)

    folder_id = None
    if a.folder and not a.dry_run:
        code, resp = api("POST", "/api/v1/folders/", tok, {"name": a.folder}, host=a.host)
        folder_id = resp.get("id") if isinstance(resp, dict) else None
        print(f"[folder] {a.folder} -> {code} {folder_id}")

    payload = []
    for tr in traces:
        steps = steps_from_internal(tr) if tr.get("history") else steps_from_openai(tr)
        title, tags, footer = summarize(tr)
        chat = build_chat(tr, steps, a.model, title, footer)
        n_items = sum(len(m.get("output") or []) for m in chat["messages"])
        print(f"[{title}] сообщений={len(chat['messages'])} output-элементов={n_items}")
        if a.dry_run:
            continue
        payload.append({"chat": chat, "folder_id": folder_id,
                        "meta": {"trace_id": tr.get("trace_id"), "reward": tr.get("reward")}})

    if a.dry_run or not payload:
        return
    code, resp = api("POST", "/api/v1/chats/import", tok, {"chats": payload}, host=a.host)
    if code != "200":
        sys.exit(f"import HTTP={code}: {str(resp)[:300]}")
    ids = [c["id"] for c in resp] if isinstance(resp, list) else []
    print(f"[ok] импортировано {len(ids)}")
    for cid, tr in zip(ids, traces):
        _, tags, _ = summarize(tr)
        for t in tags + a.tag:
            api("POST", f"/api/v1/chats/{cid}/tags", tok, {"name": t}, host=a.host)
    if ids:
        print("открыть:", f"{a.host}/c/{ids[0]}")


if __name__ == "__main__":
    main()
