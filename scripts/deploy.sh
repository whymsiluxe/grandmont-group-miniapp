#!/bin/bash
# Grandmont Group miniapp deploy -- Release-аудит Этап 8 + доп.раунд 31.07. Полный цикл:
# чистота репо -> тесты (в изолированном env, БЕЗ production credentials) -> syntax ->
# backup -> копирование в production paths -> restart -> health-проверка.
#
# 31.07 (атомарность): backend всё ещё копируется на прежние serving-пути (смена на
# staged-dir+symlink потребовала бы менять systemd unit -- WorkingDirectory сейчас
# фиксированный /home/grandmont/agent/miniapp, ExecStart=uvicorn miniapp.main:app; такой
# рефакторинг вне рамок этой задачи). Вместо этого: `trap` с момента, когда backup готов
# и МОГ БЫ понадобиться -- любая ошибка на шагах copy/restart/health триггерит
# автоматический scripts/rollback.sh на этом backup, без ручного вмешательства. Это не
# полная atomic-symlink-гарантия, но закрывает главный риск: backend/frontend/VERSION
# от РАЗНЫХ SHA после упавшего деплоя.
#
# Запуск: на VPS, из корня репозитория (/home/grandmont/agent/miniapp-repo), как
# пользователь grandmont -- НЕ через sudo/root:
#   bash scripts/deploy.sh
# (10.09, deploy permissions fix; 26.09 Wave F, deployer switched promonta -> grandmont:
# /var/www/miniapp -- root:webdeploy 2775 setgid, deployer is a member of webdeploy
# group, пишет туда напрямую без sudo. До этой правки
# скрипт требовал root целиком из-за frontend-шага; тот узкий сценарий больше не
# нужен -- не запускать через sudo по старой памяти, это лишний root-доступ без
# необходимости.)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Backend artifact manifest (single source of truth) + runtime helper functions
# shellcheck source=manifest.sh
source "$REPO_DIR/scripts/manifest.sh"
# shellcheck source=runtime_manifest.sh
source "$REPO_DIR/scripts/runtime_manifest.sh"

# Production paths -- НЕ угадано, сверено с реальным systemd unit
# (/etc/systemd/system/grandmont-miniapp.service, WorkingDirectory=/home/promonta/agent,
# ExecStart=uvicorn miniapp.main:app) и реальной раздачей frontend через Caddy
# (/var/www/miniapp/) на момент написания этого скрипта.
BACKEND_SERVING_DIR="/home/grandmont/agent/miniapp"
FRONTEND_SERVING_DIR="/var/www/miniapp"
SERVICE_NAME="grandmont-group-miniapp.service"
HEALTH_URL="https://app.promonta.fun/api/health"
HEALTH_READY_URL_LOCAL="http://127.0.0.1:8001/api/health/ready"

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="/tmp/rollback_backup_${TS}"

# 31.07: флаг + trap -- как только backup готов (шаг 7), любой exit с ошибкой (код != 0)
# на ПОСЛЕДУЮЩИХ шагах (copy/restart/health) автоматически откатывает на этот backup.
# До готовности backup (шаги 1-7) trap ничего не делает -- нечего откатывать, ошибка
# там просто означает "деплой не начинался", production не тронут.
BACKUP_READY=0
DEPLOY_FAILED_AND_ROLLED_BACK=0

_auto_rollback_on_failure() {
  local exit_code=$?
  if [[ $exit_code -eq 0 ]]; then
    return
  fi
  if [[ "$BACKUP_READY" != "1" ]]; then
    echo "ОШИБКА на шаге до готовности backup -- production не тронут, откат не нужен." >&2
    return
  fi
  echo "" >&2
  echo "!!! ДЕПЛОЙ УПАЛ (код $exit_code) -- ЗАПУСКАЮ АВТОМАТИЧЕСКИЙ ОТКАТ на $BACKUP_DIR !!!" >&2
  if "$REPO_DIR/scripts/rollback.sh" "$BACKUP_DIR"; then
    DEPLOY_FAILED_AND_ROLLED_BACK=1
    echo "ОТКАТ ВЫПОЛНЕН УСПЕШНО -- production восстановлен на предыдущую версию." >&2
  else
    echo "!!! ОТКАТ ТОЖЕ ПРОВАЛИЛСЯ -- ТРЕБУЕТСЯ РУЧНОЕ ВМЕШАТЕЛЬСТВО. Backup: $BACKUP_DIR !!!" >&2
  fi
}
trap _auto_rollback_on_failure EXIT

