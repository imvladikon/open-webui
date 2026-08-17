#!/usr/bin/env python3
"""
Поиск по нашей вике на русском: замена англоязычного эмбеддера + честный замер качества.

ЧТО БЫЛО НЕ ТАК (нашли замером, не на глаз). Из коробки OWUI берёт
`all-MiniLM-L6-v2` — модель ТОЛЬКО ДЛЯ АНГЛИЙСКОГО. Наши документы и вопросы русские,
поэтому поиск работал случайно: тот же вопрос про eliza по-английски находил нужный
раннбук, а по-русски выдавал ROADMAP. Дословная русская фраза свой документ находила
(совпали токены), осмысленный русский вопрос — нет.

ВТОРАЯ ГРАБЛЯ. В образе стоит `HF_HUB_OFFLINE=1`, поэтому смена модели через UI/API
НИЧЕГО не скачивает и тихо оставляет систему БЕЗ эмбеддера. Хуже того, `/knowledge/reindex`
в этом состоянии отвечает `true`, хотя по логам падают все файлы до одного. Поэтому
модель кладём в кэш контейнера снаружи (`--stage`), и только потом переключаем.

ТРЕТЬЕ. Смена модели требует ПЕРЕиндексации: старые векторы посчитаны другой моделью
и другой размерности.

Usage:
  ./setup_rag.py --check                 # какой эмбеддер стоит + прогнать eval
  ./setup_rag.py --stage                 # скачать модель локально и положить в контейнер
  ./setup_rag.py --apply                 # переключить эмбеддер и переиндексировать
  ./setup_rag.py --stage --apply --check --ssh qwenweb.vla.yp-c.yandex.net \
                 --host http://qwenweb.vla.yp-c.yandex.net:3000
"""
import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

CONTAINER = "open-webui"
HOST = "http://[::1]:3000"
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CACHE = "/app/backend/data/cache/embedding/models"
KB_NAME = "Наша вики: инфра, раннбуки, грабли"

# Мини-бенчмарк поиска. Для каждого вопроса ответ ТОЧНО есть в указанном файле —
# проверено грепом по вике, а не на глаз. Без такого набора любое изменение
# настроек RAG обсуждается вслепую.
EVAL = [
    ("не пускает по ssh на свежую виртуалку, permission denied publickey",
     "troubleshooting__qyp-vm-ssh-permission-denied.md"),
    ("почему ган падает с exit code 37",
     "wiki__runbooks__training-ops-diagnostics.md"),
    ("как запустить SFT обучение",
     "wiki__runbooks__run-sft.md"),
    ("что такое мёртвые группы в GRPO",
     ("wiki__domain.md", "wiki__runbooks__trace-rca.md", "wiki__gotchas.md")),
    ("как ускорить обучение, переиспользование logprob",
     ("wiki__runbooks__throughput-optimization.md", "wiki__README.md", "wiki__gotchas.md")),
    ("сегменты infiniband DEGRADED, ганги мрут",
     ("wiki__runbooks__training-ops-diagnostics.md", "wiki__infra-guide.md",
      "wiki__README.md", "wiki__gotchas.md")),
    # Эти два раздела дописаны в вику после того, как eval показал, что ответа нет.
    ("как отдать свой чекпойнт слагом в элизе",
     "wiki__runbooks__eliza.md"),
    ("деплой RL-env поднимает только 2 сервера, остальные висят без фейла",
     "wiki__gotchas.md"),
]


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def dexec(container, ssh, argv, timeout=600):
    if ssh:
        quoted = " ".join(__import__("shlex").quote(x) for x in argv)
        return sh(["ssh", "-o", "StrictHostKeyChecking=no", ssh,
                   f"sudo docker exec -i {container} {quoted}"], timeout=timeout)
    return sh(["docker", "exec", "-i", container] + argv, timeout=timeout)


def admin_token(container, ssh):
    inner = ("import jwt,sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');"
             "u=c.execute(\"select id from user where role='admin'\").fetchone()[0];"
             "print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),"
             "algorithm='HS256'))")
    r = dexec(container, ssh, ["python3", "-c", inner])
    return r.stdout.strip().splitlines()[-1]


def api(method, path, tok, host, body=None, timeout=1800):
    args = ["curl", "-s", "-m", str(timeout), "-w", "\n%{http_code}", "-X", method,
            "-H", f"Authorization: Bearer {tok}", f"{host}{path}"]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
    out = sh(args, timeout=timeout + 30).stdout
    code, payload = out.rsplit("\n", 1)[-1].strip(), out.rsplit("\n", 1)[0]
    try:
        return code, json.loads(payload)
    except Exception:
        return code, payload


