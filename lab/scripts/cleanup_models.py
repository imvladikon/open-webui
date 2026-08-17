#!/usr/bin/env python3
"""
Убрать из селектора моделей всё, что моделью не является.

ПРОБЛЕМА. Пайпы и служебные пресеты попадают в тот же список, что и настоящие
чекпойнты: Auto-continue, Project Generator, Serve Status, GPU Status, A/B Compare,
плюс технический `<слаг>-tools` от install_tool.sh. В итоге выбор модели работал как
меню режимов, и человек «выбирал модель», чтобы посмотреть статус сервов, а потом
оказывался на пайпе, который не умеет вызывать инструменты, и не мог получить файл.

ЧТО ВМЕСТО. Всё, что не про «какие веса отвечают», переехало:
  * статус сервов, свободные GPU, сравнение чекпойнтов → инструмент `lab_infra`;
  * неограниченная длина ответа → переключатель `long_output` у поля ввода;
  * генерация файлов → инструмент `make_document`.
Инструмент и переключатель доступны ЛЮБОЙ модели и не рвут диалог.

Скрипт прячет лишнее (`is_active=0`), а не удаляет: пайп остаётся в Функциях, и его
можно вернуть одной галкой. Настоящие пайпы-харнессы (песочница) остаются видимыми.

Usage:
  ./cleanup_models.py            # показать, что будет скрыто
  ./cleanup_models.py --apply
"""
import argparse
import json
import subprocess
import sys

CONTAINER = "open-webui"

# Псевдо-модели: их функциональность теперь доступна как опция любой модели.
HIDE_PIPES = ["autocontinue", "serve_status", "gpu_status", "ab_compare"]
# Технические пресеты, которые создаёт install_tool.sh «для проверки в UI».
HIDE_MODELS_SUFFIX = "-tools"


def run(container, script, payload=None):
    p = subprocess.run(["docker", "exec", "-i", container, "python3", "-c", script],
                       input=payload, capture_output=True, text=True)
    return p.stdout.strip(), p.stderr.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--restore", action="store_true", help="вернуть всё обратно")
    a = ap.parse_args()

    script = f"""
import sqlite3, json
c = sqlite3.connect('/app/backend/data/webui.db')
hide_pipes = {HIDE_PIPES!r}
apply = {bool(a.apply)!r}
restore = {bool(a.restore)!r}
target = 1 if restore else 0
report = {{'pipes': [], 'models': []}}
for fid, in c.execute("select id from function where type='pipe'"):
    if fid in hide_pipes:
        report['pipes'].append(fid)
        if apply:
            c.execute("update function set is_active=?, is_global=0 where id=?", (target, fid))
for mid, in c.execute("select id from model"):
    if mid.endswith({HIDE_MODELS_SUFFIX!r}):
        report['models'].append(mid)
        if apply:
            c.execute("update model set is_active=? where id=?", (target, mid))
if apply:
    c.commit()
print(json.dumps(report, ensure_ascii=False))
"""
    out, err = run(a.container, script)
    line = [l for l in out.splitlines() if l.startswith("{")]
    if not line:
        print("Не удалось прочитать состояние:", err[-400:] or out[-400:])
        return 1
    rep = json.loads(line[-1])
    verb = "возвращаю" if a.restore else ("скрываю" if a.apply else "будет скрыто")
    print(f"{verb} пайпов: {len(rep['pipes'])}")
    for p in rep["pipes"]:
        print(f"    {p}")
    print(f"{verb} технических пресетов: {len(rep['models'])}")
    for m in rep["models"]:
        print(f"    {m}")
    if not a.apply and not a.restore:
        print("\nЭто предпросмотр. Запусти с --apply.")
        return 0
    print("\nГотово. Тот же функционал теперь: инструмент lab_infra (статус, GPU, сравнение), "
          "инструмент make_document (файлы), переключатель «Длинный вывод» у поля ввода.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