echo "== 1/14 Проверка: работаем из git-репозитория =="
if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "ОШИБКА: $REPO_DIR не является git-репозиторием" >&2
  exit 1
fi
echo "OK: $REPO_DIR"

echo "== 2/14 Проверка ветки и SHA =="
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
CURRENT_SHA="$(git rev-parse HEAD)"
if [[ "$CURRENT_BRANCH" != "main" ]]; then
  echo "ОШИБКА: деплой разрешён только с ветки main, сейчас на '$CURRENT_BRANCH'" >&2
  exit 1
fi
echo "OK: branch=main, SHA=$CURRENT_SHA"

echo "== 3/14 Проверка чистого git status =="
if [[ -n "$(git status --porcelain)" ]]; then
  echo "ОШИБКА: есть незакоммиченные изменения -- закоммить или stash перед деплоем" >&2
  git status --short
  exit 1
fi
echo "OK: working tree чист"

echo "== 4/14 Python syntax-check =="
python3 -m compileall -q backend tests
echo "OK"

echo "== 5/14 node --check (recursive frontend/js + backend runtime JS) =="
find frontend/js -name '*.js' -print0 | xargs -0 -n1 node --check
backend_runtime_syntax_check "$REPO_DIR/backend"
echo "OK"

echo "== 6/14 Полный test suite (изолированный env, БЕЗ production credentials) =="
# 31.07 (Release-аудит, доп.раунд): раньше тесты запускались с ПОЛНЫМ
# /etc/claude-agent.env -- реальные Google Sheets/Telegram/AI credentials попадали в
# окружение тестового процесса без необходимости (тесты мокают все внешние вызовы
# через patch.object, ни один реальный ключ им не нужен). Теперь: только dummy
# BOT_TOKEN + временный MINIAPP_DATA_ROOT.
# 31.07 (доп.раунд, П4): изоляция через MINIAPP_DATA_ROOT теперь РЕАЛЬНАЯ, не только
# по факту test-mocking -- все runtime JSON-пути в main.py/roadmap_lib.py/mangel_lib.py
# переведены на os.path.join(DATA_ROOT, ...), подтверждено tests/test_data_root_isolation.py
# (реальные файловые операции с MINIAPP_DATA_ROOT=/tmp/..., прод-директория не читается
# и не изменяется). Тесты физически не могут писать в /home/grandmont/agent/miniapp или
# /var/www/miniapp даже по ошибке -- это больше не полагается только на то, что каждый
# тест правильно замокал _load_*/_save_*.
TEST_PYTHON="${BACKEND_SERVING_DIR}/.venv/bin/python3"
if [[ ! -x "$TEST_PYTHON" ]]; then
  echo "ОШИБКА: $TEST_PYTHON не найден -- venv сервиса недоступен" >&2
  exit 1
fi
TEST_DATA_ROOT="$(mktemp -d)"
BOT_TOKEN="ci-dummy-token-not-a-real-secret" MINIAPP_DATA_ROOT="$TEST_DATA_ROOT" \
  "$TEST_PYTHON" -m pytest tests/ -q
rm -rf "$TEST_DATA_ROOT"
echo "OK: тесты прошли (изолированный env, prod credentials не использовались)"

echo "== 7/14 Проверка pdfkit в serving environment =="
# 31.07 (доп.раунд, П6): pdfkit ставится вручную в BACKEND_SERVING_DIR/node_modules
# (нет package-lock в этой директории на VPS, не repo checkout) -- если его там нет,
# angebot_free.js/rechnung.js падают в рантайме на ПЕРВОМ реальном запросе Angebot/
# Rechnung PDF уже ПОСЛЕ деплоя. Проверяем ДО backup/copy -- останавливаем деплой
# раньше, с понятной инструкцией, а не оставляем прод в состоянии с рабочим backend,
# но сломанной генерацией PDF.
if [[ -d "${BACKEND_SERVING_DIR}/node_modules/pdfkit" ]]; then
  if ! node -e "require('${BACKEND_SERVING_DIR}/node_modules/pdfkit'); console.log('pdfkit OK')"; then
    echo "ОШИБКА: pdfkit найден в ${BACKEND_SERVING_DIR}/node_modules, но require() падает -- проверь установку." >&2
    echo "Исправление: cd ${BACKEND_SERVING_DIR} && npm ci --omit=dev" >&2
    exit 1
  fi
  echo "OK: pdfkit доступен в serving environment"
