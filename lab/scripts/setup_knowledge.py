#!/usr/bin/env python3
"""
Наша вики как RAG-коллекция в чате: замена Deep Research там, где внешки нет.

Единственная ChatGPT-фича, которой у нас нечем крыть, — веб-поиск и Deep Research:
сеть закрыта. Зато есть своя база знаний (раннбуки, грабли, разборы), и OWUI умеет
искать по ней локально: эмбеддер `all-MiniLM-L6-v2` уже лежит в кэше контейнера,
внешние вызовы не нужны.

Собираем коллекцию, к которой в чате обращаются через `#` (или прицепив её к пресету
модели). Ответы получаются со ссылками на исходный файл — важно, потому что раннбук
устаревает, и надо видеть, откуда взят совет.

Идемпотентно по имени файла: повторный запуск обновляет изменившиеся, не плодя дубли.

Usage:
  ./setup_knowledge.py --list
  ./setup_knowledge.py --apply
  ./setup_knowledge.py --apply --host http://qwenweb.vla.yp-c.yandex.net:3000 \
                       --ssh qwenweb.vla.yp-c.yandex.net
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"

KB_NAME = "Наша вики: инфра, раннбуки, грабли"
KB_DESC = ("Внутренняя база знаний команды: как поднимать сервы и обучение, что уже "
           "ломалось и почему, разборы бенчмарков. Спрашивай через # в чате.")

# Откуда берём документы. Порядок = приоритет при совпадении имён.
SOURCES = [
    (Path.home() / ".claude" / "wiki", "wiki"),
    (Path.home() / "Documents" / "current" / "troubleshooting", "troubleshooting"),
    (Path(__file__).resolve().parents[1] / "docs", "cockpit"),
]
MAX_BYTES = 400_000     # огромные файлы рвут чанкование и забивают top-k мусором


def collect():
    out, seen = [], set()
    for root, tag in SOURCES:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.md")):
            if p.name.upper() == "MEMORY.MD" or p.stat().st_size == 0:
                continue
            if p.stat().st_size > MAX_BYTES:
                print(f"  пропуск (>{MAX_BYTES//1000} КБ): {p}")
                continue
            # Имя в коллекции несёт источник: иначе три README неразличимы.
            rel = p.relative_to(root).as_posix().replace("/", "__")
            name = f"{tag}__{rel}"
            if name in seen:
                continue
            seen.add(name)
            out.append((name, p))
    return out


def api(method, path, tok, host, body=None, raw=False):
    args = ["curl", "-s", "-m", "180", "-w", "\n%{http_code}", "-X", method,
            "-H", f"Authorization: Bearer {tok}", f"{host}{path}"]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
    out = subprocess.run(args, capture_output=True, text=True).stdout
    code, payload = out.rsplit("\n", 1)[-1].strip(), out.rsplit("\n", 1)[0]
    if raw:
        return code, payload
    try:
        return code, json.loads(payload)
    except Exception:
        return code, payload


def upload(path, name, tok, host, sha):
    """
    Заливаем файл под нужным именем. process=true — эмбеддинги считаются сразу.

    Хеш кладём в metadata: по нему следующий запуск поймёт, что файл не менялся.
    Без этого каждый прогон перезаливал бы всё заново, а OWUI на повторную заливку
    того же текста отвечает «Duplicate content detected» — и коллекция остаётся пустой.
    """
    args = ["curl", "-s", "-m", "300", "-w", "\n%{http_code}", "-X", "POST",
            "-H", f"Authorization: Bearer {tok}",
            "-F", f"file=@{path};filename={name};type=text/markdown",
            "-F", f"metadata={json.dumps({'sha': sha, 'lab_source': name})}",
            f"{host}/api/v1/files/?process=true&process_in_background=false"]
    out = subprocess.run(args, capture_output=True, text=True).stdout
    code, payload = out.rsplit("\n", 1)[-1].strip(), out.rsplit("\n", 1)[0]
    try:
        return code, json.loads(payload)
    except Exception:
        return code, payload


def manifest_path(host):
    """Свой учёт «что уже залито»: сервер хеш заливки обратно не отдаёт."""
    key = hashlib.sha1(host.encode()).hexdigest()[:10]
    d = Path.home() / ".cache" / "owui-lab"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"knowledge-{key}.json"


def load_manifest(host):
    p = manifest_path(host)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_manifest(host, data):
    manifest_path(host).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                   encoding="utf-8")


def admin_token(container, ssh):
    inner = ("import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
             "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
             "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),"
             "algorithm='HS256'))")
    cmd = (["ssh", "-o", "StrictHostKeyChecking=no", ssh,
            f"sudo docker exec -i {container} python3 -c {json.dumps(inner)}"] if ssh
           else ["docker", "exec", "-i", container, "python3", "-c", inner])
    return subprocess.check_output(cmd, text=True).strip().splitlines()[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="снести векторы и файлы коллекции и залить заново")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--ssh", default=None)
    ap.add_argument("--host", default=HOST)
    a = ap.parse_args()

    docs = collect()
    if a.list or not a.apply:
        total = sum(p.stat().st_size for _, p in docs)
        for name, p in docs:
            print(f"  {p.stat().st_size:>7} B  {name}")
        print(f"\n{len(docs)} документов, {total/1024:.0f} КБ. Запусти с --apply.")
        return 0
    if not docs:
        print("Нечего заливать: источники пусты.")
        return 1

    tok = admin_token(a.container, a.ssh)

    code, kbs = api("GET", "/api/v1/knowledge/", tok, a.host)
    items = kbs.get("items", kbs) if isinstance(kbs, dict) else kbs
    kb = next((k for k in (items or []) if k.get("name") == KB_NAME), None)
    if kb is None:
        code, kb = api("POST", "/api/v1/knowledge/create", tok, a.host,
                       {"name": KB_NAME, "description": KB_DESC, "data": {},
                        "access_control": None})
        if code != "200" or not isinstance(kb, dict) or "id" not in kb:
            print(f"Не удалось создать коллекцию: HTTP={code} {str(kb)[:200]}")
            return 1
        print(f"Коллекция создана: {kb['id']}")
    else:
        print(f"Коллекция уже есть: {kb['id']}")

    def kb_files():
        _, full = api("GET", f"/api/v1/knowledge/{kb['id']}", tok, a.host)
        out = {}
        for f in (full.get("files") or []) if isinstance(full, dict) else []:
            meta = f.get("meta") or {}
            key = f.get("filename") or meta.get("name")
            out[key] = f
        return out

    def sha_of(entry):
        meta = entry.get("meta") or {}
        return (meta.get("data") or {}).get("sha") or meta.get("sha")

    if a.reset:
        # Проверка дубликата у OWUI смотрит на ВЕКТОРЫ коллекции, а не на таблицу
        # файлов. Если прошлый прогон оборвался, чанки остаются, и любая повторная
        # заливка того же текста получает 400 навсегда. reset чистит именно векторы.
        print("Сбрасываю коллекцию (векторы + файлы)…")
        for nm, f in kb_files().items():
            api("POST", f"/api/v1/knowledge/{kb['id']}/file/remove", tok, a.host,
                {"file_id": f["id"]})
            api("DELETE", f"/api/v1/files/{f['id']}", tok, a.host)
        code, _ = api("POST", f"/api/v1/knowledge/{kb['id']}/reset", tok, a.host)
        print(f"  reset HTTP={code}")

    have = kb_files()
    # Файлы, залитые прошлыми запусками, но выпавшие из коллекции. Их надо удалить
    # физически: OWUI считает хеш содержимого и на повторную заливку того же текста
    # отвечает «Duplicate content detected», из-за чего коллекция остаётся пустой.
    _, all_files = api("GET", "/api/v1/files/", tok, a.host)
    orphans = {}
    for f in (all_files if isinstance(all_files, list) else []):
        nm = f.get("filename") or (f.get("meta") or {}).get("name")
        if nm and nm in {n for n, _ in docs} and nm not in have:
            orphans[nm] = f
    if orphans:
        print(f"Убираю {len(orphans)} осиротевших файлов от прошлых запусков")
        for nm, f in orphans.items():
            api("DELETE", f"/api/v1/files/{f['id']}", tok, a.host)

    manifest = load_manifest(a.host)
    added, skipped, failed = [], [], []
    for name, path in docs:
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
        old = have.get(name)
        # На хеш из ответа сервера полагаться нельзя: metadata заливки в нём не
        # возвращается. Поэтому держим свой манифест рядом со скриптом.
        if old and (manifest.get(name) == digest or sha_of(old) == digest):
            skipped.append(name)
            continue

        # ВАЖЕН ПОРЯДОК: сначала ставим новое, и только при успехе убираем старое.
        # Обратный порядок уже один раз опустошил коллекцию, когда add отвалился.
        code, up = upload(path, name, tok, a.host, digest)
        if code != "200" or not isinstance(up, dict) or "id" not in up:
            failed.append((name, f"upload HTTP={code} {str(up)[:120]}"))
            print(f"  [ПАД] {name}: upload HTTP={code}")
            continue
        code2, resp = api("POST", f"/api/v1/knowledge/{kb['id']}/file/add", tok, a.host,
                          {"file_id": up["id"]})
        if code2 == "200":
            if old:
                api("POST", f"/api/v1/knowledge/{kb['id']}/file/remove", tok, a.host,
                    {"file_id": old["id"]})
                api("DELETE", f"/api/v1/files/{old['id']}", tok, a.host)
            manifest[name] = digest
            added.append(name)
            print(f"  [ok] {name}")
        elif "Duplicate content" in str(resp):
            # Этот текст уже проиндексирован. Не ошибка: убираем лишнюю копию файла
            # и запоминаем хеш, чтобы следующий прогон сюда вообще не заходил.
            api("DELETE", f"/api/v1/files/{up['id']}", tok, a.host)
            manifest[name] = digest
            skipped.append(name)
        else:
            api("DELETE", f"/api/v1/files/{up['id']}", tok, a.host)
            failed.append((name, f"add HTTP={code2} {str(resp)[:100]}"))
            print(f"  [ПАД] {name}: add HTTP={code2}")
    save_manifest(a.host, manifest)

    print(f"\nЗалито {len(added)}, без изменений {len(skipped)}, ошибок {len(failed)}")
    for n, why in failed:
        print(f"  ПАД {n}: {why}")
    if added:
        print(f"\nВ чате набери # и выбери «{KB_NAME}».")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
