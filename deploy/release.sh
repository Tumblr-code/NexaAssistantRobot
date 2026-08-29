#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 0077

readonly SERVICE_NAME="nexa-guardian.service"
readonly SERVICE_USER="nexa-guardian"
readonly SERVICE_GROUP="nexa-guardian"
readonly APP_ROOT="/opt/nexa-guardian"
readonly RELEASES_ROOT="${APP_ROOT}/releases"
readonly CURRENT_LINK="${APP_ROOT}/current"
readonly CONFIG_PATH="/etc/nexa-guardian/guardian.env"
readonly DATABASE_PATH="/var/lib/nexa-guardian/guardian.db"
readonly BACKUP_ROOT="/var/backups/nexa-guardian"
readonly MANUAL_BACKUP_ROOT="${BACKUP_ROOT}/manual"
readonly LOCK_PATH="/run/lock/nexa-guardian-release.lock"
readonly PYTHON_BIN="/usr/bin/python3"
readonly CODE_SCHEMA_VERSION="4"

artifact_mode=""
artifact_path=""
git_sha=""
schema_version=""
stage_path=""
build_path=""
switch_path=""
config_backup_staging=""
backup_path=""
database_backup_sha256=""
database_backup_validated=0
database_metadata_captured=0
database_owner_uid=""
database_owner_gid=""
database_mode=""
previous_target=""
switched=0
release_stable=0
service_stopped=0

usage() {
  cat <<'EOF'
Usage:
  sudo deploy/release.sh --wheel /absolute/path/nexa_guardian-*.whl \
    --git-sha <full-git-sha> --schema-version <integer>

  sudo deploy/release.sh --source /absolute/path/to/repository \
    [--git-sha <full-git-sha>] --schema-version <integer>

The script always deploys to /opt/nexa-guardian, backs up the protected
configuration, builds and probes the candidate, then stops the service for a
final consistent database backup before atomically switching current. It keeps
current plus the two newest older releases after a health check.
EOF
}

log() {
  printf '[release] %s\n' "$*"
}

die() {
  printf '[release] ERROR: %s\n' "$*" >&2
  exit 1
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

  if (( switched == 0 || release_stable == 1 )); then
    return
  fi

  printf '[release] deployment failed after switching current; rolling back\n' >&2
  if ! systemctl stop "${SERVICE_NAME}"; then
    printf '[release] CRITICAL: failed to stop service; database was not changed\n' >&2
    return
  fi
  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    printf '[release] CRITICAL: service is still active; database was not changed\n' >&2
    return
  fi

  if (( database_backup_validated != 1 || database_metadata_captured != 1 )) \
    || [[ -z "${backup_path}" || -z "${database_backup_sha256}" \
      || ! -f "${backup_path}" || -L "${backup_path}" ]]; then
    printf '[release] CRITICAL: validated database backup is unavailable; database was not changed\n' >&2
    return
  fi
  if ! is_path_below "$(canonical_path "${backup_path}")" \
    "$(canonical_path "${MANUAL_BACKUP_ROOT}")"; then
    printf '[release] CRITICAL: database backup resolved outside manual backup root\n' >&2
    return
  fi
  if ! current_backup_sha256="$(sha256sum -- "${backup_path}" | awk '{print $1}')"; then
    printf '[release] CRITICAL: database backup checksum could not be read; database was not changed\n' >&2
    return
  fi
  if [[ "${current_backup_sha256}" != "${database_backup_sha256}" ]]; then
    printf '[release] CRITICAL: database backup checksum changed; database was not changed\n' >&2
    return
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
    return
  fi

  if [[ -n "${previous_target}" && -d "${previous_target}" ]]; then
    if ln -s -- "${previous_target}" "${rollback_link}" \
      && mv -Tf -- "${rollback_link}" "${CURRENT_LINK}"; then
      if systemctl start "${SERVICE_NAME}" \
        && systemctl is-active --quiet "${SERVICE_NAME}"; then
        printf '[release] rollback restored the database and previous release\n' >&2
      else
        printf '[release] CRITICAL: previous release did not start cleanly\n' >&2
      fi
    else
      printf '[release] CRITICAL: failed to restore previous current link; service remains stopped\n' >&2
      [[ ! -L "${rollback_link}" ]] || rm -f -- "${rollback_link}"
    fi
  elif [[ -L "${CURRENT_LINK}" ]]; then
    rm -f -- "${CURRENT_LINK}"
    printf '[release] WARNING: no previous release exists; service remains stopped\n' >&2
  fi
}