def current_model(tok, host):
    """Только через API: модуль config отдаёт значение на момент импорта, а не живое."""
    code, d = api("GET", "/api/v1/retrieval/embedding", tok, host, timeout=60)
    if isinstance(d, dict):
        return d.get("RAG_EMBEDDING_MODEL") or "(пусто)"
    return f"не смог прочитать (HTTP={code})"


def stage(container, ssh, workdir):
    """Скачать модель там, где есть интернет, и положить в кэш контейнера."""
    py = str(Path.home() / ".claude" / "venv" / "bin" / "python3")
    py = py if Path(py).exists() else sys.executable
    print(f"Качаю {MODEL} ({py})…")
    code = (
        "import os; os.environ.pop('HF_HUB_OFFLINE', None)\n"
        "from huggingface_hub import snapshot_download\n"
        f"p = snapshot_download({MODEL!r}, cache_dir={workdir!r},\n"
        "    allow_patterns=['*.json','*.txt','*.model','*.safetensors','1_Pooling/*'],\n"
        "    ignore_patterns=['*.onnx','*.ot','*.h5','onnx*','openvino*','*.bin'])\n"
        "print(p)")
    r = sh([py, "-c", code], timeout=1800)
    if r.returncode != 0:
        print("Не удалось скачать модель:\n" + (r.stderr or "")[-800:])
        return False
    repo_dir = Path(workdir) / ("models--" + MODEL.replace("/", "--"))
    if not repo_dir.exists():
        print(f"Ожидал {repo_dir}, его нет")
        return False
    size = sum(f.stat().st_size for f in repo_dir.rglob("*") if f.is_file())
    print(f"Скачано {size/1e6:.0f} МБ, кладу в контейнер…")

    # docker cp умеет читать tar со stdin — так работает и локально, и через ssh.
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
        # dereference=False обязателен: в кэше HF snapshots/ — это симлинки в blobs/.
        # С dereference=True каждый файл уезжает дважды и архив раздувается вдвое.
        with tarfile.open(tf.name, "w", dereference=False) as tar:
            tar.add(repo_dir, arcname=repo_dir.name)
        tar_path = tf.name
    try:
        if ssh:
            cmd = (f"cat > /tmp/ef.tar && sudo docker cp /tmp/ef.tar "
                   f"{container}:/tmp/ef.tar && rm -f /tmp/ef.tar && "
                   f"sudo docker exec -i {container} tar -xf /tmp/ef.tar -C {CACHE} && "
                   f"sudo docker exec -i {container} rm -f /tmp/ef.tar")
            with open(tar_path, "rb") as fh:
                r = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", ssh, cmd],
                                   stdin=fh, capture_output=True, text=True, timeout=3600)
        else:
            r = sh(["docker", "cp", str(repo_dir), f"{container}:{CACHE}/"], timeout=1800)
        if r.returncode != 0:
            print("Копирование не прошло:\n" + (r.stderr or "")[-600:])
            return False
    finally:
        os.unlink(tar_path)
    chk = dexec(container, ssh, ["sh", "-c", f"du -sh {CACHE}/{repo_dir.name}"])
    print("В контейнере: " + chk.stdout.strip())
    return True


# Дефолтный RAG-шаблон — 1445 символов английских инструкций, и модель их пересказывает
# вслух вместо ответа. Свой короче и по-русски. TOP_K=3 на нашей плотной вике режет
# раздел пополам: симптом попадает в выдачу, а способ починки уже нет.
RAG_TEMPLATE = """Ответь на вопрос пользователя по фрагментам ниже.

Правила:
- Отвечай сразу итогом, на русском. НЕ рассуждай вслух, не пересказывай эти правила.
- Ставь ссылку [id] после утверждения, если у <source> есть атрибут id.
- Если во фрагментах ответа нет, скажи это одной строкой и не выдумывай.

<context>
{{CONTEXT}}
</context>"""
TUNING = {"TOP_K": 5, "CHUNK_SIZE": 2000, "CHUNK_OVERLAP": 200,
          "RAG_TEMPLATE": RAG_TEMPLATE}


def tune(tok, host):
    code, d = api("POST", "/api/v1/retrieval/config/update", tok, host, TUNING, timeout=120)
    got = {k: (d or {}).get(k) for k in ("TOP_K", "CHUNK_SIZE", "CHUNK_OVERLAP")} \
        if isinstance(d, dict) else {}
    print(f"Настройки поиска: HTTP={code} {got}")
    return code == "200"


