#!/usr/bin/env bash

_USERTEST_PYTHON_HEALTH_PROBE='import encodings, json, os, sys; print(json.dumps({"executable": sys.executable, "version": sys.version.split()[0], "prefix": sys.prefix, "base_prefix": getattr(sys, "base_prefix", None), "real_prefix": getattr(sys, "real_prefix", None), "exec_prefix": sys.exec_prefix, "base_exec_prefix": getattr(sys, "base_exec_prefix", None), "virtual_env": os.environ.get("VIRTUAL_ENV")}))'

_usertest_python_is_windows() {
  case "${OSTYPE:-}" in
    cygwin*|msys*|win32*|mingw*) return 0 ;;
  esac
  case "$(uname -s 2>/dev/null || true)" in
    CYGWIN*|MINGW*|MSYS*) return 0 ;;
  esac
  return 1
}

_usertest_python_venv_path() {
  local venv_dir="$1"
  if _usertest_python_is_windows; then
    printf '%s\n' "${venv_dir}/Scripts/python.exe"
  else
    printf '%s\n' "${venv_dir}/bin/python"
  fi
}

_usertest_python_normalize_windows_path() {
  printf '%s' "$1" | tr '/' '\\' | tr '[:upper:]' '[:lower:]'
}

_usertest_python_is_windowsapps_alias() {
  local candidate="$1"
  if ! _usertest_python_is_windows; then
    return 1
  fi
  [[ "$(_usertest_python_normalize_windows_path "$candidate")" == *\\windowsapps\\* ]]
}

_usertest_python_probe_reason_code() {
  local merged lowered
  merged="$1"
  lowered="$(printf '%s' "$merged" | tr '[:upper:]' '[:lower:]')"
  if [[ "$lowered" == *encodings* ]] && [[ "$lowered" == *modulenotfounderror* || "$lowered" == *"no module named"* ]]; then
    printf 'missing_stdlib\n'
    return
  fi
  if [[ "$lowered" == *"access is denied"* || "$lowered" == *"permission denied"* || "$lowered" == *"cannot be accessed by the system"* ]]; then
    printf 'access_denied\n'
    return
  fi
  if [[ "$lowered" == *"the system cannot find the file specified"* ]]; then
    printf 'not_found\n'
    return
  fi
  printf 'runtime_probe_failed\n'
}

_usertest_python_parse_json_field() {
  local payload="$1"
  local field="$2"
  printf '%s\n' "$payload" | sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | tail -n 1
}

