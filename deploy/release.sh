#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 0077

readonly SERVICE_NAME="nexa-guardian.service"
readonly BACKUP_SERVICE_NAME="nexa-guardian-backup.service"
readonly BACKUP_TIMER_NAME="nexa-guardian-backup.timer"
readonly SERVICE_USER="nexa-guardian"
readonly SERVICE_GROUP="nexa-guardian"
readonly APP_ROOT="/opt/nexa-guardian"
readonly RELEASES_ROOT="${APP_ROOT}/releases"
readonly CURRENT_LINK="${APP_ROOT}/current"
readonly CONFIG_PATH="/etc/nexa-guardian/guardian.env"
readonly DATABASE_PATH="/var/lib/nexa-guardian/guardian.db"
readonly TELEGRAM_SESSION_PATH="/var/lib/nexa-guardian/mtproto/admin.session"
readonly BACKUP_ROOT="/var/backups/nexa-guardian"
readonly MANUAL_BACKUP_ROOT="${BACKUP_ROOT}/manual"
readonly LOCK_PATH="/run/lock/nexa-guardian-release.lock"
readonly PYTHON_BIN="/usr/bin/python3"
readonly CODE_SCHEMA_VERSION="8"
readonly CODE_AI_MODEL="gpt-5.6-luna"

artifact_mode=""
artifact_path=""
git_sha=""
schema_version=""
requested_ai_model=""
allow_model_cooldown=0
stage_path=""
candidate_release_path=""
build_path=""
switch_path=""
config_backup_staging=""
candidate_config_path=""
probe_config_path="${CONFIG_PATH}"
backup_path=""
database_backup_sha256=""
database_backup_validated=0
session_backup_sha256=""
database_metadata_captured=0
database_owner_uid=""
database_owner_gid=""
database_mode=""
previous_target=""
switched=0
release_stable=0
service_stopped=0
backup_timer_paused=0
config_transition=0

usage() {
  cat <<'EOF'
Usage:
  sudo deploy/release.sh --wheel /absolute/path/nexa_guardian-*.whl \
    --git-sha <full-git-sha> --schema-version <integer> \
    [--ai-model gpt-5.6-luna] [--allow-model-cooldown]

  sudo deploy/release.sh --source /absolute/path/to/repository \
    [--git-sha <full-git-sha>] --schema-version <integer> \
    [--ai-model gpt-5.6-luna] [--allow-model-cooldown]

The script always deploys to /opt/nexa-guardian, backs up the protected
configuration, builds and probes the candidate, then stops the service for a
final consistent database backup before atomically switching current. It keeps
current plus the two newest older releases after a health check.
Use --ai-model only for the first transactional migration from the prior model.
Use --allow-model-cooldown only for an operator-approved exact 429 model_cooldown.
EOF
}

log() {
  printf '[release] %s\n' "$*"
}

die() {
  printf '[release] ERROR: %s\n' "$*" >&2
  exit 1
}

