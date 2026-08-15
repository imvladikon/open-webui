#!/usr/bin/env bash
# Установить Function (pipe/filter/action) в работающий Open WebUI и включить её.
#
# Почему не INSERT в sqlite: штатный create прогоняет replace_imports, вытаскивает
# frontmatter в meta.manifest и, главное, function.is_active по умолчанию FALSE
# (models/functions.py) - при ручном INSERT без is_active=1 функция просто не появится.
#
# Usage: ./install_function.sh <file.py> <id> <type: filter|pipe|action> [global] [container] [host]
#   global=1 для фильтра означает "применять ко всем моделям"
set -euo pipefail
FILE=${1:?путь к py-файлу}
FID=${2:?id функции}
FTYPE=${3:?filter|pipe|action}
GLOBAL=${4:-0}
CONTAINER=${5:-open-webui}
HOST=${6:-http://[::1]:3000}

TOK=$(docker exec -i "$CONTAINER" python3 - <<'PY'
import jwt, sqlite3
c = sqlite3.connect('/app/backend/data/webui.db')
uid = c.execute("select id from user where role='admin'").fetchone()[0]
print(jwt.encode({'id': uid}, open('/app/backend/.webui_secret_key').read().strip(), algorithm='HS256'))
PY
)

TMP=$(mktemp -d)
python3 - "$FILE" "$FID" "$FTYPE" "$TMP/fn.json" <<'PY'
import json, re, sys
src, fid, ftype, out = sys.argv[1:5]
code = open(src, encoding='utf-8').read()
title = (re.search(r'^title:\s*(.+)$', code, re.M) or [None, fid])[1].strip()
desc = (re.search(r'^description:\s*(.+)$', code, re.M) or [None, ''])[1].strip()
json.dump({"id": fid, "name": title, "type": ftype,
           "content": code, "meta": {"description": desc[:300]}},
          open(out, "w"), ensure_ascii=False)
PY

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  "$HOST/api/v1/functions/create" --data @"$TMP/fn.json")
echo "create HTTP=$code"
if [ "$code" != "200" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    "$HOST/api/v1/functions/id/$FID/update" --data @"$TMP/fn.json")
  echo "update HTTP=$code"
fi

# Включить (is_active) и, если просили, сделать глобальной.
# ВАЖНО: /toggle и /toggle/global это ПЕРЕКЛЮЧАТЕЛИ, а не "включить". Повторный запуск
# скрипта на уже включённой функции выключил бы её. Поэтому сверяем текущее состояние
# и дёргаем ручку только при расхождении.
STATE=$(docker exec -i "$CONTAINER" python3 - <<PY
import sqlite3
c = sqlite3.connect('/app/backend/data/webui.db')
r = c.execute("select is_active,is_global from function where id=?", ("$FID",)).fetchone()
print(f"{int(r[0] or 0)} {int(r[1] or 0)}" if r else "0 0")
PY
)
CUR_ACTIVE=$(echo "$STATE" | cut -d' ' -f1)
CUR_GLOBAL=$(echo "$STATE" | cut -d' ' -f2)
if [ "$CUR_ACTIVE" != "1" ]; then
  curl -s -o /dev/null -w "activate HTTP=%{http_code}\n" -X POST \
    -H "Authorization: Bearer $TOK" "$HOST/api/v1/functions/id/$FID/toggle"
else
  echo "already active"
fi
if [ "$GLOBAL" = "1" ] && [ "$CUR_GLOBAL" != "1" ]; then
  curl -s -o /dev/null -w "make-global HTTP=%{http_code}\n" -X POST \
    -H "Authorization: Bearer $TOK" "$HOST/api/v1/functions/id/$FID/toggle/global"
elif [ "$GLOBAL" = "1" ]; then
  echo "already global"
fi

docker exec -i "$CONTAINER" python3 - <<PY
import sqlite3
c = sqlite3.connect('/app/backend/data/webui.db')
r = c.execute("select id,type,is_active,is_global from function where id=?", ("$FID",)).fetchone()
print("db row:", r if r else "MISSING")
PY
curl -s -o /dev/null -H "Authorization: Bearer $TOK" "$HOST/api/models?refresh=true"
rm -rf "$TMP"
echo "готово. Фильтр применяется, если is_global=1, либо если он указан в meta.filterIds модели."
