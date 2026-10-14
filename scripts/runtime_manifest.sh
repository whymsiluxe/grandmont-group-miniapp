#!/usr/bin/env bash
# Runtime helper functions for the backend artifact manifest. scripts/manifest.sh
# remains the single source of truth; this file only turns that manifest into
# backup/deploy/restore operations.

_RUNTIME_MANIFEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=manifest.sh
source "${_RUNTIME_MANIFEST_DIR}/manifest.sh"

# JSON persistence uses process-local locks. Until PostgreSQL replaces these
# stores, production MUST stay single-worker. deploy.sh always sources this file
# before touching production, so fail closed if the systemd unit drifts to a
# multi-worker/reload configuration. Other consumers (rollback/tests) only get
# the helper functions below and do not query systemd.
if [[ "${BASH_SOURCE[1]:-}" == *"/deploy.sh" ]]; then
  _runtime_service_name="${GRANDMONT_GROUP_SERVICE_NAME:-grandmont-group-miniapp.service}"
  if ! _runtime_exec_start="$(systemctl show "$_runtime_service_name" -p ExecStart --value)"; then
    echo "ОШИБКА: не удалось прочитать ExecStart для $_runtime_service_name" >&2
    return 1
  fi
  if ! _runtime_environment="$(systemctl show "$_runtime_service_name" -p Environment --value)"; then
    echo "ОШИБКА: не удалось прочитать Environment для $_runtime_service_name" >&2
    return 1
  fi
  bash "${_RUNTIME_MANIFEST_DIR}/assert_single_worker.sh" \
    "$_runtime_exec_start" "$_runtime_environment"
  unset _runtime_service_name _runtime_exec_start _runtime_environment
fi

BACKEND_RUNTIME_ENTRIES=("__init__.py" "main.py")
BACKEND_RUNTIME_ENTRIES+=("${BACKEND_PY_LIBS[@]}")
BACKEND_RUNTIME_ENTRIES+=("${BACKEND_CORE_DIR}/")
BACKEND_RUNTIME_ENTRIES+=("${BACKEND_ROUTES_DIR}/")
BACKEND_RUNTIME_ENTRIES+=("${BACKEND_SERVICES_DIR}/")
BACKEND_RUNTIME_ENTRIES+=("${BACKEND_JS_FILES[@]}")
BACKEND_RUNTIME_ENTRIES+=("${BACKEND_SUBPROCESS_SCRIPTS[@]}")

backend_runtime_manifest_print() {
  printf '%s\n' "${BACKEND_RUNTIME_ENTRIES[@]}"
}

_manifest_norm_entry() {
  local entry="$1"
  printf '%s' "${entry%/}"
}

backend_runtime_backup() {
  local serving_dir="$1"
  local backup_dir="$2"
  local backup_backend_dir="${backup_dir}/backend_runtime"

  rm -rf "$backup_backend_dir"
  mkdir -p "${backup_backend_dir}/.absent"
  backend_runtime_manifest_print > "${backup_backend_dir}/.manifest"

  local entry norm src dst marker
  for entry in "${BACKEND_RUNTIME_ENTRIES[@]}"; do
    norm="$(_manifest_norm_entry "$entry")"
    src="${serving_dir}/${norm}"
    dst="${backup_backend_dir}/${norm}"
    marker="${backup_backend_dir}/.absent/${norm}"

    if [[ -d "$src" ]]; then
      mkdir -p "$dst"
      rsync -a --delete "${src}/" "${dst}/"
    elif [[ -f "$src" ]]; then
      mkdir -p "$(dirname "$dst")"
      cp "$src" "$dst"
    else
      mkdir -p "$(dirname "$marker")"
      touch "$marker"
    fi
  done
}

