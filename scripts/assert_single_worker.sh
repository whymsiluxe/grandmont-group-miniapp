#!/usr/bin/env bash
set -euo pipefail

EXEC_START="${1:-}"
UNIT_ENV="${2:-}"

fail() {
  echo "ОШИБКА: JSON runtime требует ровно один backend worker до миграции на PostgreSQL: $*" >&2
  exit 1
}

[[ -n "$EXEC_START" ]] || fail "systemd ExecStart пуст"
[[ "$EXEC_START" == *"uvicorn"* ]] || fail "ExecStart больше не запускает uvicorn"
[[ "$EXEC_START" == *"miniapp.main:app"* ]] || fail "ExecStart больше не запускает miniapp.main:app"
[[ "$EXEC_START" != *"--reload"* ]] || fail "--reload недопустим в production"

workers=1
if [[ "$EXEC_START" =~ --workers=([0-9]+) ]]; then
  workers="${BASH_REMATCH[1]}"
elif [[ "$EXEC_START" =~ --workers[[:space:]]+([0-9]+) ]]; then
  workers="${BASH_REMATCH[1]}"
elif [[ "$EXEC_START" == *"--workers"* ]]; then
  fail "не удалось однозначно разобрать --workers в ExecStart"
fi

if [[ "$workers" != "1" ]]; then
  fail "ExecStart задаёт --workers=$workers"
fi

if [[ "$UNIT_ENV" =~ (^|[[:space:]])WEB_CONCURRENCY=([^[:space:]]+) ]]; then
  concurrency="${BASH_REMATCH[2]}"
  concurrency="${concurrency%\"}"
  concurrency="${concurrency#\"}"
  [[ "$concurrency" =~ ^[0-9]+$ ]] || fail "WEB_CONCURRENCY имеет некорректное значение '$concurrency'"
  [[ "$concurrency" == "1" ]] || fail "WEB_CONCURRENCY=$concurrency"
fi

echo "OK: single-worker invariant подтверждён (JSON storage process-local locks защищены)"