_usertest_python_probe_candidate() {
  local source="$1"
  local candidate="$2"

  USERTEST_PYTHON_PROBE_SOURCE="$source"
  USERTEST_PYTHON_PROBE_PATH="$candidate"
  USERTEST_PYTHON_PROBE_PRESENT=0
  USERTEST_PYTHON_PROBE_USABLE=0
  USERTEST_PYTHON_PROBE_REASON_CODE=""
  USERTEST_PYTHON_PROBE_REASON=""
  USERTEST_PYTHON_PROBE_VERSION=""
  USERTEST_PYTHON_PROBE_EXECUTABLE=""

  if [[ -z "$candidate" ]]; then
    USERTEST_PYTHON_PROBE_REASON_CODE="not_found"
    USERTEST_PYTHON_PROBE_REASON="Empty interpreter path."
    return 1
  fi

  if [[ "$candidate" == */* || "$candidate" == *\\* || "$candidate" == [A-Za-z]:* ]]; then
    if [[ ! -e "$candidate" ]]; then
      USERTEST_PYTHON_PROBE_REASON_CODE="not_found"
      USERTEST_PYTHON_PROBE_REASON="Interpreter not found at: ${candidate}"
      return 1
    fi
    USERTEST_PYTHON_PROBE_PRESENT=1
  else
    if ! command -v "$candidate" >/dev/null 2>&1; then
      USERTEST_PYTHON_PROBE_REASON_CODE="not_found"
      USERTEST_PYTHON_PROBE_REASON="\`${candidate}\` was not found on PATH."
      return 1
    fi
    USERTEST_PYTHON_PROBE_PRESENT=1
    candidate="$(command -v "$candidate")"
    USERTEST_PYTHON_PROBE_PATH="$candidate"
  fi

  if _usertest_python_is_windowsapps_alias "$candidate"; then
    USERTEST_PYTHON_PROBE_REASON_CODE="windowsapps_alias"
    USERTEST_PYTHON_PROBE_REASON="Resolved to a WindowsApps launcher alias. Install/select a full Python interpreter and retry."
    return 1
  fi

  local output rc payload_line reason_code
  rc=0
  output="$("$candidate" -c "${_USERTEST_PYTHON_HEALTH_PROBE}" 2>&1)" || rc=$?

  if [[ "$rc" -ne 0 ]]; then
    USERTEST_PYTHON_PROBE_REASON_CODE="$(_usertest_python_probe_reason_code "$output")"
    USERTEST_PYTHON_PROBE_REASON="$output"
    return 1
  fi

  payload_line="$(printf '%s\n' "$output" | sed '/^[[:space:]]*$/d' | tail -n 1)"
  if [[ -z "$payload_line" ]]; then
    USERTEST_PYTHON_PROBE_REASON_CODE="runtime_probe_failed"
    USERTEST_PYTHON_PROBE_REASON="Interpreter probe did not emit parseable JSON payload."
    return 1
  fi

  USERTEST_PYTHON_PROBE_VERSION="$(_usertest_python_parse_json_field "$payload_line" version)"
  USERTEST_PYTHON_PROBE_EXECUTABLE="$(_usertest_python_parse_json_field "$payload_line" executable)"
  USERTEST_PYTHON_PROBE_USABLE=1
  return 0
}

_usertest_python_add_candidate() {
  local source="$1"
  local candidate="$2"
  if [[ -z "$candidate" ]]; then
    return
  fi
  local key
  if _usertest_python_is_windows; then
    key="$(_usertest_python_normalize_windows_path "$candidate")"
  else
    key="$candidate"
  fi
  if [[ " ${USERTEST_PYTHON_SEEN_KEYS:-} " == *" ${key} "* ]]; then
    return
  fi
  USERTEST_PYTHON_SEEN_KEYS="${USERTEST_PYTHON_SEEN_KEYS:-} ${key}"
  USERTEST_PYTHON_CANDIDATE_SOURCES+=("$source")
  USERTEST_PYTHON_CANDIDATE_PATHS+=("$candidate")
}

_usertest_python_py0p_interpreters() {
  if ! _usertest_python_is_windows; then
    return
  fi
  if ! command -v py >/dev/null 2>&1; then
    return
  fi
  py -0p 2>/dev/null | tr -d '\r' | sed -n 's#.*\([A-Za-z]:\\.*\)$#\1#p'
}

_usertest_python_where_all() {
  local command_name="$1"
  if ! _usertest_python_is_windows; then
    return
  fi
  if command -v where.exe >/dev/null 2>&1; then
    where.exe "$command_name" 2>/dev/null | tr -d '\r'
    return
  fi
  if command -v where >/dev/null 2>&1; then
    where "$command_name" 2>/dev/null | tr -d '\r'
  fi
}

usertest_resolve_python() {
  local repo_root="$1"
  local timeout_seconds="${2:-5}"
  local workspace_venv venv_env resolved source candidate
  local -a rejections

  USERTEST_PYTHON_SEEN_KEYS=""
  USERTEST_PYTHON_CANDIDATE_SOURCES=()
  USERTEST_PYTHON_CANDIDATE_PATHS=()

  if [[ -n "${USERTEST_PYTHON:-}" ]]; then
    _usertest_python_add_candidate "sandbox_env" "$USERTEST_PYTHON"
  fi

  workspace_venv="$(_usertest_python_venv_path "${repo_root}/.venv")"
  _usertest_python_add_candidate "workspace_venv" "$workspace_venv"

  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    venv_env="$(_usertest_python_venv_path "$VIRTUAL_ENV")"
    _usertest_python_add_candidate "virtual_env" "$venv_env"
  fi

  while IFS= read -r candidate; do
    _usertest_python_add_candidate "py_0p" "$candidate"
  done < <(_usertest_python_py0p_interpreters)

  while IFS= read -r candidate; do
    if _usertest_python_is_windowsapps_alias "$candidate"; then
      continue
    fi
    _usertest_python_add_candidate "where_python" "$candidate"
  done < <(_usertest_python_where_all python)

  while IFS= read -r candidate; do
    if _usertest_python_is_windowsapps_alias "$candidate"; then
      continue
    fi
    _usertest_python_add_candidate "where_python3" "$candidate"
  done < <(_usertest_python_where_all python3)

  _usertest_python_add_candidate "command_py" "$(command -v py 2>/dev/null || true)"
  _usertest_python_add_candidate "command_python" "$(command -v python 2>/dev/null || true)"
  _usertest_python_add_candidate "command_python3" "$(command -v python3 2>/dev/null || true)"

  rejections=()
  local idx
  for idx in "${!USERTEST_PYTHON_CANDIDATE_PATHS[@]}"; do
    source="${USERTEST_PYTHON_CANDIDATE_SOURCES[$idx]}"
    candidate="${USERTEST_PYTHON_CANDIDATE_PATHS[$idx]}"
    if _usertest_python_probe_candidate "$source" "$candidate"; then
      USERTEST_PYTHON_BIN="$USERTEST_PYTHON_PROBE_PATH"
      USERTEST_PYTHON_SOURCE="$USERTEST_PYTHON_PROBE_SOURCE"
      USERTEST_PYTHON_VERSION="$USERTEST_PYTHON_PROBE_VERSION"
      USERTEST_PYTHON_EXECUTABLE="$USERTEST_PYTHON_PROBE_EXECUTABLE"
      export USERTEST_PYTHON="$USERTEST_PYTHON_BIN"
      export USERTEST_PYTHON_BIN USERTEST_PYTHON_SOURCE USERTEST_PYTHON_VERSION USERTEST_PYTHON_EXECUTABLE
      return 0
    fi
    if [[ -n "$USERTEST_PYTHON_PROBE_REASON" ]]; then
      rejections+=("[${source}] rejected (${USERTEST_PYTHON_PROBE_REASON_CODE}): ${USERTEST_PYTHON_PROBE_PATH}\n    ${USERTEST_PYTHON_PROBE_REASON}")
    else
      rejections+=("[${source}] rejected (${USERTEST_PYTHON_PROBE_REASON_CODE}): ${USERTEST_PYTHON_PROBE_PATH}")
    fi
  done

  {
    printf 'No usable Python interpreter found (within ~%ss).\n\n' "$timeout_seconds"
    printf 'Tried:\n'
    if [[ "${#rejections[@]}" -eq 0 ]]; then
      printf '  - No candidates were discovered.\n'
    else
      local item
      for item in "${rejections[@]}"; do
        printf '  - %b\n' "$item"
      done
    fi
    printf '\nFix options:\n'
    printf '  1) Install CPython (python.org) or via your package manager.\n'
    printf '  2) Disable Windows App Execution Alias shims for python.exe/python3.exe on Windows.\n'
    printf '  3) Export USERTEST_PYTHON to a known-good interpreter path before rerunning.\n'
  } >&2
  return 1
}