update_ai_model_config() {
  local config_path="$1"
  local target_model="$2"

  "${PYTHON_BIN}" - "${config_path}" "${target_model}" "$$" <<'PY'
import os
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
target_model = sys.argv[2]
process_id = sys.argv[3]
staging_path = config_path.parent / f".{config_path.name}.ai-model.{process_id}"
original = config_path.read_bytes()
lines = original.splitlines(keepends=True)
matches = [index for index, line in enumerate(lines) if line.startswith(b"AI_MODEL=")]
if len(matches) != 1:
    raise RuntimeError("configuration must contain exactly one literal AI_MODEL= line")

index = matches[0]
line = lines[index]
newline = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
current_model = line[len(b"AI_MODEL=") : len(line) - len(newline) if newline else None]
target = target_model.encode("ascii")
if current_model == target:
    print("unchanged")
    raise SystemExit(0)
if not current_model or any(character in current_model for character in b" \t#'\""):
    raise RuntimeError("AI_MODEL must be an unquoted value on its own line")

lines[index] = b"AI_MODEL=" + target + newline
updated = b"".join(lines)
metadata = config_path.stat()
if staging_path.exists() or staging_path.is_symlink():
    raise RuntimeError(f"configuration staging path already exists: {staging_path}")

try:
    descriptor = os.open(
        staging_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        metadata.st_mode & 0o7777,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as target_stream:
            target_stream.write(updated)
            target_stream.flush()
            os.fchown(target_stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(target_stream.fileno(), metadata.st_mode & 0o7777)
            os.fsync(target_stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.replace(staging_path, config_path)
    directory_fd = os.open(config_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    try:
        staging_path.unlink()
    except FileNotFoundError:
        pass
    raise

print("changed")
PY
}

restore_candidate_config_after_failed_rollback() {
  local update_result=""

  if [[ -z "${requested_ai_model}" ]]; then
    return 0
  fi
  if ! update_result="$(update_ai_model_config "${CONFIG_PATH}" "${requested_ai_model}")"; then
    return 1
  fi
  [[ "${update_result}" == "changed" || "${update_result}" == "unchanged" ]] || return 1
  config_transition=1
  printf '[release] restored candidate-compatible configuration after rollback link failure\n' >&2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

canonical_path() {
  readlink -f -- "$1"
}

is_path_below() {
  local candidate="$1"
  local root="$2"
  [[ "${candidate}" == "${root}/"* ]]
}

is_same_or_below() {
  local candidate="$1"
  local root="$2"
  [[ "${candidate}" == "${root}" || "${candidate}" == "${root}/"* ]]
}

path_contains_mountpoint() {
  local root="$1"
  local mount_targets=""
  local mount_target=""

  mount_targets="$(findmnt -rn -o TARGET)" || return 2
  while IFS= read -r mount_target; do
    if is_same_or_below "${mount_target}" "${root}"; then
      return 0
    fi
  done <<<"${mount_targets}"
  return 1
}

safe_remove_tree() {
  local target="$1"
  local allowed_root="$2"
  local target_real=""
  local allowed_root_real=""
  local mount_check=0

  [[ -d "${target}" && ! -L "${target}" ]] || return 1
  target_real="$(canonical_path "${target}")" || return 1
  allowed_root_real="$(canonical_path "${allowed_root}")" || return 1
  is_path_below "${target_real}" "${allowed_root_real}" || return 1
  if path_contains_mountpoint "${target_real}"; then
    return 1
  else
    mount_check=$?
  fi
  (( mount_check == 1 )) || return 1
  rm -rf --one-file-system -- "${target_real}"
}

wait_for_backup_service_idle() {
  local active_state=""
  local queued_job=""

  for _attempt in {1..120}; do
    active_state="$(
      systemctl show "${BACKUP_SERVICE_NAME}" --property=ActiveState --value
    )" || return 1
    queued_job="$(systemctl show "${BACKUP_SERVICE_NAME}" --property=Job --value)" \
      || return 1
    if [[ "${active_state}" == "inactive" && -z "${queued_job}" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

switch_current_to() {
  local target="$1"
  switch_path="${CURRENT_LINK}.new.$$"

  [[ -d "${target}" ]] || die "release target does not exist: ${target}"
  is_path_below "$(canonical_path "${target}")" "$(canonical_path "${RELEASES_ROOT}")" \
    || die "release target is outside ${RELEASES_ROOT}: ${target}"
  [[ ! -e "${switch_path}" && ! -L "${switch_path}" ]] \
    || die "temporary current link already exists: ${switch_path}"

  ln -s -- "${target}" "${switch_path}"
  # From here onward cleanup must restore the prior link and database. Mark the
  # switch attempt before mv so a signal cannot leave the new current untracked.
  switched=1
  mv -Tf -- "${switch_path}" "${CURRENT_LINK}"
  switch_path=""
}

restore_previous_release() {
  local rollback_link="${CURRENT_LINK}.rollback.$$"
  local current_backup_sha256=""
  local current_after_rollback=""

  if (( switched == 0 || release_stable == 1 )); then
    return
  fi

  printf '[release] deployment failed after switching current; rolling back\n' >&2
  if ! systemctl stop "${BACKUP_SERVICE_NAME}"; then
    printf '[release] CRITICAL: failed to stop database backup service; database was not changed\n' >&2
    return 1
  fi
  if ! wait_for_backup_service_idle; then
    printf '[release] CRITICAL: database backup service is still active; database was not changed\n' >&2
    return 1
  fi
  if ! systemctl stop "${SERVICE_NAME}"; then
    printf '[release] CRITICAL: failed to stop service; database was not changed\n' >&2
    return 1
  fi
  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    printf '[release] CRITICAL: service is still active; database was not changed\n' >&2
    return 1
  fi

  if (( database_backup_validated != 1 || database_metadata_captured != 1 )) \
    || [[ -z "${backup_path}" || -z "${database_backup_sha256}" \
      || ! -f "${backup_path}" || -L "${backup_path}" ]]; then
    printf '[release] CRITICAL: validated database backup is unavailable; database was not changed\n' >&2
    return 1
  fi
  if ! is_path_below "$(canonical_path "${backup_path}")" \
    "$(canonical_path "${MANUAL_BACKUP_ROOT}")"; then
    printf '[release] CRITICAL: database backup resolved outside manual backup root\n' >&2
    return 1
  fi
  if ! current_backup_sha256="$(sha256sum -- "${backup_path}" | awk '{print $1}')"; then
    printf '[release] CRITICAL: database backup checksum could not be read; database was not changed\n' >&2
    return 1
  fi
  if [[ "${current_backup_sha256}" != "${database_backup_sha256}" ]]; then
    printf '[release] CRITICAL: database backup checksum changed; database was not changed\n' >&2
    return 1
  fi

  if ! "${PYTHON_BIN}" - \
    "${backup_path}" \
    "${DATABASE_PATH}" \
    "${database_owner_uid}" \
    "${database_owner_gid}" \
    "${database_mode}" \
    "$$" <<'PY'
import os
import shutil
import sqlite3
import sys
from pathlib import Path

backup_path = Path(sys.argv[1])
database_path = Path(sys.argv[2])
owner_uid = int(sys.argv[3])
owner_gid = int(sys.argv[4])
mode = int(sys.argv[5], 8)
process_id = sys.argv[6]
staging_path = database_path.parent / f".{database_path.name}.rollback.{process_id}"


def validate(path: Path) -> None:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=15)
    try:
        connection.execute("PRAGMA busy_timeout = 15000")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise RuntimeError(f"quick_check failed for {path}: {quick_check!r}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"foreign_key_check failed for {path}: {foreign_keys!r}")
    finally:
        connection.close()


if staging_path.exists() or staging_path.is_symlink():
    raise RuntimeError(f"rollback staging path already exists: {staging_path}")

validate(backup_path)
try:
    with backup_path.open("rb") as source, staging_path.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    os.chown(staging_path, owner_uid, owner_gid)
    os.chmod(staging_path, mode)
    staging_fd = os.open(staging_path, os.O_RDONLY)
    try:
        os.fsync(staging_fd)
    finally:
        os.close(staging_fd)
    validate(staging_path)
    os.replace(staging_path, database_path)
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(f"{database_path}{suffix}").unlink()
        except FileNotFoundError:
            pass
    directory_fd = os.open(database_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    validate(database_path)
    restored = database_path.stat()
    if (restored.st_uid, restored.st_gid, restored.st_mode & 0o7777) != (
        owner_uid,
        owner_gid,
        mode,
    ):
        raise RuntimeError("restored database ownership or permissions do not match")
except BaseException:
    try:
        staging_path.unlink()
    except FileNotFoundError:
        pass
    raise
PY
  then
    printf '[release] CRITICAL: database restore failed; previous service was not started\n' >&2
    return 1
  fi

  if [[ -n "${previous_target}" && -d "${previous_target}" ]]; then
    if ! restore_original_config; then
      printf '[release] CRITICAL: old code was not restored because its configuration could not be restored\n' >&2
      return 1
    fi
    if ln -s -- "${previous_target}" "${rollback_link}" \
      && mv -Tf -- "${rollback_link}" "${CURRENT_LINK}"; then
      if systemctl start "${SERVICE_NAME}" \
        && systemctl is-active --quiet "${SERVICE_NAME}"; then
        printf '[release] rollback restored the database and previous release\n' >&2
        return 0
      else
        printf '[release] CRITICAL: previous release did not start cleanly\n' >&2
        return 1
      fi
    else
      current_after_rollback="$(canonical_path "${CURRENT_LINK}" 2>/dev/null || true)"
      [[ ! -L "${rollback_link}" ]] || rm -f -- "${rollback_link}"
      if [[ "${current_after_rollback}" == "${previous_target}" ]]; then
        if systemctl start "${SERVICE_NAME}" \
          && systemctl is-active --quiet "${SERVICE_NAME}"; then
          printf '[release] rollback link was already on the previous release; service restored\n' >&2
          return 0
        fi
        printf '[release] CRITICAL: previous release link was intact but service did not start\n' >&2
        return 1
      fi
      if ! restore_candidate_config_after_failed_rollback; then
        printf '[release] CRITICAL: failed to restore candidate-compatible configuration\n' >&2
      fi
      printf '[release] CRITICAL: failed to restore previous current link; service remains stopped\n' >&2
      return 1
    fi
  elif [[ -L "${CURRENT_LINK}" ]]; then
    rm -f -- "${CURRENT_LINK}"
    printf '[release] WARNING: no previous release exists; service remains stopped\n' >&2
    return 1
  fi

  printf '[release] CRITICAL: rollback had no previous release to restore\n' >&2
  return 1
}

restore_original_config() {
  local restore_result=""

  if (( config_transition == 0 )); then
    return 0
  fi
  if [[ ! -f "${config_backup_path}" || -L "${config_backup_path}" ]]; then
    printf '[release] CRITICAL: original configuration backup is unavailable\n' >&2
    return 1
  fi
  if ! restore_result="$("${PYTHON_BIN}" - \
    "${config_backup_path}" "${CONFIG_PATH}" "${config_mode}" "$$" <<'PY'
import os
import shutil
import sys
from pathlib import Path

backup_path = Path(sys.argv[1])
config_path = Path(sys.argv[2])
mode = int(sys.argv[3], 8)
process_id = sys.argv[4]
staging_path = config_path.parent / f".{config_path.name}.rollback.{process_id}"
metadata = config_path.stat()

if staging_path.exists() or staging_path.is_symlink():
    raise RuntimeError(f"configuration rollback staging path already exists: {staging_path}")

try:
    with backup_path.open("rb") as source:
        descriptor = os.open(staging_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fchown(target.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(target.fileno(), mode)
                os.fsync(target.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
    os.replace(staging_path, config_path)
    directory_fd = os.open(config_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    try:
        staging_path.unlink()
    except FileNotFoundError:
        pass
    raise

print("restored")
PY
  )"; then
    printf '[release] CRITICAL: failed to restore original configuration\n' >&2
    return 1
  fi
  [[ "${restore_result}" == "restored" ]] || return 1
  cmp -s -- "${config_backup_path}" "${CONFIG_PATH}" || return 1
  [[ "$(stat -c '%u:%g:%a' -- "${CONFIG_PATH}")" == "0:0:${config_mode}" ]] || return 1
  config_transition=0
  printf '[release] restored the original configuration before rollback\n' >&2
}

cleanup() {
  local exit_code=$?
  local candidate_real=""
  local current_real=""
  local releases_root_real=""
  local rollback_succeeded=1

  # Cleanup must reach the recovery blocks even when a noncritical removal
  # fails. Do not allow another INT/TERM to interrupt an in-progress rollback.
  trap '' INT TERM
  set +e
  if (( switched == 1 && release_stable == 0 )); then
    # A timer restarted just before a signal could otherwise launch a backup
    # while rollback replaces the database and clears its WAL files.
    backup_timer_paused=1
    if ! systemctl stop "${BACKUP_TIMER_NAME}" \
      || systemctl is-active --quiet "${BACKUP_TIMER_NAME}"; then
      printf '[release] CRITICAL: backup timer could not be stopped before rollback\n' >&2
      rollback_succeeded=0
      exit_code=1
    fi
  fi
  if (( rollback_succeeded == 1 )); then
    if ! restore_previous_release; then
      rollback_succeeded=0
      exit_code=1
    fi
  fi

  if (( switched == 0 && service_stopped == 1 && release_stable == 0 \
    && rollback_succeeded == 1 )); then
    if ! restore_original_config; then
      printf '[release] CRITICAL: previous service remains stopped because its configuration could not be restored\n' >&2
      rollback_succeeded=0
      exit_code=1
    elif systemctl start "${SERVICE_NAME}"; then
      printf '[release] previous service restarted after pre-switch failure\n' >&2
    else
      printf '[release] CRITICAL: failed to restart previous service after pre-switch failure\n' >&2
      exit_code=1
    fi
  fi

  if [[ -n "${stage_path}" && -d "${stage_path}" ]]; then
    if ! safe_remove_tree "${stage_path}" "${RELEASES_ROOT}"; then
      printf '[release] WARNING: failed to remove staging directory\n' >&2
      (( release_stable == 1 )) || exit_code=1
    fi
  fi
  if (( release_stable == 0 )) \
    && [[ -n "${candidate_release_path}" && -d "${candidate_release_path}" \
      && ! -L "${candidate_release_path}" ]]; then
    if ! releases_root_real="$(canonical_path "${RELEASES_ROOT}")" \
      || ! candidate_real="$(canonical_path "${candidate_release_path}")" \
      || ! is_path_below "${candidate_real}" "${releases_root_real}"; then
      printf '[release] CRITICAL: refusing to remove failed candidate outside releases root\n' >&2
      exit_code=1
    else
      if [[ -L "${CURRENT_LINK}" ]]; then
        current_real="$(canonical_path "${CURRENT_LINK}" 2>/dev/null || true)"
      fi
      if is_same_or_below "${current_real}" "${candidate_real}"; then
        printf '[release] WARNING: failed candidate is still current and was retained\n' >&2
        exit_code=1
      else
        if ! safe_remove_tree "${candidate_real}" "${RELEASES_ROOT}"; then
          printf '[release] WARNING: failed candidate could not be removed\n' >&2
          exit_code=1
        fi
      fi
    fi
  fi
  if [[ -n "${build_path}" && -d "${build_path}" ]]; then
    if ! safe_remove_tree "${build_path}" "/tmp"; then
      printf '[release] WARNING: failed to remove build directory\n' >&2
      (( release_stable == 1 )) || exit_code=1
    fi
  fi
  if [[ -n "${switch_path}" && -L "${switch_path}" ]]; then
    if ! rm -f -- "${switch_path}"; then
      printf '[release] WARNING: failed to remove temporary current link\n' >&2
      (( release_stable == 1 )) || exit_code=1
    fi
  fi
  if [[ -n "${config_backup_staging}" && -f "${config_backup_staging}" ]]; then
    if ! rm -f -- "${config_backup_staging}"; then
      printf '[release] WARNING: failed to remove temporary configuration backup\n' >&2
      (( release_stable == 1 )) || exit_code=1
    fi
  fi
  if [[ -n "${candidate_config_path}" ]]; then
    if [[ "${candidate_config_path}" == "${MANUAL_BACKUP_ROOT}"/.candidate-env-* \
      && -f "${candidate_config_path}" && ! -L "${candidate_config_path}" ]]; then
      if ! rm -f -- "${candidate_config_path}"; then
        printf '[release] WARNING: failed to remove candidate configuration copy\n' >&2
        (( release_stable == 1 )) || exit_code=1
      fi
    elif [[ -e "${candidate_config_path}" || -L "${candidate_config_path}" ]]; then
      printf '[release] CRITICAL: refusing to remove unexpected candidate configuration path\n' >&2
      exit_code=1
    fi
  fi

  if (( backup_timer_paused == 1 )); then
    if (( switched == 1 && release_stable == 0 && rollback_succeeded == 0 )); then
      printf '[release] CRITICAL: backup timer remains stopped after incomplete rollback\n' >&2
      exit_code=1
    elif systemctl start "${BACKUP_TIMER_NAME}"; then
      printf '[release] backup timer restarted after release interruption\n' >&2
    else
      printf '[release] CRITICAL: failed to restart backup timer\n' >&2
      exit_code=1
    fi
  fi

  exit "${exit_code}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

while (($# > 0)); do
  case "$1" in
    --wheel)
      [[ $# -ge 2 ]] || die "--wheel requires a path"
      [[ -z "${artifact_mode}" ]] || die "choose exactly one of --wheel or --source"
      artifact_mode="wheel"
      artifact_path="$2"
      shift 2
      ;;
    --source)
      [[ $# -ge 2 ]] || die "--source requires a path"
      [[ -z "${artifact_mode}" ]] || die "choose exactly one of --wheel or --source"
      artifact_mode="source"
      artifact_path="$2"
      shift 2
      ;;
    --git-sha)
      [[ $# -ge 2 ]] || die "--git-sha requires a value"
      git_sha="$2"
      shift 2
      ;;
    --schema-version)
      [[ $# -ge 2 ]] || die "--schema-version requires a value"
      schema_version="$2"
      shift 2
      ;;
    --ai-model)
      [[ $# -ge 2 ]] || die "--ai-model requires a value"
      [[ -z "${requested_ai_model}" ]] || die "--ai-model may only be provided once"
      requested_ai_model="$2"
      shift 2
      ;;
    --allow-model-cooldown)
      (( allow_model_cooldown == 0 )) || die "--allow-model-cooldown may only be provided once"
      allow_model_cooldown=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "${artifact_mode}" ]] || die "one of --wheel or --source is required"
[[ -n "${schema_version}" ]] || die "--schema-version is required"
[[ "${schema_version}" =~ ^[0-9]+$ ]] || die "schema version must be a non-negative integer"
[[ "${schema_version}" == "${CODE_SCHEMA_VERSION}" ]] \
  || die "this release script requires schema version ${CODE_SCHEMA_VERSION}"
[[ -z "${requested_ai_model}" || "${requested_ai_model}" == "${CODE_AI_MODEL}" ]] \
  || die "this release script only supports AI model ${CODE_AI_MODEL}"
(( allow_model_cooldown == 0 )) || [[ "${requested_ai_model}" == "${CODE_AI_MODEL}" ]] \
  || die "--allow-model-cooldown requires --ai-model ${CODE_AI_MODEL}"
[[ "${artifact_path}" = /* ]] || die "artifact path must be absolute"
[[ ${EUID} -eq 0 ]] || die "run this script as root"

require_command flock
require_command getent
require_command findmnt
require_command cmp
require_command mv
require_command mktemp
require_command readlink
require_command sha256sum
require_command stat
require_command systemctl
[[ -x "${PYTHON_BIN}" ]] || die "Python is not executable: ${PYTHON_BIN}"
getent passwd "${SERVICE_USER}" >/dev/null || die "service user does not exist: ${SERVICE_USER}"
getent group "${SERVICE_GROUP}" >/dev/null || die "service group does not exist: ${SERVICE_GROUP}"

exec 9>"${LOCK_PATH}"
flock -n 9 || die "another Nexa Guardian release is already running"

if [[ "${artifact_mode}" == "wheel" ]]; then
  [[ -f "${artifact_path}" && "${artifact_path}" == *.whl ]] \
    || die "wheel does not exist or does not end in .whl: ${artifact_path}"
  artifact_path="$(canonical_path "${artifact_path}")"
else
  [[ -d "${artifact_path}" && -f "${artifact_path}/pyproject.toml" ]] \
    || die "source path must contain pyproject.toml: ${artifact_path}"
  artifact_path="$(canonical_path "${artifact_path}")"
  require_command git
  source_git_sha="$(git -C "${artifact_path}" rev-parse --verify HEAD)" \
    || die "source must be a Git worktree"
  [[ -z "$(git -C "${artifact_path}" status --porcelain --untracked-files=normal)" ]] \
    || die "source worktree is dirty; commit or remove all changes before release"
  if [[ -z "${git_sha}" ]]; then
    git_sha="${source_git_sha}"
  elif [[ "$(printf '%s' "${git_sha}" | tr '[:upper:]' '[:lower:]')" != "${source_git_sha}" ]]; then
    die "--git-sha does not match the source HEAD"
  fi
fi
[[ -n "${git_sha}" ]] || die "--git-sha is required when deploying a wheel"
git_sha="$(printf '%s' "${git_sha}" | tr '[:upper:]' '[:lower:]')"
[[ "${git_sha}" =~ ^[0-9a-f]{40,64}$ ]] || die "Git SHA must be 40 to 64 hexadecimal characters"

build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
readonly build_time
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly timestamp
readonly short_sha="${git_sha:0:12}"
readonly release_id="${timestamp}-${short_sha}"
readonly final_release="${RELEASES_ROOT}/${release_id}"
backup_path="${MANUAL_BACKUP_ROOT}/pre-release-${release_id}.db"
readonly backup_path
readonly session_backup_path="${MANUAL_BACKUP_ROOT}/pre-release-${release_id}.session"
readonly config_backup_path="${MANUAL_BACKUP_ROOT}/pre-release-${release_id}.env"

[[ ! -L "${APP_ROOT}" ]] || die "application root must not be a symlink: ${APP_ROOT}"
[[ ! -L "${RELEASES_ROOT}" ]] || die "releases root must not be a symlink: ${RELEASES_ROOT}"
[[ ! -L "${BACKUP_ROOT}" ]] || die "backup root must not be a symlink: ${BACKUP_ROOT}"
[[ ! -L "${MANUAL_BACKUP_ROOT}" ]] \
  || die "manual backup root must not be a symlink: ${MANUAL_BACKUP_ROOT}"
mkdir -p -- "${APP_ROOT}" "${RELEASES_ROOT}" "${BACKUP_ROOT}"
[[ "$(canonical_path "${APP_ROOT}")" == "${APP_ROOT}" ]] \
  || die "application root resolved unexpectedly: ${APP_ROOT}"
[[ "$(canonical_path "${RELEASES_ROOT}")" == "${RELEASES_ROOT}" ]] \
  || die "releases root resolved unexpectedly: ${RELEASES_ROOT}"
[[ "$(canonical_path "${BACKUP_ROOT}")" == "${BACKUP_ROOT}" ]] \
  || die "backup root resolved unexpectedly: ${BACKUP_ROOT}"
mkdir -p -- "${MANUAL_BACKUP_ROOT}"
[[ "$(canonical_path "${MANUAL_BACKUP_ROOT}")" == "${MANUAL_BACKUP_ROOT}" ]] \
  || die "manual backup root resolved unexpectedly: ${MANUAL_BACKUP_ROOT}"
chown "root:${SERVICE_GROUP}" -- "${APP_ROOT}" "${RELEASES_ROOT}"
chmod 0750 -- "${APP_ROOT}" "${RELEASES_ROOT}"
chown "root:${SERVICE_GROUP}" -- "${BACKUP_ROOT}"
# The service group can create/prune daily backups in the root. The sticky bit
# prevents it from renaming or replacing the root-owned manual backup directory.
chmod 1770 -- "${BACKUP_ROOT}"
chown root:root -- "${MANUAL_BACKUP_ROOT}"
chmod 0700 -- "${MANUAL_BACKUP_ROOT}"
[[ ! -e "${final_release}" && ! -L "${final_release}" ]] \
  || die "release already exists: ${final_release}"
[[ ! -e "${backup_path}" && ! -L "${backup_path}" ]] \
  || die "database backup already exists: ${backup_path}"
[[ ! -e "${session_backup_path}" && ! -L "${session_backup_path}" ]] \
  || die "Telegram session backup already exists: ${session_backup_path}"
[[ ! -e "${config_backup_path}" && ! -L "${config_backup_path}" ]] \
  || die "configuration backup already exists: ${config_backup_path}"

if [[ -e "${CURRENT_LINK}" && ! -L "${CURRENT_LINK}" ]]; then
  die "current exists but is not a symbolic link: ${CURRENT_LINK}"
fi
if [[ -L "${CURRENT_LINK}" ]]; then
  previous_target="$(canonical_path "${CURRENT_LINK}")"
  releases_root_real="$(canonical_path "${RELEASES_ROOT}")"
  is_path_below "${previous_target}" "${releases_root_real}" \
    || die "current points outside ${RELEASES_ROOT}: ${previous_target}"
  [[ "$(dirname -- "${previous_target}")" == "${releases_root_real}" ]] \
    || die "current must point to a direct child of ${RELEASES_ROOT}: ${previous_target}"
  [[ "$(basename -- "${previous_target}")" \
    =~ ^[0-9]{8}T[0-9]{6}Z(-[0-9a-f]{7,12})?$ ]] \
    || die "current release name is invalid: ${previous_target}"
  [[ -d "${previous_target}" ]] || die "current points to a missing release: ${previous_target}"
fi

[[ -f "${DATABASE_PATH}" && ! -L "${DATABASE_PATH}" ]] \
  || die "production database must be a regular non-symlink file: ${DATABASE_PATH}"
[[ "$(canonical_path "${DATABASE_PATH}")" == "${DATABASE_PATH}" ]] \
  || die "production database path resolved unexpectedly: ${DATABASE_PATH}"
database_owner_uid="$(stat -c '%u' -- "${DATABASE_PATH}")"
database_owner_gid="$(stat -c '%g' -- "${DATABASE_PATH}")"
database_mode="$(stat -c '%a' -- "${DATABASE_PATH}")"
[[ "${database_owner_uid}" =~ ^[0-9]+$ && "${database_owner_gid}" =~ ^[0-9]+$ ]] \
  || die "production database ownership could not be determined"
[[ "${database_mode}" =~ ^[0-7]{3,4}$ ]] \
  || die "production database permissions could not be determined"
database_metadata_captured=1

[[ -f "${TELEGRAM_SESSION_PATH}" && ! -L "${TELEGRAM_SESSION_PATH}" ]] \
  || die "Telegram session must be a regular non-symlink file: ${TELEGRAM_SESSION_PATH}"
[[ "$(canonical_path "${TELEGRAM_SESSION_PATH}")" == "${TELEGRAM_SESSION_PATH}" ]] \
  || die "Telegram session path resolved unexpectedly: ${TELEGRAM_SESSION_PATH}"
session_mode="$(stat -c '%a' -- "${TELEGRAM_SESSION_PATH}")"
[[ "${session_mode}" =~ ^[0-7]{3,4}$ ]] \
  || die "Telegram session permissions could not be determined"
(( (8#${session_mode} & ~8#600) == 0 )) \
  || die "Telegram session permissions must not be wider than 0600"

[[ -f "${CONFIG_PATH}" && ! -L "${CONFIG_PATH}" ]] \
  || die "configuration must be a regular non-symlink file: ${CONFIG_PATH}"
[[ "$(canonical_path "${CONFIG_PATH}")" == "${CONFIG_PATH}" ]] \
  || die "configuration path resolved unexpectedly: ${CONFIG_PATH}"
config_owner="$(stat -c '%u:%g' -- "${CONFIG_PATH}")"
[[ "${config_owner}" == "0:0" ]] || die "configuration must be owned by root:root"
config_mode="$(stat -c '%a' -- "${CONFIG_PATH}")"
[[ "${config_mode}" == "600" || "${config_mode}" == "400" ]] \
  || die "configuration permissions must be 0600 or 0400, got ${config_mode}"

config_backup_staging="${config_backup_path}.tmp.$$"
[[ ! -e "${config_backup_staging}" && ! -L "${config_backup_staging}" ]] \
  || die "temporary configuration backup already exists: ${config_backup_staging}"
log "creating protected configuration backup at ${config_backup_path}"
cp -- "${CONFIG_PATH}" "${config_backup_staging}"
chown root:root -- "${config_backup_staging}"
chmod 0600 -- "${config_backup_staging}"
cmp -s -- "${CONFIG_PATH}" "${config_backup_staging}" \
  || die "configuration backup verification failed"
[[ "$(stat -c '%u:%g:%a' -- "${config_backup_staging}")" == "0:0:600" ]] \
  || die "configuration backup permissions verification failed"
mv -- "${config_backup_staging}" "${config_backup_path}"
config_backup_staging=""

if [[ -n "${requested_ai_model}" ]]; then
  candidate_config_path="$(
    mktemp "${MANUAL_BACKUP_ROOT}/.candidate-env-${release_id}.XXXXXX"
  )" || die "failed to create candidate configuration path"
  [[ -f "${candidate_config_path}" && ! -L "${candidate_config_path}" ]] \
    || die "candidate configuration path is not a regular file"
  cp -- "${CONFIG_PATH}" "${candidate_config_path}"
  chown root:root -- "${candidate_config_path}"
  chmod 0600 -- "${candidate_config_path}"
  cmp -s -- "${CONFIG_PATH}" "${candidate_config_path}" \
    || die "candidate configuration copy verification failed"
  candidate_update_result="$(
    update_ai_model_config "${candidate_config_path}" "${requested_ai_model}"
  )" || die "failed to prepare candidate AI_MODEL"
  [[ "${candidate_update_result}" == "changed" ]] \
    || die "--ai-model is only valid when migrating from a different configured model"
  probe_config_path="${candidate_config_path}"
fi

build_path="$(mktemp -d /tmp/nexa-guardian-build.XXXXXX)"
if [[ "${artifact_mode}" == "source" ]]; then
  log "building wheel from ${artifact_path}"
  if "${PYTHON_BIN}" -c 'import build' >/dev/null 2>&1; then
    "${PYTHON_BIN}" -m build --wheel --outdir "${build_path}" "${artifact_path}"
  elif command -v uv >/dev/null 2>&1; then
    uv build --wheel --out-dir "${build_path}" "${artifact_path}"
  else
    "${PYTHON_BIN}" -m pip wheel --no-deps --wheel-dir "${build_path}" "${artifact_path}"
  fi
  mapfile -t built_wheels < <(find "${build_path}" -maxdepth 1 -type f -name 'nexa_guardian-*.whl' -print)
  [[ ${#built_wheels[@]} -eq 1 ]] \
    || die "source build must produce exactly one nexa_guardian wheel"
  artifact_path="${built_wheels[0]}"
fi

"${PYTHON_BIN}" - "${artifact_path}" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as wheel:
    invalid_member = wheel.testzip()
    if invalid_member is not None:
        raise RuntimeError(f"invalid wheel member: {invalid_member}")
PY
wheel_sha256="$(sha256sum -- "${artifact_path}" | awk '{print $1}')"
readonly wheel_sha256

stage_path="${RELEASES_ROOT}/.staging-${release_id}.$$"
mkdir -m 0755 -- "${stage_path}"
mkdir -m 0755 -- "${stage_path}/artifacts"
cp -- "${artifact_path}" "${stage_path}/artifacts/"
copied_wheel="${stage_path}/artifacts/$(basename "${artifact_path}")"
[[ "$(sha256sum -- "${copied_wheel}" | awk '{print $1}')" == "${wheel_sha256}" ]] \
  || die "copied wheel checksum does not match source artifact"

"${PYTHON_BIN}" - \
  "${stage_path}/release.json" \
  "${git_sha}" \
  "${build_time}" \
  "${wheel_sha256}" \
  "${schema_version}" \
  "$(basename "${artifact_path}")" \
  "${allow_model_cooldown}" <<'PY'
import json
import os
import sys

(
    path,
    git_sha,
    build_time,
    wheel_sha256,
    schema_version,
    artifact,
    allow_model_cooldown,
) = sys.argv[1:]
metadata = {
    "application": "nexa-guardian",
    "git_sha": git_sha,
    "build_time_utc": build_time,
    "wheel_sha256": wheel_sha256,
    "schema_version": int(schema_version),
    "artifact": artifact,
    "gateway_probe_policy": (
        "allow_model_cooldown" if allow_model_cooldown == "1" else "strict"
    ),
}
with open(path, "x", encoding="utf-8") as stream:
    json.dump(metadata, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
PY
chmod 0644 -- "${stage_path}/release.json"

mv -T -- "${stage_path}" "${final_release}"
candidate_release_path="${final_release}"
stage_path=""
copied_wheel="${final_release}/artifacts/$(basename "${artifact_path}")"
[[ "$(sha256sum -- "${copied_wheel}" | awk '{print $1}')" == "${wheel_sha256}" ]] \
  || die "final wheel checksum does not match source artifact"

# Build the virtual environment at its permanent path. Python entry-point
# shebangs embed the venv path and break if a completed venv is moved later.
log "creating isolated virtual environment"
"${PYTHON_BIN}" -m venv "${final_release}/.venv"
"${final_release}/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-cache-dir "${copied_wheel}"
"${final_release}/.venv/bin/python" -c 'import nexa_guardian'
log "probing ${CODE_AI_MODEL} adaptive-reasoning capabilities and production settings"
probe_arguments=(--env-file "${probe_config_path}" --require-production-settings)
if (( allow_model_cooldown == 1 )); then
  probe_arguments+=(--allow-model-cooldown)
  log "allowing only an operator-authorized exact 429 model_cooldown during this migration"
fi
"${final_release}/.venv/bin/nexa-guardian-probe" \
  "${probe_arguments[@]}" \
  || die "gateway capability probe failed; production was not switched"
if [[ -n "${candidate_config_path}" ]]; then
  rm -f -- "${candidate_config_path}" || die "failed to remove candidate configuration copy"
  candidate_config_path=""
  probe_config_path="${CONFIG_PATH}"
fi
chown -hR "root:${SERVICE_GROUP}" -- "${final_release}"
chmod -R u=rwX,g=rX,o= -- "${final_release}"

systemctl is-enabled --quiet "${BACKUP_TIMER_NAME}" \
  || die "backup timer must be enabled before release"
systemctl is-active --quiet "${BACKUP_TIMER_NAME}" \
  || die "backup timer must be active before release"
log "pausing ${BACKUP_TIMER_NAME} during the final database transition"
backup_timer_paused=1
systemctl stop "${BACKUP_TIMER_NAME}" || die "failed to pause backup timer"
wait_for_backup_service_idle \
  || die "database backup service did not become idle after pausing the timer"

systemctl is-active --quiet "${SERVICE_NAME}" \
  || die "service must be active before the final database backup"
log "stopping ${SERVICE_NAME} for a final consistent database backup"
service_stopped=1
systemctl stop "${SERVICE_NAME}" || die "failed to stop service before database backup"
systemctl is-active --quiet "${SERVICE_NAME}" \
  && die "service is still active after stop"

log "checking raffle migration safety"
"${PYTHON_BIN}" - "${DATABASE_PATH}" "${schema_version}" <<'PY'
import sqlite3
import sys
from pathlib import Path

database_uri = f"{Path(sys.argv[1]).resolve().as_uri()}?mode=ro"
target_schema = int(sys.argv[2])
connection = sqlite3.connect(database_uri, uri=True, timeout=15)
try:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'raffles'"
    ).fetchone()
    if table is not None:
        running = connection.execute(
            "SELECT COUNT(*) FROM raffles WHERE status = 'running'"
        ).fetchone()
        current_schema = connection.execute("PRAGMA user_version").fetchone()
        if (
            running is not None
            and int(running[0]) != 0
            and current_schema is not None
            and int(current_schema[0]) < target_schema
        ):
            raise RuntimeError("an old-schema raffle is still running; release aborted")
finally:
    connection.close()
PY

log "creating validated database backup at ${backup_path}"
"${PYTHON_BIN}" - "${DATABASE_PATH}" "${backup_path}" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_path, backup_path = sys.argv[1:]
source_uri = f"{Path(source_path).resolve().as_uri()}?mode=ro"
source = None
target = None
try:
    source = sqlite3.connect(source_uri, uri=True, timeout=15)
    target = sqlite3.connect(backup_path, timeout=15)
    source.execute("PRAGMA busy_timeout = 15000")
    source.backup(target)
    target.commit()
    quick_check = target.execute("PRAGMA quick_check").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise RuntimeError(f"backup quick_check failed: {quick_check!r}")
    foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError(f"backup foreign_key_check failed: {foreign_keys!r}")
except BaseException:
    if target is not None:
        target.close()
    if source is not None:
        source.close()
    try:
        os.unlink(backup_path)
    except FileNotFoundError:
        pass
    raise
else:
    target.close()
    source.close()
os.chmod(backup_path, 0o600)
PY
[[ -f "${backup_path}" && ! -L "${backup_path}" ]] \
  || die "validated database backup is unavailable: ${backup_path}"
[[ "$(stat -c '%u:%g:%a' -- "${backup_path}")" == "0:0:600" ]] \
  || die "database backup ownership or permissions verification failed"
database_backup_sha256="$(sha256sum -- "${backup_path}" | awk '{print $1}')"
[[ "${database_backup_sha256}" =~ ^[0-9a-f]{64}$ ]] \
  || die "database backup checksum verification failed"
database_backup_validated=1

log "creating validated Telegram session backup at ${session_backup_path}"
"${PYTHON_BIN}" - "${TELEGRAM_SESSION_PATH}" "${session_backup_path}" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_path, backup_path = sys.argv[1:]
source_uri = f"{Path(source_path).resolve().as_uri()}?mode=ro"
source = None
target = None
try:
    source = sqlite3.connect(source_uri, uri=True, timeout=15)
    target = sqlite3.connect(backup_path, timeout=15)
    source.execute("PRAGMA busy_timeout = 15000")
    source.backup(target)
    target.commit()
    quick_check = target.execute("PRAGMA quick_check").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise RuntimeError(f"session backup quick_check failed: {quick_check!r}")
    foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError("session backup foreign_key_check failed")
except BaseException:
    if target is not None:
        target.close()
    if source is not None:
        source.close()
    try:
        os.unlink(backup_path)
    except FileNotFoundError:
        pass
    raise
else:
    target.close()
    source.close()
os.chmod(backup_path, 0o600)
PY
[[ -f "${session_backup_path}" && ! -L "${session_backup_path}" ]] \
  || die "validated Telegram session backup is unavailable: ${session_backup_path}"
[[ "$(stat -c '%u:%g:%a' -- "${session_backup_path}")" == "0:0:600" ]] \
  || die "Telegram session backup ownership or permissions verification failed"
session_backup_sha256="$(sha256sum -- "${session_backup_path}" | awk '{print $1}')"
[[ "${session_backup_sha256}" =~ ^[0-9a-f]{64}$ ]] \
  || die "Telegram session backup checksum verification failed"

if [[ -n "${requested_ai_model}" ]]; then
  log "atomically applying AI_MODEL=${requested_ai_model} immediately before code switch"
  config_transition=1
  production_update_result="$(
    update_ai_model_config "${CONFIG_PATH}" "${requested_ai_model}"
  )" || die "failed to apply production AI_MODEL transactionally"
  [[ "${production_update_result}" == "changed" ]] \
    || die "production AI_MODEL changed unexpectedly during release"
fi

log "switching current to ${final_release}"
switch_current_to "${final_release}"

if ! systemctl start "${SERVICE_NAME}"; then
  die "systemctl start failed"
fi
service_stopped=0

healthy=0
for _attempt in {1..10}; do
  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    main_pid="$(systemctl show "${SERVICE_NAME}" --property=MainPID --value)"
    exec_status="$(systemctl show "${SERVICE_NAME}" --property=ExecMainStatus --value)"
    if [[ "${main_pid}" =~ ^[1-9][0-9]*$ && "${exec_status}" == "0" ]]; then
      healthy=1
      break
    fi
  fi
  sleep 1
done
(( healthy == 1 )) || die "service did not become healthy"

# Require the service to remain active after the initial successful probe.
sleep 5
systemctl is-active --quiet "${SERVICE_NAME}" || die "service exited during stability check"
systemctl is-enabled --quiet "${SERVICE_NAME}" || die "service is not enabled"
restart_count="$(systemctl show "${SERVICE_NAME}" --property=NRestarts --value)"
[[ "${restart_count}" == "0" ]] || die "service restarted during health check: ${restart_count}"

"${PYTHON_BIN}" - "${DATABASE_PATH}" "${schema_version}" <<'PY'
import sqlite3
import sys
from pathlib import Path

database_uri = f"{Path(sys.argv[1]).resolve().as_uri()}?mode=ro"
expected_schema_version = int(sys.argv[2])
connection = sqlite3.connect(database_uri, uri=True, timeout=15)
try:
    connection.execute("PRAGMA busy_timeout = 15000")
    quick_check = connection.execute("PRAGMA quick_check").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise RuntimeError(f"production quick_check failed: {quick_check!r}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError(f"production foreign_key_check failed: {foreign_keys!r}")
    schema_version = connection.execute("PRAGMA user_version").fetchone()
    if schema_version is None or schema_version[0] != expected_schema_version:
        raise RuntimeError(
            "production schema version mismatch: "
            f"expected {expected_schema_version}, got {schema_version!r}"
        )
finally:
    connection.close()
PY

log "running one verified database and Telegram session backup with the new release"
backup_started_epoch="$(date +%s)"
systemctl start "${BACKUP_SERVICE_NAME}" || die "new release database backup failed"
wait_for_backup_service_idle \
  || die "new release database backup did not return to idle"
backup_result="$(systemctl show "${BACKUP_SERVICE_NAME}" --property=Result --value)"
backup_exec_status="$(
  systemctl show "${BACKUP_SERVICE_NAME}" --property=ExecMainStatus --value
)"
[[ "${backup_result}" == "success" && "${backup_exec_status}" == "0" ]] \
  || die "new release database backup did not complete successfully"
"${PYTHON_BIN}" - "${BACKUP_ROOT}" "${backup_started_epoch}" <<'PY'
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
started_at = int(sys.argv[2])
pattern = re.compile(r"(guardian|telegram-session)-(\d{8}T\d{6}Z)\.(db|session)")
pairs: dict[str, dict[str, Path]] = {}
for path in root.iterdir():
    match = pattern.fullmatch(path.name)
    if match is None or path.is_symlink() or not path.is_file():
        continue
    if path.stat().st_mtime < started_at - 1:
        continue
    pairs.setdefault(match.group(2), {})[match.group(1)] = path

complete = [(stamp, files) for stamp, files in pairs.items() if len(files) == 2]
if not complete:
    raise RuntimeError("new backup did not create a matching database/session pair")
stamp, files = max(complete)
for label in ("guardian", "telegram-session"):
    path = files[label]
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError(f"backup permissions are not 0600: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=15)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise RuntimeError(f"backup quick_check failed: {path}")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError(f"backup foreign_key_check failed: {path}")
    finally:
        connection.close()
print(f"[release] verified daily backup pair: {stamp}")
PY

systemctl start "${BACKUP_TIMER_NAME}" || die "failed to resume backup timer"
systemctl is-enabled --quiet "${BACKUP_TIMER_NAME}" || die "backup timer is not enabled"
systemctl is-active --quiet "${BACKUP_TIMER_NAME}" || die "backup timer is not active"
release_stable=1
backup_timer_paused=0
log "release is healthy; pruning old code releases"
mapfile -t release_names < <(
  find "${RELEASES_ROOT}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r
)
old_releases_kept=0
if ! releases_root_real="$(canonical_path "${RELEASES_ROOT}")" \
  || ! current_target="$(canonical_path "${CURRENT_LINK}")" \
  || ! is_path_below "${current_target}" "${releases_root_real}"; then
  printf '[release] WARNING: release is live, but old-release pruning was skipped\n' >&2
else
  for candidate_name in "${release_names[@]}"; do
    if [[ ! "${candidate_name}" =~ ^[0-9]{8}T[0-9]{6}Z(-[0-9a-f]{7,12})?$ ]]; then
      continue
    fi
    candidate="${RELEASES_ROOT}/${candidate_name}"
    if [[ -L "${candidate}" ]] || ! candidate_real="$(canonical_path "${candidate}")" \
      || ! is_path_below "${candidate_real}" "${releases_root_real}"; then
      printf '[release] WARNING: unsafe old-release candidate was not removed: %s\n' \
        "${candidate}" >&2
      continue
    fi
    if is_same_or_below "${current_target}" "${candidate_real}"; then
      continue
    fi
    ((old_releases_kept += 1))
    if (( old_releases_kept <= 2 )); then
      continue
    fi
    log "removing old code release ${candidate}"
    if ! safe_remove_tree "${candidate_real}" "${RELEASES_ROOT}"; then
      printf '[release] WARNING: release is live, but old release could not be removed: %s\n' \
        "${candidate_real}" >&2
    fi
  done
fi

log "deployed ${release_id}"
log "configuration backup: ${config_backup_path}"
log "database backup: ${backup_path}"
log "Telegram session backup: ${session_backup_path}"
log "wheel sha256: ${wheel_sha256}"
