#!/usr/bin/env bash
# Установить кастомную OWUI-тулзу и привязать её к модели поверх нашего слага.
# Проверено на Open WebUI 0.11.0: тулза реально вызывается в UI (tool-loop исполняет OWUI).
# Usage: ./install_tool.sh <tool_file.py> <tool_id> <base_model_slug> [container] [host]
set -euo pipefail
TOOL_FILE=${1:?путь к py-файлу с классом Tools}
TOOL_ID=${2:?id тулзы, напр. magic_tool}
BASE_MODEL=${3:?слаг базовой модели, напр. qwen38-27b-gate}
CONTAINER=${4:-open-webui}
HOST=${5:-http://[::1]:3000}
MODEL_ID="${BASE_MODEL}-tools"

# JWT админа: секрет лежит в ФАЙЛЕ, в env он пустой (create_token в отдельном процессе упадёт)
TOK=$(docker exec -i "$CONTAINER" python3 - <<'PY'
import jwt, sqlite3
c = sqlite3.connect('/app/backend/data/webui.db')
uid = c.execute("select id from user where role='admin'").fetchone()[0]
print(jwt.encode({'id': uid}, open('/app/backend/.webui_secret_key').read().strip(), algorithm='HS256'))
PY
)

TMP=$(mktemp -d)
python3 - "$TOOL_FILE" "$TOOL_ID" "$TMP/tool.json" <<'PY'
import json, sys
src, tid, out = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({"id": tid, "name": tid, "meta": {"description": f"custom tool {tid}"},
           "content": open(src).read()}, open(out, "w"))
PY
# 1) создать тулзу (OWUI сам посчитает specs из type-hints + docstring), а если она уже
#    есть — ОБНОВИТЬ. Раньше здесь был только create: повторный запуск получал HTTP 400,
#    скрипт бодро писал «Готово», а в контейнере оставался старый код тулзы.
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  "$HOST/api/v1/tools/create" --data @"$TMP/tool.json")
if [ "$code" = "200" ]; then
  echo "tool create HTTP=$code"
else
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    "$HOST/api/v1/tools/id/$TOOL_ID/update" --data @"$TMP/tool.json")
  echo "tool update HTTP=$code"
  [ "$code" = "200" ] || { echo "НЕ УСТАНОВИЛОСЬ: ни create, ни update"; exit 1; }
fi

# 2) workspace-модель поверх слага с привязанной тулзой + native function calling
python3 - "$MODEL_ID" "$BASE_MODEL" "$TOOL_ID" "$TMP/model.json" <<'PY'
import json, sys
mid, base, tid, out = sys.argv[1:5]
json.dump({"id": mid, "base_model_id": base, "name": f"{base} + tools",
           "meta": {"toolIds": [tid], "description": f"{base} с тулзой {tid}"},
           "params": {"function_calling": "native"}}, open(out, "w"))
PY
mcode=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  "$HOST/api/v1/models/create" --data @"$TMP/model.json")
echo "model create HTTP=$mcode (400/401 = уже есть, это нормально)"

# 3) ОБЯЗАТЕЛЬНО: без рефреша модель не резолвится ("Model not found")
curl -sS -o /dev/null -w 'models refresh HTTP=%{http_code}\n' \
  -H "Authorization: Bearer $TOK" "$HOST/api/models?refresh=true"
rm -rf "$TMP"

echo "Готово. В UI выбери модель '$MODEL_ID' (у поля ввода загорится Available Tools)."
echo "ПРОВЕРЯТЬ В БРАУЗЕРЕ: curl /api/chat/completions = passthrough, он вернёт tool_calls СЫРЫМИ без исполнения."