def apply(container, ssh, tok, host):
    code, _ = api("POST", "/api/v1/retrieval/embedding/update", tok, host,
                  {"RAG_EMBEDDING_ENGINE": "", "RAG_EMBEDDING_MODEL": MODEL,
                   "RAG_EMBEDDING_BATCH_SIZE": 16})
    print(f"Смена эмбеддера: HTTP={code}")
    print("В конфиге теперь:", current_model(tok, host))
    print("Переиндексация…")
    # В логах лежат ошибки прошлых попыток, и `docker logs --tail N` выдаёт их за
    # свежие. Отсечка по времени тоже подвела: `--since` без таймзоны docker читает
    # как локальное время. Поэтому считаем строки ДО и ПОСЛЕ — часы вообще не нужны.
    before = log_count(container, ssh, "No embedding model is loaded")
    code, _ = api("POST", "/api/v1/knowledge/reindex", tok, host)
    # Реиндекс отвечает true даже когда упали ВСЕ файлы, поэтому верим только логам.
    fails = log_count(container, ssh, "No embedding model is loaded") - before
    print(f"Реиндекс: HTTP={code}"
          + ("  ⚠ в логах «No embedding model is loaded» — модель НЕ загрузилась"
             if fails else "  (ошибок в логах нет)"))
    # Пустая коллекция при живом поиске — реальный сценарий: векторы переживают
    # файлы, и по ответам подмены не видно. Поэтому сверяем счётчик, а не выдачу.
    left = kb_file_count(tok, host)
    if left == 0:
        print("  ⚠ в коллекции 0 файлов, поиск идёт по осиротевшим векторам. "
              "Чини: ./setup_knowledge.py --apply --reset")
        return False
    print(f"  файлов в коллекции: {left}")
    return not fails


def log_count(container, ssh, needle):
    cmd = f"docker logs {container} 2>&1 | grep -c '{needle}' || true"
    if ssh:
        r = sh(["ssh", "-o", "StrictHostKeyChecking=no", ssh, "sudo " + cmd])
    else:
        r = sh(["sh", "-c", cmd])
    try:
        return int((r.stdout or "0").strip().splitlines()[-1])
    except Exception:
        return 0


def kb_file_count(tok, host):
    code, kbs = api("GET", "/api/v1/knowledge/", tok, host, timeout=60)
    items = kbs.get("items", kbs) if isinstance(kbs, dict) else kbs
    kb = next((k for k in (items or []) if k.get("name") == KB_NAME), None)
    if not kb:
        return 0
    # Список файлов — отдельная ручка; в `GET /knowledge/{id}` его нет.
    code, resp = api("GET", f"/api/v1/knowledge/{kb['id']}/files", tok, host, timeout=60)
    items = resp.get("items", resp) if isinstance(resp, dict) else resp
    return len(items) if isinstance(items, list) else 0


def evaluate(tok, host):
    code, kbs = api("GET", "/api/v1/knowledge/", tok, host)
    items = kbs.get("items", kbs) if isinstance(kbs, dict) else kbs
    kb = next((k for k in (items or []) if k.get("name") == KB_NAME), None)
    if not kb:
        print(f"Коллекции «{KB_NAME}» нет — сначала ./setup_knowledge.py --apply")
        return 1
    hit = 0
    n = kb_file_count(tok, host)
    warn = "  ⚠ файлов 0: ищет по осиротевшим векторам" if n == 0 else ""
    print(f"\nEval поиска ({len(EVAL)} вопросов, топ-3; файлов в коллекции: {n}){warn}")
    for q, want in EVAL:
        want = (want,) if isinstance(want, str) else want
        code, d = api("POST", "/api/v1/retrieval/query/collection", tok, host,
                      {"collection_names": [kb["id"]], "query": q, "k": 3})
        if not isinstance(d, dict) or "documents" not in d:
            print(f"  [ПАД ] {q[:52]:<52} ← {str(d)[:70]}")
            continue
        names = [(m or {}).get("name") for m in (d.get("metadatas") or [[]])[0]]
        scores = (d.get("distances") or [[]])[0]
        ok = any(n in want for n in names)
        hit += ok
        top = f"{names[0]} {scores[0]:.3f}" if names else "пусто"
        print(f"  [{'ok  ' if ok else 'мимо'}] {q[:52]:<52} → {top}")
        if not ok:
            print(f"          ждали: {', '.join(want)}")
    print(f"\nПопаданий в топ-3: {hit}/{len(EVAL)}")
    return 0 if hit == len(EVAL) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--ssh", default=None)
    ap.add_argument("--host", default=HOST)
    a = ap.parse_args()
    if not (a.stage or a.apply or a.check):
        ap.print_help()
        return 0

    tok = admin_token(a.container, a.ssh)
    print("Текущий эмбеддер:", current_model(tok, a.host))

    if a.stage:
        with tempfile.TemporaryDirectory() as wd:
            if not stage(a.container, a.ssh, wd):
                return 1
    if a.apply:
        tune(tok, a.host)
        if not apply(a.container, a.ssh, tok, a.host):
            return 1
    if a.check:
        return evaluate(tok, a.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