else
  echo "ОШИБКА: pdfkit не найден в ${BACKEND_SERVING_DIR}/node_modules -- Angebot/Rechnung PDF не будут работать после деплоя." >&2
  echo "Исправление: cd ${BACKEND_SERVING_DIR} && npm ci --omit=dev (используя package.json/package-lock.json из репозитория)" >&2
  exit 1
fi

echo "== 8/14 Создание timestamped backup =="
mkdir -p "$BACKUP_DIR"
backend_runtime_backup "$BACKEND_SERVING_DIR" "$BACKUP_DIR"
if [[ -f "${BACKEND_SERVING_DIR}/VERSION" ]]; then
  cp "${BACKEND_SERVING_DIR}/VERSION" "${BACKUP_DIR}/VERSION"
else
  touch "${BACKUP_DIR}/.VERSION_ABSENT"
fi
if [[ -d "$FRONTEND_SERVING_DIR" ]]; then
  mkdir -p "${BACKUP_DIR}/frontend"
  rsync -a "${FRONTEND_SERVING_DIR}/" "${BACKUP_DIR}/frontend/"
fi
echo "== 9/14 Проверка, что backup реально содержит файлы =="
if [[ -z "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ]]; then
  echo "ОШИБКА: backup-директория пуста после копирования -- деплой остановлен" >&2
  exit 1
fi
if [[ ! -f "${BACKUP_DIR}/backend_runtime/.manifest" || ! -f "${BACKUP_DIR}/backend_runtime/main.py" ]]; then
  echo "ОШИБКА: backend runtime backup неполный -- деплой остановлен" >&2
  exit 1
fi
echo "OK: backup сохранён в $BACKUP_DIR"
find "$BACKUP_DIR" -type f | sed 's/^/  /'
BACKUP_READY=1

echo "== 10/14 Копирование backend в serving-путь =="
backend_runtime_deploy "$REPO_DIR/backend" "$BACKEND_SERVING_DIR"
backend_runtime_syntax_check "$BACKEND_SERVING_DIR"
# 18.09 (audit finding): standalone systemd-timer scripts (cleanup_old_attachments.py,
# daily_plan_cutoff_check.py) used to require a manual `cp` after every repo edit --
# now part of the regular deploy so the repo is the actual source of truth for what
# the timers run, not a snapshot someone remembers to refresh.
deploy_standalone_scripts "$REPO_DIR/backend" "$BACKEND_SERVING_DIR"
# Version-файл для /api/health -- version/commit видны в ответе без git subprocess
# на каждый запрос (main.py читает VERSION рядом с собой, см. APP_VERSION_FILE).
cat > "${BACKEND_SERVING_DIR}/VERSION" <<EOF
{"version": "$(git describe --tags --always 2>/dev/null || echo 0.9.0-rc1)", "commit": "$CURRENT_SHA"}
EOF
echo "OK"

echo "== 11/14 Копирование frontend (без .git, без тестов, без backup-файлов) =="
mkdir -p "$FRONTEND_SERVING_DIR"
# 12.09: excluded frontend artifacts are deleted from production too. Plain
# `--delete` protects excluded files on destination, so old app.html.bak-* and
# even legacy .git/.bak directories stayed publicly reachable in /var/www/miniapp.
# 26.09 (Wave F, deployer switched promonta -> grandmont): -a implies -g (preserve
# group), which made rsync try to chgrp every already-existing destination file to
# the deployer's own group -- only the file's owner or root may do that, so it
# failed non-atomically on every file not already owned by the current deployer.
# -rlptD is -a minus -g/-o (no group/owner preservation); the destination's setgid
# bit on /var/www/miniapp already assigns the right group to new files, and existing
# files keep whatever group chown set them to -- no chgrp needed either way.
rsync -rlptD -v --delete --delete-excluded \
  --exclude='.git*' --exclude='.archived-legacy' --exclude='.archived-legacy/' \
  --exclude='.bak' --exclude='.bak/' --exclude='*.bak' --exclude='*.bak-*' \
  --exclude='*.corrupt-*' --exclude='*.old' --exclude='*~' \
  "$REPO_DIR/frontend/" "$FRONTEND_SERVING_DIR/"
STALE_FRONTEND_ARTIFACT="$(find "$FRONTEND_SERVING_DIR" -maxdepth 4 \
  \( -name '.git' -o -name '.git*' -o -name '.archived-legacy' -o -name '.bak' \
     -o -name '*.bak' -o -name '*.bak-*' -o -name '*.corrupt-*' -o -name '*.old' -o -name '*~' \) \
  -print -quit)"
if [[ -n "$STALE_FRONTEND_ARTIFACT" ]]; then
  echo "ОШИБКА: stale frontend artifact survived deploy: $STALE_FRONTEND_ARTIFACT" >&2
  exit 1
fi
# 31.07 (Release-аудит П9, cache-busting): app.html ссылается на js/css относительными
# путями без версии (src="js/chat.js", href="css/tokens.css") -- Caddy теперь кеширует
# /js/* и /css/* на год (immutable), поэтому КАЖДЫЙ деплой обязан менять URL, иначе
# браузер отдаёт старый JS из кеша после релиза. Единый механизм: sed добавляет
# ?v=<SHA> в SERVING-копии app.html (не в репозитории -- в git остаётся чистый путь без
# версии, деплой -- единственное место, где появляется версия).
sed -i -E "s#(src=\"js/[^\"]+)\"#\1?v=${CURRENT_SHA}\"#g; s#(href=\"css/[^\"]+)\"#\1?v=${CURRENT_SHA}\"#g" \
  "${FRONTEND_SERVING_DIR}/app.html"
# 10.09 (deploy permissions fix): /var/www/miniapp is now root:webdeploy 2775
# (setgid) -- promonta is a webdeploy group member and can write here directly,
# no root/sudo needed for this step any more. New files inherit the webdeploy
# group automatically via setgid; no chown required after rsync.
echo "OK (group-writable via webdeploy, no chown needed)"
echo "OK"

echo "== 12/14 Restart backend =="
# 10.09: sudoers grants promonta NOPASSWD exactly this one systemctl call
# (/etc/sudoers.d/promonta-miniapp-deploy) -- the only remaining root-need
# in this script, everything else now runs as plain promonta.
sudo /bin/systemctl restart "$SERVICE_NAME"
sleep 3
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "ОШИБКА: $SERVICE_NAME не активен после restart -- см. journalctl -u $SERVICE_NAME" >&2
  exit 1
fi
echo "OK: $SERVICE_NAME активен"

echo "== 13/14 Health/readiness проверка =="
HEALTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || echo 000)"
echo "GET $HEALTH_URL -> $HEALTH_CODE"
if [[ "$HEALTH_CODE" != "200" ]]; then
  echo "ОШИБКА: /api/health не отвечает 200 после деплоя" >&2
  exit 1
fi
echo "(readiness /api/health/ready owner-only -- проверь вручную через Telegram-авторизованный запрос, скрипт её не может вызвать без initData)"

echo "== 14/14 Проверка, что deployed SHA совпадает с ожидаемым =="
DEPLOYED_SHA="$(curl -s "$HEALTH_URL" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("commit",""))' 2>/dev/null || echo '')"
if [[ "$DEPLOYED_SHA" != "$CURRENT_SHA" ]]; then
  echo "ОШИБКА: /api/health вернул commit=$DEPLOYED_SHA, ожидался $CURRENT_SHA" >&2
  exit 1
fi
echo "OK: deployed SHA подтверждён"

echo ""
echo "== Последние логи backend =="
journalctl -u "$SERVICE_NAME" -n 20 --no-pager

echo ""
# 17.09 (P1 retention/disk-hygiene finding): rollback_backup_* directories
# accumulated unbounded in /tmp -- 83 backups / 1.8GB found on this VPS
# before this fix, going back weeks. Each deploy makes one; nothing ever
# removed old ones. Keep the newest 10 (matches the realistic rollback
# window -- nobody rolls back more than a few deploys), delete the rest.
# Pure disk hygiene, no live references to old backups once newer ones
# exist and are known-good.
OLD_BACKUPS=$(ls -dt /tmp/rollback_backup_* 2>/dev/null | tail -n +11)
if [ -n "$OLD_BACKUPS" ]; then
  echo "$OLD_BACKUPS" | xargs rm -rf
  REMOVED_COUNT=$(echo "$OLD_BACKUPS" | wc -l)
  echo "Очистка: удалено $REMOVED_COUNT старых backup-каталогов (оставлены последние 10)"
fi

echo "=== ДЕПЛОЙ ЗАВЕРШЁН ==="
echo "SHA:     $CURRENT_SHA"
echo "Backup:  $BACKUP_DIR (не удалён -- для отката: scripts/rollback.sh $BACKUP_DIR)"

trap - EXIT