backend_runtime_deploy() {
  local repo_backend_dir="$1"
  local serving_dir="$2"

  local entry norm src dst
  for entry in "${BACKEND_RUNTIME_ENTRIES[@]}"; do
    norm="$(_manifest_norm_entry "$entry")"
    src="${repo_backend_dir}/${norm}"
    dst="${serving_dir}/${norm}"

    if [[ -d "$src" ]]; then
      rm -rf "$dst"
      mkdir -p "$dst"
      rsync -a --delete "${src}/" "${dst}/"
    elif [[ -f "$src" ]]; then
      mkdir -p "$(dirname "$dst")"
      cp "$src" "$dst"
    else
      echo "ОШИБКА: runtime manifest entry отсутствует в repo backend: $src" >&2
      return 1
    fi
  done
}

backend_runtime_restore() {
  local backup_dir="$1"
  local serving_dir="$2"
  local backup_backend_dir="${backup_dir}/backend_runtime"
  local manifest="${backup_backend_dir}/.manifest"

  if [[ ! -f "$manifest" ]]; then
    echo "ОШИБКА: нет runtime manifest в backup: $manifest" >&2
    return 1
  fi

  local entry norm src dst marker
  while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    norm="$(_manifest_norm_entry "$entry")"
    src="${backup_backend_dir}/${norm}"
    dst="${serving_dir}/${norm}"
    marker="${backup_backend_dir}/.absent/${norm}"

    if [[ -e "$marker" && ! -e "$src" ]]; then
      rm -rf "$dst"
    elif [[ -d "$src" ]]; then
      rm -rf "$dst"
      mkdir -p "$dst"
      rsync -a --delete "${src}/" "${dst}/"
    elif [[ -f "$src" ]]; then
      mkdir -p "$(dirname "$dst")"
      cp "$src" "$dst"
    else
      echo "ОШИБКА: backup не содержит runtime entry и absent-marker: $entry" >&2
      return 1
    fi
  done < "$manifest"
}

# 18.09 (audit finding): standalone scripts (systemd-timer-only, never imported
# by main.py -- see BACKEND_STANDALONE_SCRIPTS in manifest.sh) are copied here
# so the repo stays the real source of truth for what a timer actually runs.
# Deliberately NOT part of backend_runtime_backup/restore -- these files are not
# imported by the running app, so a bad edit to one of them can't break
# main.py's own startup/serving, and doesn't need the same rollback safety net
# (worst case: a broken standalone script fails its own next systemd run,
# logged, app itself is unaffected).
deploy_standalone_scripts() {
  local repo_backend_dir="$1"
  local serving_dir="$2"
  local entry src dst

  for entry in "${BACKEND_STANDALONE_SCRIPTS[@]}"; do
    src="${repo_backend_dir}/${entry}"
    dst="${serving_dir}/${entry}"
    if [[ ! -f "$src" ]]; then
      echo "ОШИБКА: standalone script отсутствует в repo backend: $src" >&2
      return 1
    fi
    cp "$src" "$dst"
    chmod +x "$dst"
  done
}

backend_runtime_syntax_check() {
  local backend_dir="$1"
  local py_files=()
  local js_files=()
  local entry norm path f

  for entry in "${BACKEND_RUNTIME_ENTRIES[@]}"; do
    norm="$(_manifest_norm_entry "$entry")"
    path="${backend_dir}/${norm}"
    if [[ -d "$path" ]]; then
      while IFS= read -r -d '' f; do
        case "$f" in
          *.py) py_files+=("$f") ;;
          *.js) js_files+=("$f") ;;
        esac
      done < <(find "$path" -type f \( -name '*.py' -o -name '*.js' \) -print0)
    elif [[ -f "$path" ]]; then
      case "$path" in
        *.py) py_files+=("$path") ;;
        *.js) js_files+=("$path") ;;
      esac
    fi
  done

  if [[ ${#py_files[@]} -gt 0 ]]; then
    python3 -m py_compile "${py_files[@]}"
  fi
  if [[ ${#js_files[@]} -gt 0 ]]; then
    local js
    for js in "${js_files[@]}"; do
      node --check "$js"
    done
  fi
}
