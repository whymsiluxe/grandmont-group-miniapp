#!/bin/bash
# Grandmont Group miniapp rollback -- Release-аудит Этап 8. Восстанавливает production
# файлы из backup, созданного scripts/deploy.sh (/tmp/rollback_backup_<timestamp>/).
#
# Запуск: как пользователь promonta, БЕЗ sudo/root (10.09, deploy permissions fix --
# /var/www/miniapp теперь root:webdeploy 2775 setgid, promonta пишет напрямую):
#   bash scripts/rollback.sh /tmp/rollback_backup_20260730_153917
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/runtime_manifest.sh"

BACKEND_SERVING_DIR="/home/grandmont/agent/miniapp"
FRONTEND_SERVING_DIR="/var/www/miniapp"
SERVICE_NAME="grandmont-group-miniapp.service"
HEALTH_URL="https://app.promonta.fun/api/health"

# 11.09 (Phase 1): source manifest for BACKEND_PY_LIBS / BACKEND_JS_FILES / BACKEND_CORE_DIR
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=manifest.sh
source "$REPO_DIR/scripts/manifest.sh"

BACKUP_DIR="${1:-}"
if [[ -z "$BACKUP_DIR" ]]; then
  echo "Использование: $0 <путь-к-backup-директории>" >&2
  echo "Доступные backup'ы:" >&2
  ls -dt /tmp/rollback_backup_* 2>/dev/null | sed 's/^/  /' >&2
  exit 1
fi

echo "== 1/6 Валидация backup-директории =="
if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "ОШИБКА: $BACKUP_DIR не существует" >&2
  exit 1
fi
if [[ -z "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ]]; then
  echo "ОШИБКА: $BACKUP_DIR пуст -- нечего восстанавливать" >&2
  exit 1
fi
HAS_RUNTIME_MANIFEST=0
if [[ -f "$BACKUP_DIR/backend_runtime/.manifest" ]]; then
  HAS_RUNTIME_MANIFEST=1
fi
if [[ "$HAS_RUNTIME_MANIFEST" == "1" && ! -f "$BACKUP_DIR/backend_runtime/main.py" ]]; then
  echo "ОШИБКА: $BACKUP_DIR/backend_runtime/main.py отсутствует -- runtime backup неполный" >&2
  exit 1
fi
if [[ "$HAS_RUNTIME_MANIFEST" != "1" && ! -f "$BACKUP_DIR/main.py" ]]; then
  echo "ОШИБКА: ни backend_runtime/.manifest, ни main.py не найдены -- это не похоже на backup от deploy.sh" >&2
  exit 1
fi
echo "OK: $BACKUP_DIR содержит:"
find "$BACKUP_DIR" -type f | sed 's/^/  /'

echo "== 2/6 Syntax-check backup перед восстановлением =="
if [[ "$HAS_RUNTIME_MANIFEST" == "1" ]]; then
  backend_runtime_syntax_check "$BACKUP_DIR/backend_runtime"
else
  python3 -m py_compile "$BACKUP_DIR/main.py"
  for _f in "${BACKEND_PY_LIBS[@]}"; do
    if [[ -f "$BACKUP_DIR/${_f}" ]]; then python3 -m py_compile "$BACKUP_DIR/${_f}"; fi
  done
  if [[ -d "$BACKUP_DIR/${BACKEND_CORE_DIR}" ]]; then
    python3 -m py_compile "$BACKUP_DIR/${BACKEND_CORE_DIR}"/*.py
  fi
  for _f in "${BACKEND_JS_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/${_f}" ]]; then node --check "$BACKUP_DIR/${_f}"; fi
  done
fi
echo "OK"

echo "== 3/6 Восстановление backend =="
if [[ "$HAS_RUNTIME_MANIFEST" == "1" ]]; then
  backend_runtime_restore "$BACKUP_DIR" "$BACKEND_SERVING_DIR"
else
  cp "$BACKUP_DIR/main.py" "${BACKEND_SERVING_DIR}/main.py"
  for _f in "${BACKEND_PY_LIBS[@]}"; do
    if [[ -f "$BACKUP_DIR/${_f}" ]]; then
      cp "$BACKUP_DIR/${_f}" "${BACKEND_SERVING_DIR}/${_f}"
    elif [[ -f "$BACKUP_DIR/.${_f}.ABSENT" ]]; then
      rm -f "${BACKEND_SERVING_DIR}/${_f}"
    fi
  done
  for _f in "${BACKEND_JS_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/${_f}" ]]; then
      cp "$BACKUP_DIR/${_f}" "${BACKEND_SERVING_DIR}/${_f}"
    fi
  done
  if [[ -d "$BACKUP_DIR/${BACKEND_CORE_DIR}" ]]; then
    rm -rf "${BACKEND_SERVING_DIR}/${BACKEND_CORE_DIR}"
    cp -r "$BACKUP_DIR/${BACKEND_CORE_DIR}" "${BACKEND_SERVING_DIR}/${BACKEND_CORE_DIR}"
  elif [[ -f "$BACKUP_DIR/.${BACKEND_CORE_DIR}.ABSENT" ]]; then
    rm -rf "${BACKEND_SERVING_DIR}/${BACKEND_CORE_DIR}"
  fi
fi
# VERSION -- симметрично deploy.sh: backup либо содержит старый VERSION (сценарий A,
# восстановить), либо маркер .VERSION_ABSENT (сценарий B, файла до deploy не было --
# удалить тот, что создал деплой, чтобы /api/health не показывал отменённый SHA).
if [[ -f "$BACKUP_DIR/VERSION" ]]; then
  cp "$BACKUP_DIR/VERSION" "${BACKEND_SERVING_DIR}/VERSION"
elif [[ -f "$BACKUP_DIR/.VERSION_ABSENT" ]]; then
  rm -f "${BACKEND_SERVING_DIR}/VERSION"
fi
echo "OK"

echo "== 4/6 Восстановление frontend (если было в backup) =="
if [[ -d "$BACKUP_DIR/frontend" ]]; then
  rsync -a --delete "$BACKUP_DIR/frontend/" "${FRONTEND_SERVING_DIR}/"
  # 10.09: no chown needed -- root:webdeploy 2775 setgid on FRONTEND_SERVING_DIR
  # means new files inherit the webdeploy group automatically.
  echo "OK"
else
  echo "(в этом backup не было frontend -- пропущено)"
fi

echo "== 5/6 Restart backend =="
# 10.09: sudoers grants promonta NOPASSWD exactly this one systemctl call.
sudo /bin/systemctl restart "$SERVICE_NAME"
sleep 3
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "ОШИБКА: $SERVICE_NAME не активен после restart даже после отката -- требуется ручное вмешательство" >&2
  journalctl -u "$SERVICE_NAME" -n 30 --no-pager
  exit 1
fi
echo "OK: $SERVICE_NAME активен"

echo "== 6/6 Health-проверка =="
HEALTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || echo 000)"
echo "GET $HEALTH_URL -> $HEALTH_CODE"
if [[ "$HEALTH_CODE" != "200" ]]; then
  echo "ПРЕДУПРЕЖДЕНИЕ: /api/health не отвечает 200 даже после отката -- нужна ручная диагностика" >&2
fi

echo ""
echo "=== ОТКАТ ЗАВЕРШЁН ==="
echo "Backup НЕ удалён (на случай если понадобится снова): $BACKUP_DIR"