cleanup() {
  local exit_code=$?
  restore_previous_release

  if (( switched == 0 && service_stopped == 1 && release_stable == 0 )); then
    if systemctl start "${SERVICE_NAME}"; then
      printf '[release] previous service restarted after pre-switch failure\n' >&2
    else
      printf '[release] CRITICAL: failed to restart previous service after pre-switch failure\n' >&2
    fi
  fi

  if [[ -n "${stage_path}" && -d "${stage_path}" ]]; then
    rm -rf --one-file-system -- "${stage_path}"
  fi
  if [[ -n "${build_path}" && -d "${build_path}" ]]; then
    rm -rf --one-file-system -- "${build_path}"
  fi
  if [[ -n "${switch_path}" && -L "${switch_path}" ]]; then
    rm -f -- "${switch_path}"
  fi
  if [[ -n "${config_backup_staging}" && -f "${config_backup_staging}" ]]; then
    rm -f -- "${config_backup_staging}"
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
[[ "${artifact_path}" = /* ]] || die "artifact path must be absolute"
[[ ${EUID} -eq 0 ]] || die "run this script as root"

require_command flock
require_command getent
require_command cmp
require_command mv
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
[[ ! -e "${config_backup_path}" && ! -L "${config_backup_path}" ]] \
  || die "configuration backup already exists: ${config_backup_path}"

if [[ -e "${CURRENT_LINK}" && ! -L "${CURRENT_LINK}" ]]; then
  die "current exists but is not a symbolic link: ${CURRENT_LINK}"
fi
if [[ -L "${CURRENT_LINK}" ]]; then
  previous_target="$(canonical_path "${CURRENT_LINK}")"
  is_path_below "${previous_target}" "$(canonical_path "${RELEASES_ROOT}")" \
    || die "current points outside ${RELEASES_ROOT}: ${previous_target}"
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

log "creating isolated virtual environment"
"${PYTHON_BIN}" -m venv "${stage_path}/.venv"
"${stage_path}/.venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
  "${copied_wheel}"
"${stage_path}/.venv/bin/python" -c 'import nexa_guardian'
log "probing GPT-5.6 capabilities and required production settings"
"${stage_path}/.venv/bin/nexa-guardian-probe" \
  --env-file "${CONFIG_PATH}" --require-production-settings \
  || die "gateway capability probe failed; production was not switched"

"${PYTHON_BIN}" - \
  "${stage_path}/release.json" \
  "${git_sha}" \
  "${build_time}" \
  "${wheel_sha256}" \
  "${schema_version}" \
  "$(basename "${artifact_path}")" <<'PY'
import json
import os
import sys

path, git_sha, build_time, wheel_sha256, schema_version, artifact = sys.argv[1:]
metadata = {
    "application": "nexa-guardian",
    "git_sha": git_sha,
    "build_time_utc": build_time,
    "wheel_sha256": wheel_sha256,
    "schema_version": int(schema_version),
    "artifact": artifact,
}
with open(path, "x", encoding="utf-8") as stream:
    json.dump(metadata, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
PY
chmod 0644 -- "${stage_path}/release.json"
chown -hR "root:${SERVICE_GROUP}" -- "${stage_path}"
chmod -R u=rwX,g=rX,o= -- "${stage_path}"

mv -- "${stage_path}" "${final_release}"
stage_path=""

systemctl is-active --quiet "${SERVICE_NAME}" \
  || die "service must be active before the final database backup"
log "stopping ${SERVICE_NAME} for a final consistent database backup"
systemctl stop "${SERVICE_NAME}" || die "failed to stop service before database backup"
systemctl is-active --quiet "${SERVICE_NAME}" \
  && die "service is still active after stop"
service_stopped=1

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

release_stable=1
log "release is healthy; pruning old code releases"
mapfile -t release_names < <(
  find "${RELEASES_ROOT}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r
)
old_releases_kept=0
releases_root_real="$(canonical_path "${RELEASES_ROOT}")"
current_target="$(canonical_path "${CURRENT_LINK}")"
is_path_below "${current_target}" "${releases_root_real}" \
  || die "current points outside releases root: ${current_target}"
for candidate_name in "${release_names[@]}"; do
  if [[ ! "${candidate_name}" =~ ^[0-9]{8}T[0-9]{6}Z(-[0-9a-f]{7,12})?$ ]]; then
    continue
  fi
  candidate="${RELEASES_ROOT}/${candidate_name}"
  candidate_real="$(canonical_path "${candidate}")"
  is_path_below "${candidate_real}" "${releases_root_real}" \
    || die "refusing to prune path outside releases root: ${candidate_real}"
  if [[ "${candidate_real}" == "${current_target}" ]]; then
    continue
  fi
  ((old_releases_kept += 1))
  if (( old_releases_kept <= 2 )); then
    continue
  fi
  [[ "${candidate_real}" != "${current_target}" ]] \
    || die "refusing to prune current release: ${candidate_real}"
  [[ ! -L "${candidate}" ]] || die "refusing to recursively remove a symlink: ${candidate}"
  log "removing old code release ${candidate}"
  rm -rf --one-file-system -- "${candidate}"
done

log "deployed ${release_id}"
log "configuration backup: ${config_backup_path}"
log "database backup: ${backup_path}"
log "wheel sha256: ${wheel_sha256}"
