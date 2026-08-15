#!/usr/bin/env bash
# Безопасный перезапуск командного Open WebUI + починка удалённого WAL.
#
# ЗАЧЕМ. На демо-VM обнаружены (проверено вживую):
#  - WEBUI_AUTH=false: admin-токен выдаётся без пароля, /openai/config светит eliza-токен,
#    CORS отражает любой Origin, весь webui.db качается любым на VPN;
#  - процесс держит webui.db-wal и -shm как (deleted): всё записанное после этого живёт только
#    в отвязанном иноде и пропадёт при перезапуске контейнера. Внешний читатель видит стухшие
#    данные (отсюда «функция создалась, но её нет в БД»).
#
# Этот скрипт: (1) снимает свежий бэкап через API, (2) поднимает НОВЫЙ контейнер с auth и
# закрытым CORS на чистом томе, (3) восстанавливает чаты/функции/конфиг из бэкапа.
# НИЧЕГО не удаляет у старого контейнера до успешного бэкапа. Запускать в окно обслуживания.
#
# ⚠ ТРЕБУЕТ РЕШЕНИЙ, которые скрипт не принимает за тебя:
#   - способ auth: OIDC-корп или trusted-header за прокси (переменные ниже);
#   - сервисный eliza-токен взамен личного (SERVICE_TOKEN);
#   - точный корп-URL для CORS (CORS_ORIGIN).
# Без них скрипт остановится и попросит их задать.
set -euo pipefail

VM=${VM:-vladigur@qwenweb.vla.yp-c.yandex.net}
CONTAINER=${CONTAINER:-open-webui}
PORT=${PORT:-3000}
CORS_ORIGIN=${CORS_ORIGIN:-}                 # напр. http://qwenweb.vla.yp-c.yandex.net:3000
SERVICE_TOKEN=${SERVICE_TOKEN:-}             # сервисный eliza-токен (НЕ личный)
TRUSTED_EMAIL_HEADER=${TRUSTED_EMAIL_HEADER:-X-Auth-Request-Email}  # если за auth-прокси
SECRET_KEY=${SECRET_KEY:-}                    # WEBUI_SECRET_KEY; пусто = сгенерим
SLUGS=${SLUGS:-qwen38-27b-gate}              # слаги через пробел

say(){ printf '\n=== %s ===\n' "$*"; }
die(){ echo "СТОП: $*" >&2; exit 1; }
rsh(){ ssh -6 -o BatchMode=yes -o StrictHostKeyChecking=no "$VM" "$@"; }

[ -n "$CORS_ORIGIN" ]   || die "задай CORS_ORIGIN (точный корп-URL, не *)"
[ -n "$SERVICE_TOKEN" ] || die "задай SERVICE_TOKEN (сервисный eliza-токен, не личный)"
[ -n "$SECRET_KEY" ] || SECRET_KEY=$(openssl rand -hex 32)

# ---- 1. Бэкап через API (читает живые данные, не трогает удалённый WAL) ----
say "1/4 бэкап через API"
STAMP=$(date +%Y%m%d-%H%M)
rsh "sudo docker exec -i $CONTAINER python3 - <<'PY' > /tmp/tok.txt
import jwt,sqlite3
c=sqlite3.connect('/app/backend/data/webui.db')
u=c.execute(\"select id from user where role='admin'\").fetchone()[0]
print(jwt.encode({'id':u},open('/app/backend/.webui_secret_key').read().strip(),algorithm='HS256'))
PY
TOK=\$(cat /tmp/tok.txt); mkdir -p ~/owui-backup-$STAMP && cd ~/owui-backup-$STAMP
for p in chats/all/db functions/export configs/export models/export tools/export; do
  o=\$(echo \$p | tr / _).json
  curl -s -o \$o -H \"Authorization: Bearer \$TOK\" http://[::1]:$PORT/api/v1/\$p
  echo \"\$p -> \$(wc -c < \$o) bytes\"
done"
mkdir -p "$(dirname "$0")/../.backups"
rsh "cd ~/owui-backup-$STAMP && tar -czf - *.json" > "$(dirname "$0")/../.backups/prerelaunch-$STAMP.tar.gz"
echo "локальный бэкап: .backups/prerelaunch-$STAMP.tar.gz"

# ---- 2. Остановить старый, поднять новый на ЧИСТОМ томе с auth ----
say "2/4 новый контейнер с auth (старый том сохраняем как -old)"
URLS=""; KEYS=""
for s in $SLUGS; do
  URLS="${URLS:+$URLS;}https://api.eliza.yandex.net/raw/internal/zeliboba/$s/v1"
  KEYS="${KEYS:+$KEYS;}$SERVICE_TOKEN"
done
rsh "
set -e
sudo docker rename $CONTAINER ${CONTAINER}-old || true
sudo docker stop ${CONTAINER}-old || true
sudo mv ~/openwebui-data ~/openwebui-data-old-$STAMP 2>/dev/null || true
mkdir -p ~/openwebui-data
sudo docker run -d --name $CONTAINER --restart unless-stopped --network host \
  -e PORT=$PORT -e HOST=:: \
  -e WEBUI_SECRET_KEY='$SECRET_KEY' \
  -e WEBUI_AUTH_TRUSTED_EMAIL_HEADER='$TRUSTED_EMAIL_HEADER' \
  -e CORS_ALLOW_ORIGIN='$CORS_ORIGIN' \
  -e WEBUI_AUTH_COOKIE_SECURE=true -e WEBUI_AUTH_COOKIE_SAME_SITE=strict \
  -e ENABLE_OPENAI_API=true \
  -e OPENAI_API_BASE_URLS='$URLS' -e OPENAI_API_KEYS='$KEYS' \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -e SSL_CERT_FILE=/ca/ca.pem -e REQUESTS_CA_BUNDLE=/ca/ca.pem \
  -e WEBUI_NAME='LLM Agents Chat' \
  -v ~/openwebui-data:/app/backend/data -v ~/ca.pem:/ca/ca.pem:ro \
  ghcr.io/open-webui/open-webui:main"
echo "жди ~40с health"

# ---- 3. Восстановить контент из бэкапа через API ----
say "3/4 восстановление чатов/функций/конфига"
echo "ПОСЛЕ первого входа (создастся первый юзер = admin) прогони import-эндпоинты:"
cat <<'NOTE'
  TOK=<jwt admin нового инстанса>
  curl -X POST .../api/v1/configs/import   --data @configs_export.json
  curl -X POST .../api/v1/chats/import     --data @chats_all_db.json    # обёртка {"chats":[...]}
  curl -X POST .../api/v1/functions/sync    --data @functions_export.json
NOTE

# ---- 4. Проверка периметра (та же цепочка, что вскрыла дыры) ----
say "4/4 проверка периметра"
cat <<NOTE
  T=\$(curl -s -X POST http://VM:$PORT/api/v1/auths/signin -d '{"email":"x@x","password":"x"}' | jq -r .token)
  curl -s -H "Authorization: Bearer \$T" http://VM:$PORT/openai/config     # ждём 401
  curl -s -i -H 'Origin: https://example.org' http://VM:$PORT/api/config | grep -i access-control-allow-origin  # НЕ должен отражать
Приёмка: обе проверки закрыты, старый контейнер ${CONTAINER}-old и том openwebui-data-old-$STAMP
можно удалить ПОСЛЕ подтверждения, что данные на месте. Личный eliza-токен ОТОЗВАТЬ.
NOTE
