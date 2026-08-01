#!/bin/bash -p

set -Eeuo pipefail
umask 022

readonly FONT_FAMILY="Noto Sans CJK SC"
readonly TRUSTED_COMMAND_PATH="/usr/sbin:/usr/bin:/sbin:/bin"
readonly TRUSTED_TEMPORARY_ROOT="/tmp"
readonly -a SYSTEM_PACKAGES=(
  python3.12
  python3.12-venv
  ffmpeg
  fontconfig
  fonts-noto-cjk
  ca-certificates
)

wheel_path=""
wheel_sha256=""
wheelhouse_path=""
runtime_lock_path=""
runtime_lock_sha256=""
apt_snapshot_id=""
installation_prefix=""
work_directory=""
smoke_directory=""
final_directory=""
current_link=""
previous_current_target=""
replacement_link=""
transaction_directory=""
created_version=0
switched_current=0
reused_version=0
transaction_active=0
prefix_locked=0

fail() {
  printf '生产安装失败：%s\n' "$*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
用法：install-production.sh \
  --wheel PATH --wheel-sha256 SHA256 \
  --wheelhouse DIR \
  --runtime-lock PATH --runtime-lock-sha256 SHA256 \
  --apt-snapshot-id YYYYMMDDTHHMMSSZ \
  --prefix ABSOLUTE_PATH
EOF
}

fsync_directory() {
  python3.12 -I - "$1" <<'PY'
from pathlib import Path
import os
import sys

descriptor = os.open(Path(sys.argv[1]), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

ready_identity_is_valid() {
  local directory=$1
  local manifest="${directory}/installation-manifest.json"
  local ready="${directory}/READY"
  [[ -f "${manifest}" && ! -L "${manifest}" ]] || return 1
  [[ -f "${ready}" && ! -L "${ready}" ]] || return 1
  python3.12 -I - "${manifest}" "${ready}" >/dev/null 2>&1 <<'PY'
import hashlib
import json
from pathlib import Path
import sys

manifest_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
try:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    ready = json.loads(ready_path.read_bytes())
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
if manifest.get("schema_version") != "production-installation-manifest.v1":
    raise SystemExit(1)
expected = {
    "installation_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "schema_version": "production-installation-ready.v1",
}
if ready != expected:
    raise SystemExit(1)
PY
}

current_points_to() {
  local expected_directory=$1
  local target resolved
  [[ -L "${current_link}" ]] || return 1
  target=$(readlink -- "${current_link}")
  if [[ "${target}" == /* ]]; then
    resolved=$(realpath -m -- "${target}")
  else
    resolved=$(realpath -m -- "${installation_prefix}/${target}")
  fi
  [[ "${resolved}" == "${expected_directory}" ]]
}

restore_current_link() {
  replacement_link="${installation_prefix}/.current.restore.$$"
  rm -f -- "${replacement_link}"
  if [[ -n "${previous_current_target}" ]]; then
    ln -s -- "${previous_current_target}" "${replacement_link}"
    mv -Tf -- "${replacement_link}" "${current_link}"
    replacement_link=""
  else
    rm -f -- "${current_link}"
  fi
  fsync_directory "${installation_prefix}"
}

recover_interrupted_transaction() {
  transaction_directory="${installation_prefix}/.install-transaction"
  if [[ ! -e "${transaction_directory}" && ! -L "${transaction_directory}" ]]; then
    transaction_directory=""
    return 0
  fi
  [[ -d "${transaction_directory}" && ! -L "${transaction_directory}" ]] \
    || fail "安装事务记录已损坏"
  local candidate_file="${transaction_directory}/candidate-version"
  local created_file="${transaction_directory}/created-version"
  [[ -f "${candidate_file}" && ! -L "${candidate_file}" ]] \
    || fail "安装事务缺少候选版本"
  [[ -f "${created_file}" && ! -L "${created_file}" ]] \
    || fail "安装事务缺少创建身份"
  local candidate_version created_identity candidate_directory
  candidate_version=$(<"${candidate_file}")
  created_identity=$(<"${created_file}")
  [[ "${candidate_version}" =~ ^[0-9]+(\.[0-9]+)*$ ]] \
    || fail "安装事务候选版本不合法"
  [[ "${created_identity}" == "0" || "${created_identity}" == "1" ]] \
    || fail "安装事务创建身份不合法"
  candidate_directory="${installation_prefix}/versions/${candidate_version}"

  local has_previous=0 has_absent=0
  [[ -L "${transaction_directory}/previous-current" ]] && has_previous=1
  [[ -f "${transaction_directory}/previous-absent" \
    && ! -L "${transaction_directory}/previous-absent" ]] && has_absent=1
  (( has_previous + has_absent == 1 )) || fail "安装事务回滚身份已损坏"

  if current_points_to "${candidate_directory}" \
    && ready_identity_is_valid "${candidate_directory}"; then
    rm -rf -- "${transaction_directory}"
    fsync_directory "${installation_prefix}"
    transaction_directory=""
    return 0
  fi

  if current_points_to "${candidate_directory}"; then
    if (( has_previous == 1 )); then
      previous_current_target=$(readlink -- \
        "${transaction_directory}/previous-current")
    else
      previous_current_target=""
    fi
    restore_current_link
  elif [[ -e "${current_link}" && ! -L "${current_link}" ]]; then
    fail "current 必须是符号链接或不存在"
  fi
  if [[ "${created_identity}" == "1" ]] \
    && ! ready_identity_is_valid "${candidate_directory}"; then
    rm -rf -- "${candidate_directory}"
    fsync_directory "${installation_prefix}/versions"
  fi
  rm -rf -- "${transaction_directory}"
  fsync_directory "${installation_prefix}"
  transaction_directory=""
  previous_current_target=""
}

prepare_install_transaction() {
  local temporary="${installation_prefix}/.install-transaction.$$"
  transaction_directory="${installation_prefix}/.install-transaction"
  [[ ! -e "${transaction_directory}" && ! -L "${transaction_directory}" ]] \
    || fail "已有安装事务尚未恢复"
  rm -rf -- "${temporary}"
  install -d -m 0700 -- "${temporary}"
  printf '%s\n' "${application_version}" >"${temporary}/candidate-version"
  printf '%s\n' "${created_version}" >"${temporary}/created-version"
  chmod 0600 "${temporary}/candidate-version" "${temporary}/created-version"
  if [[ -n "${previous_current_target}" ]]; then
    ln -s -- "${previous_current_target}" "${temporary}/previous-current"
  else
    : >"${temporary}/previous-absent"
    chmod 0600 "${temporary}/previous-absent"
  fi
  python3.12 -I - "${temporary}" <<'PY'
from pathlib import Path
import os
import sys

directory = Path(sys.argv[1])
for path in directory.iterdir():
    if path.is_file() and not path.is_symlink():
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
  mv -- "${temporary}" "${transaction_directory}"
  fsync_directory "${installation_prefix}"
  transaction_active=1
}

commit_install_transaction() {
  rm -rf -- "${transaction_directory}"
  fsync_directory "${installation_prefix}"
  transaction_directory=""
  transaction_active=0
}

validate_managed_directory() {
  local directory=$1
  local label=$2
  local trusted_owner=$3
  local owner mode
  [[ -d "${directory}" && ! -L "${directory}" ]] \
    || fail "${label}必须是非符号链接目录"
  owner=$(stat -c '%u' -- "${directory}") \
    || fail "无法读取${label}所有者"
  [[ "${owner}" == "${trusted_owner}" ]] \
    || fail "${label}必须由安装用户所有"
  mode=$(stat -c '%a' -- "${directory}") \
    || fail "无法读取${label}权限"
  [[ "${mode}" =~ ^[0-7]{3,4}$ ]] || fail "${label}权限格式不合法"
  (( (8#${mode} & 8#022) == 0 )) \
    || fail "${label}不得允许组或其他用户写入"
}

validate_trusted_regular_file() {
  local path=$1
  local label=$2
  local trusted_owner=$3
  local resolved owner mode
  [[ -f "${path}" ]] || fail "${label}必须是普通文件"
  resolved=$(realpath -e -- "${path}") || fail "无法解析${label}"
  [[ -f "${resolved}" && ! -L "${resolved}" ]] \
    || fail "${label}目标必须是普通文件"
  owner=$(stat -c '%u' -- "${resolved}") || fail "无法读取${label}所有者"
  [[ "${owner}" == "${trusted_owner}" ]] || fail "${label}所有者不可信"
  mode=$(stat -c '%a' -- "${resolved}") || fail "无法读取${label}权限"
  [[ "${mode}" =~ ^[0-7]{3,4}$ ]] || fail "${label}权限格式不合法"
  (( (8#${mode} & 8#022) == 0 )) \
    || fail "${label}不得允许组或其他用户写入"
  printf '%s\n' "${resolved}"
}

validate_trusted_temporary_root() {
  local directory=$1
  local trusted_owner=$2
  local owner mode
  [[ -d "${directory}" && ! -L "${directory}" ]] \
    || fail "临时目录根必须是非符号链接目录"
  owner=$(stat -c '%u' -- "${directory}") \
    || fail "无法读取临时目录根所有者"
  [[ "${owner}" == "${trusted_owner}" ]] \
    || fail "临时目录根所有者不可信"
  mode=$(stat -c '%a' -- "${directory}") \
    || fail "无法读取临时目录根权限"
  [[ "${mode}" =~ ^[0-7]{3,4}$ ]] \
    || fail "临时目录根权限格式不合法"
  if (( (8#${mode} & 8#022) != 0 )); then
    (( (8#${mode} & 8#1000) != 0 )) \
      || fail "共享写临时目录根必须启用 sticky 保护"
  fi
}

validate_trusted_directory_chain() {
  local directory=$1
  local trusted_owner=$2
  local component owner mode
  component=${directory}
  while :; do
    [[ -d "${component}" && ! -L "${component}" ]] \
      || fail "安装前缀目录链必须全部由非符号链接目录组成"
    owner=$(stat -c '%u' -- "${component}") \
      || fail "无法读取安装前缀目录链所有者"
    [[ "${owner}" == "0" || "${owner}" == "${trusted_owner}" ]] \
      || fail "安装前缀目录链所有者不可信"
    mode=$(stat -c '%a' -- "${component}") \
      || fail "无法读取安装前缀目录链权限"
    [[ "${mode}" =~ ^[0-7]{3,4}$ ]] \
      || fail "安装前缀目录链权限格式不合法"
    if (( (8#${mode} & 8#022) != 0 )); then
      (( owner == 0 && (8#${mode} & 8#1000) != 0 )) \
        || fail "安装前缀目录链不得允许组或其他用户写入"
    fi
    [[ "${component}" == "/" ]] && break
    component=$(dirname -- "${component}")
  done
}

lock_installation_prefix() {
  local trusted_owner=$1
  local ancestor locked_identity path_identity versions_directory
  (( prefix_locked == 0 )) || return 0
  ancestor=${installation_prefix}
  while [[ ! -e "${ancestor}" && ! -L "${ancestor}" ]]; do
    ancestor=$(dirname -- "${ancestor}")
  done
  validate_trusted_directory_chain "${ancestor}" "${trusted_owner}"
  if [[ -e "${installation_prefix}" || -L "${installation_prefix}" ]]; then
    validate_managed_directory "${installation_prefix}" "安装前缀" \
      "${trusted_owner}"
  else
    validate_managed_directory "${ancestor}" "安装前缀创建锚点" \
      "${trusted_owner}"
    install -d -m 0755 -- "${installation_prefix}"
    validate_managed_directory "${installation_prefix}" "安装前缀" \
      "${trusted_owner}"
  fi
  validate_trusted_directory_chain "${installation_prefix}" "${trusted_owner}"
  [[ ! -L "${installation_prefix}/.install.lock" ]] \
    || fail "安装锁文件不得是符号链接"
  versions_directory="${installation_prefix}/versions"
  if [[ -e "${versions_directory}" || -L "${versions_directory}" ]]; then
    validate_managed_directory "${versions_directory}" "versions 目录" \
      "${trusted_owner}"
  else
    install -d -m 0755 -- "${versions_directory}"
    validate_managed_directory "${versions_directory}" "versions 目录" \
      "${trusted_owner}"
  fi
  exec 9<"${installation_prefix}"
  flock 9
  locked_identity=$(stat -Lc '%d:%i' -- /proc/self/fd/9) \
    || fail "无法读取已锁定安装前缀身份"
  path_identity=$(stat -Lc '%d:%i' -- "${installation_prefix}") \
    || fail "无法重新读取安装前缀身份"
  [[ "${locked_identity}" == "${path_identity}" ]] \
    || fail "安装前缀在加锁期间被替换"
  current_link="${installation_prefix}/current"
  prefix_locked=1
}

cleanup() {
  local status=$?
  local rollback_succeeded=1
  trap - EXIT
  if (( status != 0 )); then
    if (( switched_current == 1 )); then
      restore_current_link || rollback_succeeded=0
    fi
    if (( rollback_succeeded == 1 )); then
      if (( created_version == 1 )) && [[ -n "${final_directory}" ]]; then
        rm -rf -- "${final_directory}"
        fsync_directory "${installation_prefix}/versions" || true
      fi
      if (( transaction_active == 1 )) \
        && [[ -n "${transaction_directory}" ]]; then
        rm -rf -- "${transaction_directory}"
        fsync_directory "${installation_prefix}" || true
      fi
    fi
  fi
  if [[ -n "${replacement_link}" ]]; then
    rm -f -- "${replacement_link}"
  fi
  if [[ -n "${smoke_directory}" ]]; then
    rm -rf -- "${smoke_directory}"
  fi
  if [[ -n "${work_directory}" ]]; then
    rm -rf -- "${work_directory}"
  fi
  exit "${status}"
}

install_production_main() {
local os_release_file=$1
local effective_user_id=$2
local trusted_owner_id=$3
shift 3
trap cleanup EXIT
export TMPDIR="${TRUSTED_TEMPORARY_ROOT}"
export TMP="${TRUSTED_TEMPORARY_ROOT}"
export TEMP="${TRUSTED_TEMPORARY_ROOT}"

while (( $# > 0 )); do
  case "$1" in
    --wheel)
      (( $# >= 2 )) || { usage; fail "--wheel 缺少参数"; }
      wheel_path=$2
      shift 2
      ;;
    --wheel-sha256)
      (( $# >= 2 )) || { usage; fail "--wheel-sha256 缺少参数"; }
      wheel_sha256=$2
      shift 2
      ;;
    --wheelhouse)
      (( $# >= 2 )) || { usage; fail "--wheelhouse 缺少参数"; }
      wheelhouse_path=$2
      shift 2
      ;;
    --runtime-lock)
      (( $# >= 2 )) || { usage; fail "--runtime-lock 缺少参数"; }
      runtime_lock_path=$2
      shift 2
      ;;
    --runtime-lock-sha256)
      (( $# >= 2 )) || { usage; fail "--runtime-lock-sha256 缺少参数"; }
      runtime_lock_sha256=$2
      shift 2
      ;;
    --apt-snapshot-id)
      (( $# >= 2 )) || { usage; fail "--apt-snapshot-id 缺少参数"; }
      apt_snapshot_id=$2
      shift 2
      ;;
    --prefix)
      (( $# >= 2 )) || { usage; fail "--prefix 缺少参数"; }
      installation_prefix=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      fail "未知参数：$1"
      ;;
  esac
done

[[ -n "${wheel_path}" ]] || fail "必须提供 --wheel"
[[ -n "${wheel_sha256}" ]] || fail "必须提供 --wheel-sha256"
[[ -n "${wheelhouse_path}" ]] || fail "必须提供 --wheelhouse"
[[ -n "${runtime_lock_path}" ]] || fail "必须提供 --runtime-lock"
[[ -n "${runtime_lock_sha256}" ]] || fail "必须提供 --runtime-lock-sha256"
[[ -n "${apt_snapshot_id}" ]] || fail "必须提供 --apt-snapshot-id"
[[ -n "${installation_prefix}" ]] || fail "必须提供 --prefix"

[[ "${wheel_sha256}" =~ ^[0-9a-f]{64}$ ]] || fail "wheel SHA-256 格式不合法"
[[ "${runtime_lock_sha256}" =~ ^[0-9a-f]{64}$ ]] || fail "运行锁 SHA-256 格式不合法"
[[ "${apt_snapshot_id}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || fail "APT snapshot ID 格式不合法"
snapshot_calendar_time="${apt_snapshot_id:0:4}-${apt_snapshot_id:4:2}-${apt_snapshot_id:6:2} ${apt_snapshot_id:9:2}:${apt_snapshot_id:11:2}:${apt_snapshot_id:13:2} UTC"
normalized_snapshot=$(date --utc --date "${snapshot_calendar_time}" '+%Y%m%dT%H%M%SZ' 2>/dev/null) \
  || fail "APT snapshot ID 不是有效 UTC 时间"
[[ "${normalized_snapshot}" == "${apt_snapshot_id}" ]] || fail "APT snapshot ID 不是规范 UTC 时间"
[[ "${installation_prefix}" == /* ]] || fail "安装前缀必须是绝对路径"
[[ "${wheel_path}" == *.whl ]] || fail "应用构建物必须是 wheel"
[[ -f "${wheel_path}" && ! -L "${wheel_path}" ]] || fail "wheel 必须是普通文件"
[[ -d "${wheelhouse_path}" && ! -L "${wheelhouse_path}" ]] || fail "wheelhouse 必须是目录"
[[ -f "${runtime_lock_path}" && ! -L "${runtime_lock_path}" ]] || fail "运行锁必须是普通文件"

wheel_path=$(realpath -e -- "${wheel_path}")
wheelhouse_path=$(realpath -e -- "${wheelhouse_path}")
runtime_lock_path=$(realpath -e -- "${runtime_lock_path}")
lexical_installation_prefix=$(realpath -ms -- "${installation_prefix}")
resolved_installation_prefix=$(realpath -m -- "${installation_prefix}")
[[ "${lexical_installation_prefix}" == "${resolved_installation_prefix}" ]] \
  || fail "安装前缀路径不得包含符号链接"
installation_prefix=${resolved_installation_prefix}
case "${installation_prefix}" in
  /|/bin|/boot|/dev|/etc|/home|/opt|/proc|/root|/run|/sbin|/sys|/tmp|/usr|/var)
    fail "安装前缀不能是系统顶层目录"
    ;;
esac
wheel_filename=$(basename -- "${wheel_path}")
runtime_lock_filename=$(basename -- "${runtime_lock_path}")
[[ "${wheel_filename}" =~ ^[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl$ ]] \
  || fail "应用 wheel 文件名不合法"

while IFS= read -r -d '' wheelhouse_artifact; do
  [[ -f "${wheelhouse_artifact}" && ! -L "${wheelhouse_artifact}" ]] \
    || fail "wheelhouse 只能包含本地普通文件"
  [[ "${wheelhouse_artifact}" == *.whl ]] \
    || fail "wheelhouse 只能包含预构建 wheel"
done < <(
  find "${wheelhouse_path}" -mindepth 1 -maxdepth 1 -print0
)

[[ "${effective_user_id}" == "0" ]] || fail "系统包安装必须以 root 运行"
validate_trusted_temporary_root "${TRUSTED_TEMPORARY_ROOT}" 0

os_release_file=$(validate_trusted_regular_file \
  "${os_release_file}" "操作系统身份文件" "${trusted_owner_id}")
read_os_release_value() {
  local key=$1
  local line value
  while IFS= read -r line; do
    if [[ "${line}" == "${key}="* ]]; then
      value=${line#*=}
      value=${value#\"}
      value=${value%\"}
      value=${value#\'}
      value=${value%\'}
      printf '%s\n' "${value}"
      return 0
    fi
  done <"${os_release_file}"
  return 1
}
os_id=$(read_os_release_value ID) || fail "操作系统身份缺少 ID"
os_version=$(read_os_release_value VERSION_ID) || fail "操作系统身份缺少 VERSION_ID"
[[ "${os_id}" == "ubuntu" && "${os_version}" == "24.04" ]] || fail "只认证 Ubuntu 24.04"
architecture=$(dpkg --print-architecture)
[[ "${architecture}" == "amd64" ]] || fail "只认证 amd64 架构"

if [[ -d "${installation_prefix}" && ! -L "${installation_prefix}" ]]; then
  lock_installation_prefix "${trusted_owner_id}"
elif [[ -e "${installation_prefix}" || -L "${installation_prefix}" ]]; then
  fail "安装前缀必须是普通目录或不存在"
fi

work_directory=$(mktemp -d \
  "${TRUSTED_TEMPORARY_ROOT}/video-auto-editor-install.XXXXXX")
chmod 0755 "${work_directory}"
apt_sources="${work_directory}/ubuntu.sources"
cat >"${apt_sources}" <<EOF
Types: deb
URIs: http://archive.ubuntu.com/ubuntu
Suites: noble noble-updates
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Snapshot: ${apt_snapshot_id}

Types: deb
URIs: http://security.ubuntu.com/ubuntu
Suites: noble-security
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Snapshot: ${apt_snapshot_id}
EOF
chmod 0644 "${apt_sources}"

apt_preferences="${work_directory}/snapshot.preferences"
cat >"${apt_preferences}" <<'EOF'
Package: *
Pin: release o=Ubuntu,n=noble
Pin-Priority: 1001

Package: *
Pin: release o=Ubuntu,n=noble-updates
Pin-Priority: 1001

Package: *
Pin: release o=Ubuntu,n=noble-security
Pin-Priority: 1001
EOF
chmod 0644 "${apt_preferences}"

apt_parts="${work_directory}/apt-parts"
apt_lists="${work_directory}/apt-lists"
apt_archives="${work_directory}/apt-archives"
install -d -m 0755 -- \
  "${apt_parts}" \
  "${apt_lists}/partial" \
  "${apt_archives}/partial"

readonly -a apt_options=(
  -o "Dir::Etc::main=/dev/null"
  -o "Dir::Etc::parts=${apt_parts}"
  -o "Dir::Etc::sourcelist=${apt_sources}"
  -o "Dir::Etc::sourceparts=${apt_parts}"
  -o "Dir::Etc::preferences=${apt_preferences}"
  -o "Dir::Etc::preferencesparts=${apt_parts}"
  -o "Dir::State::lists=${apt_lists}"
  -o "Dir::Cache::pkgcache=${work_directory}/apt-pkgcache.bin"
  -o "Dir::Cache::srcpkgcache=${work_directory}/apt-srcpkgcache.bin"
  -o "Dir::Cache::archives=${apt_archives}"
  -o "APT::Get::List-Cleanup=0"
  -o "APT::Get::Always-Include-Phased-Updates=true"
  -o "DPkg::Lock::Timeout=300"
  -o "Acquire::Languages=none"
)
DEBIAN_FRONTEND=noninteractive apt-get \
  "${apt_options[@]}" --snapshot "${apt_snapshot_id}" \
  --error-on=any update

snapshot_versions_file="${work_directory}/snapshot-versions.tsv"
: >"${snapshot_versions_file}"
snapshot_package_specs=()
for package in "${SYSTEM_PACKAGES[@]}"; do
  candidate=$(
    LC_ALL=C apt-cache "${apt_options[@]}" policy "${package}" \
      | awk '$1 == "Candidate:" && !found { print $2; found = 1 }'
  )
  [[ -n "${candidate}" && "${candidate}" != "(none)" ]] \
    || fail "snapshot 没有提供系统包候选：${package}"
  [[ "${candidate}" =~ ^[A-Za-z0-9][A-Za-z0-9.+:~_-]*$ ]] \
    || fail "snapshot 候选版本格式不合法：${package}"
  if ! LC_ALL=C apt-cache "${apt_options[@]}" madison "${package}" \
    | awk -F '|' -v expected="${candidate}" '
        {
          version = $2
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", version)
          if (version == expected) found = 1
        }
        END { exit(found ? 0 : 1) }
      '
  then
    fail "snapshot 索引未提供候选版本：${package}=${candidate}"
  fi
  printf '%s\t%s\n' "${package}" "${candidate}" \
    >>"${snapshot_versions_file}"
  snapshot_package_specs+=("${package}=${candidate}")
done

DEBIAN_FRONTEND=noninteractive apt-get \
  "${apt_options[@]}" --snapshot "${apt_snapshot_id}" \
  --assume-yes --no-install-recommends --allow-downgrades --no-remove \
  install "${snapshot_package_specs[@]}"

validate_snapshot_package_versions() {
  local package expected actual
  while IFS=$'\t' read -r package expected; do
    actual=$(LC_ALL=C dpkg-query -W -f='${Version}' "${package}") \
      || fail "无法读取已安装系统包版本：${package}"
    [[ "${actual}" == "${expected}" ]] \
      || fail "已安装系统包不等于 snapshot 候选版本：${package}，期望 ${expected}，实际 ${actual}"
  done <"${snapshot_versions_file}"
}
validate_snapshot_package_versions

python3.12 -I - <<'PY'
import platform
import sys

if platform.python_implementation() != "CPython":
    raise SystemExit("生产安装要求 CPython")
if sys.platform != "linux" or platform.machine().casefold() not in {
    "amd64",
    "x86_64",
}:
    raise SystemExit("生产安装要求 Linux amd64")
if not ((3, 12, 3) <= sys.version_info[:3] < (3, 13, 0)):
    raise SystemExit("生产安装要求 CPython >=3.12.3,<3.13")
PY

if (( prefix_locked == 1 )); then
  recover_interrupted_transaction
fi

input_directory="${work_directory}/locked-inputs"
locked_wheel_directory="${input_directory}/application"
locked_runtime_directory="${input_directory}/runtime"
locked_wheelhouse_directory="${input_directory}/wheelhouse"
install -d -m 0700 -- \
  "${locked_wheel_directory}" \
  "${locked_runtime_directory}" \
  "${locked_wheelhouse_directory}"
cp --no-dereference --reflink=never -- \
  "${wheel_path}" "${locked_wheel_directory}/${wheel_filename}"
cp --no-dereference --reflink=never -- \
  "${runtime_lock_path}" "${locked_runtime_directory}/${runtime_lock_filename}"
while IFS= read -r -d '' wheelhouse_artifact; do
  artifact_filename=$(basename -- "${wheelhouse_artifact}")
  [[ "${artifact_filename}" =~ ^[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl$ ]] \
    || fail "wheelhouse wheel 文件名不合法"
  cp --no-dereference --reflink=never -- \
    "${wheelhouse_artifact}" \
    "${locked_wheelhouse_directory}/${artifact_filename}"
done < <(
  find "${wheelhouse_path}" -mindepth 1 -maxdepth 1 -print0
)

wheel_path="${locked_wheel_directory}/${wheel_filename}"
runtime_lock_path="${locked_runtime_directory}/${runtime_lock_filename}"
wheelhouse_path="${locked_wheelhouse_directory}"
[[ -f "${wheel_path}" && ! -L "${wheel_path}" ]] \
  || fail "无法创建可信 wheel 私有副本"
[[ -f "${runtime_lock_path}" && ! -L "${runtime_lock_path}" ]] \
  || fail "无法创建可信运行锁私有副本"
while IFS= read -r -d '' wheelhouse_artifact; do
  [[ -f "${wheelhouse_artifact}" && ! -L "${wheelhouse_artifact}" ]] \
    || fail "无法创建可信 wheelhouse 私有副本"
  chmod 0400 "${wheelhouse_artifact}"
done < <(
  find "${wheelhouse_path}" -mindepth 1 -maxdepth 1 -print0
)
chmod 0400 "${wheel_path}" "${runtime_lock_path}"
chmod 0700 \
  "${locked_wheel_directory}" \
  "${locked_runtime_directory}" \
  "${locked_wheelhouse_directory}"

actual_wheel_sha256=$(sha256sum -- "${wheel_path}" | awk '{print $1}')
[[ "${actual_wheel_sha256}" == "${wheel_sha256}" ]] || fail "wheel SHA-256 不匹配"
actual_runtime_lock_sha256=$(sha256sum -- "${runtime_lock_path}" | awk '{print $1}')
[[ "${actual_runtime_lock_sha256}" == "${runtime_lock_sha256}" ]] || fail "运行锁 SHA-256 不匹配"

wheel_metadata_file="${work_directory}/wheel-metadata"
python3.12 -I - "${wheel_path}" >"${wheel_metadata_file}" <<'PY'
import email.parser
import pathlib
import sys
import zipfile

wheel = pathlib.Path(sys.argv[1])
try:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("wheel 必须有且只有一份 METADATA")
        message = email.parser.BytesParser().parsebytes(
            archive.read(metadata_names[0])
        )
except (OSError, ValueError, zipfile.BadZipFile, KeyError) as error:
    raise SystemExit(f"无法读取候选 wheel 元数据：{error}") from error

if message.get("Name") != "video-auto-editor":
    raise SystemExit("候选 wheel 项目名称不合法")
version = message.get("Version", "")
if not version or any(character not in "0123456789." for character in version):
    raise SystemExit("候选 wheel 版本不合法")
python_specifiers = {
    specifier.strip()
    for specifier in message.get("Requires-Python", "").split(",")
    if specifier.strip()
}
if python_specifiers != {">=3.12.3", "<3.13"}:
    raise SystemExit("候选 wheel 未锁定认证 CPython 范围")
if message.get_all("Requires-Dist", []):
    raise SystemExit("应用 wheel 不得声明未锁定运行依赖")
print(version)
PY
application_version=$(<"${wheel_metadata_file}")
[[ "${application_version}" =~ ^[0-9]+(\.[0-9]+)*$ ]] \
  || fail "候选 wheel 版本不能形成安全的版本目录"

python3.12 -I - "${runtime_lock_path}" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
logical_lines: list[str] = []
pending = ""
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if line.endswith("\\"):
        pending += line[:-1].strip() + " "
        continue
    logical_lines.append((pending + line).strip())
    pending = ""
if pending:
    raise SystemExit("运行锁存在未完成的续行")

requirement = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==[^\s;]+(?:\s*;\s*[^\s]+(?:\s+[^\s]+)*)?"
    r"(?:\s+--hash=sha256:[0-9a-f]{64})+"
)
for line in logical_lines:
    if requirement.fullmatch(line) is None:
        raise SystemExit("运行锁只允许精确固定且带 SHA-256 的依赖")
PY

if (( prefix_locked == 0 )); then
  lock_installation_prefix "${trusted_owner_id}"
  recover_interrupted_transaction
fi

final_directory="${installation_prefix}/versions/${application_version}"
manifest_path="${final_directory}/installation-manifest.json"
ready_path="${final_directory}/READY"
if [[ -e "${final_directory}" || -L "${final_directory}" ]]; then
  validate_managed_directory "${final_directory}" "既有版本路径" \
    "${trusted_owner_id}"
  if [[ ! -f "${manifest_path}" || -L "${manifest_path}" || ! -f "${ready_path}" || -L "${ready_path}" ]]; then
    [[ ! -e "${ready_path}" && ! -L "${ready_path}" ]] \
      || fail "既有版本的可运行身份已损坏"
    if [[ -L "${current_link}" ]]; then
      partial_current_target=$(readlink -- "${current_link}")
      if [[ "${partial_current_target}" == /* ]]; then
        partial_current_path=$(realpath -m -- "${partial_current_target}")
      else
        partial_current_path=$(realpath -m -- "${installation_prefix}/${partial_current_target}")
      fi
      [[ "${partial_current_path}" != "${final_directory}" ]] \
        || fail "current 正在引用未就绪版本，拒绝自动删除"
    elif [[ -e "${current_link}" ]]; then
      fail "current 必须是符号链接或不存在"
    fi
    rm -rf -- "${final_directory}"
  fi
fi
if [[ -e "${final_directory}" || -L "${final_directory}" ]]; then
  [[ -f "${manifest_path}" && ! -L "${manifest_path}" ]] || fail "既有版本缺少可信安装清单"
  [[ -f "${ready_path}" && ! -L "${ready_path}" ]] || fail "既有版本没有可运行标记"
  python3.12 -I - \
    "${manifest_path}" \
    "${ready_path}" \
    "${wheelhouse_path}" \
    "${installation_prefix}" \
    "${apt_snapshot_id}" \
    "${wheel_path}" \
    "${wheel_sha256}" \
    "${application_version}" \
    "${runtime_lock_path}" \
    "${runtime_lock_sha256}" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

(
    manifest_name,
    ready_name,
    wheelhouse_name,
    prefix,
    snapshot,
    wheel_name,
    wheel_digest,
    application_version,
    lock_name,
    lock_digest,
) = sys.argv[1:]
manifest_path = Path(manifest_name)
ready_path = Path(ready_name)
try:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    ready = json.loads(ready_path.read_bytes())
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"既有安装身份不可读：{error}") from error

actual_manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
if ready != {
    "installation_manifest_sha256": actual_manifest_digest,
    "schema_version": "production-installation-ready.v1",
}:
    raise SystemExit("既有可运行标记与安装清单不一致")

expected_identity = {
    "application_name": "video-auto-editor",
    "application_version": application_version,
    "wheel_filename": Path(wheel_name).name,
    "wheel_sha256": wheel_digest,
    "lock_filename": Path(lock_name).name,
    "lock_sha256": lock_digest,
    "prefix": prefix,
    "snapshot": snapshot,
}
try:
    actual_identity = {
        "application_name": manifest["application"]["name"],
        "application_version": manifest["application"]["version"],
        "wheel_filename": manifest["application"]["wheel"]["filename"],
        "wheel_sha256": manifest["application"]["wheel"]["sha256"],
        "lock_filename": manifest["runtime_lock"]["filename"],
        "lock_sha256": manifest["runtime_lock"]["sha256"],
        "prefix": manifest["installation_prefix"],
        "snapshot": manifest["apt_snapshot_id"],
    }
except (KeyError, TypeError) as error:
    raise SystemExit("既有安装清单缺少候选身份") from error
if manifest.get("schema_version") != "production-installation-manifest.v1":
    raise SystemExit("既有安装清单 schema 不受支持")
if actual_identity != expected_identity:
    raise SystemExit("同版本的既有安装与本次锁定输入不一致")

wheelhouse = []
for artifact in sorted(Path(wheelhouse_name).iterdir(), key=lambda item: item.name):
    if not artifact.is_file() or artifact.is_symlink():
        continue
    wheelhouse.append(
        {
            "filename": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    )
if manifest.get("wheelhouse") != wheelhouse:
    raise SystemExit("既有安装与本地 wheelhouse 摘要不一致")
PY
  reused_version=1
else
  install -d -m 0755 -- "${final_directory}"
  validate_managed_directory "${final_directory}" "新版本路径" \
    "${trusted_owner_id}"
  created_version=1
  python3.12 -I -m venv "${final_directory}/venv"

  readonly -a pip_common=(
    --isolated
    --disable-pip-version-check
    --no-input
    install
    --no-index
    --no-deps
  )
  PIP_CONFIG_FILE=/dev/null "${final_directory}/venv/bin/python" -I -m pip \
    "${pip_common[@]}" \
    --find-links "${wheelhouse_path}" \
    --require-hashes \
    --only-binary=:all: \
    -r "${runtime_lock_path}"
  PIP_CONFIG_FILE=/dev/null "${final_directory}/venv/bin/python" -I -m pip \
    "${pip_common[@]}" "${wheel_path}"
fi

if [[ -L "${current_link}" ]]; then
  previous_current_target=$(readlink -- "${current_link}")
elif [[ -e "${current_link}" ]]; then
  fail "current 必须是符号链接或不存在"
fi
prepare_install_transaction
replacement_link="${installation_prefix}/.current.$$"
rm -f -- "${replacement_link}"
ln -s -- "versions/${application_version}" "${replacement_link}"
# 固定安装契约先切换 current 再预检；只有 READY 才表示版本可运行。
switched_current=1
mv -Tf -- "${replacement_link}" "${current_link}"
replacement_link=""
fsync_directory "${installation_prefix}"

readiness_issues=()
readiness_issue() {
  readiness_issues+=("$1")
}

if ! pip_check_output=$(
  PIP_CONFIG_FILE=/dev/null "${final_directory}/venv/bin/python" -I -m pip \
    --isolated --disable-pip-version-check --no-input check 2>&1
); then
  printf '%s\n' "${pip_check_output}" >&2
  readiness_issue "Python 运行依赖锁没有形成完整且一致的闭包"
fi

if ! installed_version=$("${final_directory}/venv/bin/python" -I - 2>/dev/null <<'PY'
from importlib.metadata import version

print(version("video-auto-editor"))
PY
); then
  readiness_issue "无法读取已安装应用版本"
elif [[ "${installed_version}" != "${application_version}" ]]; then
  readiness_issue "已安装应用版本与 wheel 不一致"
fi

if ! "${final_directory}/venv/bin/python" -I - "${final_directory}/venv" >/dev/null 2>&1 <<'PY'
from importlib.metadata import distribution
from pathlib import Path
import platform
import sys

expected = Path(sys.argv[1]).resolve()
if platform.python_implementation() != "CPython":
    raise SystemExit("版本环境不是 CPython")
if sys.platform != "linux" or platform.machine().casefold() not in {
    "amd64",
    "x86_64",
}:
    raise SystemExit("版本环境不是 Linux amd64")
if not ((3, 12, 3) <= sys.version_info[:3] < (3, 13, 0)):
    raise SystemExit("版本环境的 CPython 版本不受认证")
if Path(sys.prefix).resolve() != expected:
    raise SystemExit("解释器与版本化虚拟环境前缀不一致")
distribution_root = Path(distribution("video-auto-editor").locate_file("")).resolve()
if expected not in distribution_root.parents:
    raise SystemExit("应用 distribution 不在版本化虚拟环境内")
PY
then
  readiness_issue "解释器、应用 distribution 与版本化安装前缀不一致"
fi

if ! cli_version_output=$(
  "${final_directory}/venv/bin/video-auto-editor" --version 2>/dev/null
) || [[ "${cli_version_output}" != "video-auto-editor ${application_version}" ]]; then
  readiness_issue "已安装控制台入口版本不一致"
fi

media_smoke_ready=1
ffmpeg_version=""
ffprobe_version=""
if ffmpeg_version_output=$(ffmpeg -version 2>/dev/null); then
  ffmpeg_version_line=${ffmpeg_version_output%%$'\n'*}
else
  ffmpeg_version_line=""
fi
if [[ "${ffmpeg_version_line}" =~ ^ffmpeg\ version\ ([^[:space:]]+) ]]; then
  ffmpeg_version=${BASH_REMATCH[1]}
else
  readiness_issue "无法执行或解析 FFmpeg 版本"
  media_smoke_ready=0
fi
if ffprobe_version_output=$(ffprobe -version 2>/dev/null); then
  ffprobe_version_line=${ffprobe_version_output%%$'\n'*}
else
  ffprobe_version_line=""
fi
if [[ "${ffprobe_version_line}" =~ ^ffprobe\ version\ ([^[:space:]]+) ]]; then
  ffprobe_version=${BASH_REMATCH[1]}
else
  readiness_issue "无法执行或解析 ffprobe 版本"
  media_smoke_ready=0
fi
if [[ -n "${ffmpeg_version}" && -n "${ffprobe_version}" \
  && "${ffmpeg_version}" != "${ffprobe_version}" ]]; then
  readiness_issue "FFmpeg 与 ffprobe 上游版本不一致"
  media_smoke_ready=0
fi
if [[ -n "${ffmpeg_version}" ]] && ! python3.12 -I - "${ffmpeg_version}" >/dev/null 2>&1 <<'PY'
import re
import sys

matched = re.match(r"(?:[0-9]+:)?([0-9]+)\.([0-9]+)", sys.argv[1])
if matched is None or not ((6, 1) <= tuple(map(int, matched.groups())) < (7, 0)):
    raise SystemExit("FFmpeg 版本不在 >=6.1,<7 范围")
PY
then
  readiness_issue "FFmpeg 版本不在 >=6.1,<7 范围"
  media_smoke_ready=0
fi
if ! filters_listing=$(ffmpeg -hide_banner -filters 2>/dev/null) \
  || ! grep -Eq '(^|[[:space:]])subtitles([[:space:]]|$)' \
    <<<"${filters_listing}"; then
  readiness_issue "FFmpeg 缺少 subtitles 滤镜"
  media_smoke_ready=0
fi
if encoder_listing=$(ffmpeg -hide_banner -encoders 2>/dev/null); then
  if ! grep -Eq '(^|[[:space:]])libx264([[:space:]]|$)' <<<"${encoder_listing}"; then
    readiness_issue "FFmpeg 缺少 libx264 编码器"
    media_smoke_ready=0
  fi
  if ! grep -Eq '(^|[[:space:]])aac([[:space:]]|$)' <<<"${encoder_listing}"; then
    readiness_issue "FFmpeg 缺少 AAC 编码器"
    media_smoke_ready=0
  fi
else
  readiness_issue "FFmpeg 缺少 libx264 编码器"
  readiness_issue "FFmpeg 缺少 AAC 编码器"
  media_smoke_ready=0
fi

font_ready=1
font_family=""
font_file=""
if ! fc-list -q "${FONT_FAMILY}" 2>/dev/null; then
  readiness_issue "没有找到认证中文字体"
  font_ready=0
fi
if font_match=$(fc-match --format '%{family}\n%{file}\n' "${FONT_FAMILY}" 2>/dev/null); then
  font_family=$(sed -n '1p' <<<"${font_match}")
  font_file=$(sed -n '2p' <<<"${font_match}")
  if [[ "${font_family}" != *"${FONT_FAMILY}"* ]]; then
    readiness_issue "fontconfig 没有命中认证中文字体家族"
    font_ready=0
  fi
  if [[ ! -f "${font_file}" || ! -r "${font_file}" ]]; then
    readiness_issue "认证中文字体文件不可读"
    font_ready=0
  fi
else
  readiness_issue "无法执行 fontconfig 字体匹配"
  font_ready=0
fi

if (( media_smoke_ready == 1 && font_ready == 1 )); then
  if smoke_directory=$(mktemp -d "${installation_prefix}/.readiness.XXXXXX"); then
    cat >"${smoke_directory}/readiness.srt" <<'EOF'
1
00:00:00,000 --> 00:00:00,900
中文预检
EOF
    if ! (
      cd "${smoke_directory}" \
        && ffmpeg -hide_banner -loglevel error -y \
          -f lavfi -i color=c=black:s=320x240:d=1 \
          -f lavfi -i anullsrc=r=48000:cl=stereo \
          -t 1 -c:v libx264 -pix_fmt yuv420p -c:a aac source.mp4 \
        && ffmpeg -hide_banner -loglevel error -y \
          -i source.mp4 \
          -vf "subtitles=readiness.srt:force_style='FontName=${FONT_FAMILY}'" \
          -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart burned.mp4 \
        && ffprobe -v error -show_entries format=format_name,duration \
          -show_entries stream=codec_type -of json burned.mp4 >probe.json \
        && "${final_directory}/venv/bin/python" -I - <<'PY'
import json
from pathlib import Path

document = json.loads(Path("probe.json").read_text(encoding="utf-8"))
format_fact = document.get("format", {})
if "mp4" not in str(format_fact.get("format_name", "")).split(","):
    raise SystemExit("ffprobe 没有确认 MP4 容器")
try:
    duration = float(format_fact.get("duration", 0))
except (TypeError, ValueError):
    raise SystemExit("ffprobe 没有返回有效时长") from None
stream_types = {
    stream.get("codec_type") for stream in document.get("streams", [])
    if isinstance(stream, dict)
}
if duration <= 0 or not {"video", "audio"}.issubset(stream_types):
    raise SystemExit("ffprobe 没有确认完整音视频流")
PY
    ); then
      readiness_issue "中文字幕媒体与 ffprobe JSON 烟测失败"
    fi
    rm -rf -- "${smoke_directory}"
    smoke_directory=""
  else
    readiness_issue "无法创建媒体预检临时目录"
  fi
fi

if ! "${final_directory}/venv/bin/python" -I - >/dev/null 2>&1 <<'PY'
import ssl

context = ssl.create_default_context()
if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
    raise SystemExit("默认 TLS 校验未启用")
if context.cert_store_stats().get("x509_ca", 0) <= 0:
    raise SystemExit("默认 TLS CA 信任库为空")
PY
then
  readiness_issue "默认 TLS CA 信任库不可用"
fi

if ! "${final_directory}/venv/bin/python" -I - "${installation_prefix}" >/dev/null 2>&1 <<'PY'
from pathlib import Path
import os
import shutil
import sys
import tempfile

prefix = Path(sys.argv[1])
directory = Path(tempfile.mkdtemp(prefix=".atomic-readiness.", dir=prefix))
try:
    source = directory / "source"
    target = directory / "target"
    with source.open("wb") as stream:
        stream.write(b"production-installation-readiness.v1\n")
        stream.flush()
        os.fsync(stream.fileno())
    if source.stat().st_dev != prefix.stat().st_dev:
        raise SystemExit("暂存与安装前缀不在同一文件系统")
    os.replace(source, target)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    target.unlink()
finally:
    shutil.rmtree(directory, ignore_errors=True)
PY
then
  readiness_issue "安装前缀不支持可写、fsync 与同文件系统原子重命名"
fi

if (( ${#readiness_issues[@]} > 0 )); then
  printf '严格环境预检发现 %d 个问题：\n' "${#readiness_issues[@]}" >&2
  printf -- '- %s\n' "${readiness_issues[@]}" >&2
  exit 1
fi

validate_snapshot_package_versions
final_wheel_sha256=$(sha256sum -- "${wheel_path}" | awk '{print $1}')
[[ "${final_wheel_sha256}" == "${wheel_sha256}" ]] \
  || fail "安装期间 wheel 私有副本发生变化"
final_runtime_lock_sha256=$(sha256sum -- "${runtime_lock_path}" | awk '{print $1}')
[[ "${final_runtime_lock_sha256}" == "${runtime_lock_sha256}" ]] \
  || fail "安装期间运行锁私有副本发生变化"

dpkg_versions_file="${work_directory}/dpkg-versions.tsv"
LC_ALL=C dpkg-query -W \
  -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
  >"${dpkg_versions_file}"

manifest_path="${final_directory}/installation-manifest.json"
manifest_temporary="${final_directory}/.installation-manifest.json.$$"
"${final_directory}/venv/bin/python" -I - \
  "${manifest_temporary}" \
  "${dpkg_versions_file}" \
  "${snapshot_versions_file}" \
  "${wheelhouse_path}" \
  "${installation_prefix}" \
  "${apt_snapshot_id}" \
  "${wheel_path}" \
  "${wheel_sha256}" \
  "${application_version}" \
  "${runtime_lock_path}" \
  "${runtime_lock_sha256}" \
  "${architecture}" \
  "${os_id}" \
  "${os_version}" \
  "${ffmpeg_version}" \
  "${ffprobe_version}" \
  "${font_family}" \
  "${font_file}" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import platform
import sys

(
    output_name,
    packages_name,
    snapshot_packages_name,
    wheelhouse_name,
    prefix,
    snapshot,
    wheel_name,
    wheel_digest,
    application_version,
    lock_name,
    lock_digest,
    architecture,
    os_id,
    os_version,
    ffmpeg_version,
    ffprobe_version,
    font_family,
    font_file,
) = sys.argv[1:]

packages: dict[str, str] = {}
for line in Path(packages_name).read_text(encoding="utf-8").splitlines():
    name, version, status = line.split("\t", 2)
    if not status.startswith("ii"):
        continue
    normalized_name = name.removesuffix(":" + architecture)
    if normalized_name in packages:
        raise SystemExit(f"系统包清单存在重复身份：{normalized_name}")
    packages[normalized_name] = version

snapshot_packages: dict[str, str] = {}
for line in Path(snapshot_packages_name).read_text(encoding="utf-8").splitlines():
    name, version = line.split("\t", 1)
    snapshot_packages[name] = version

wheelhouse = []
for artifact in sorted(Path(wheelhouse_name).iterdir(), key=lambda item: item.name):
    if not artifact.is_file() or artifact.is_symlink():
        continue
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    wheelhouse.append({"filename": artifact.name, "sha256": digest})

manifest = {
    "application": {
        "name": "video-auto-editor",
        "version": application_version,
        "wheel": {
            "filename": Path(wheel_name).name,
            "sha256": wheel_digest,
        },
    },
    "apt_snapshot_id": snapshot,
    "environment": {
        "ffmpeg_version": ffmpeg_version,
        "ffprobe_version": ffprobe_version,
        "font_family": font_family,
        "font_file": font_file,
    },
    "installation_prefix": prefix,
    "platform": {
        "architecture": architecture,
        "operating_system": os_id,
        "operating_system_version": os_version,
    },
    "python": {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    },
    "runtime_lock": {
        "filename": Path(lock_name).name,
        "sha256": lock_digest,
    },
    "schema_version": "production-installation-manifest.v1",
    "snapshot_packages": snapshot_packages,
    "system_packages": packages,
    "wheelhouse": wheelhouse,
}
payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
output = Path(output_name)
with output.open("wb") as stream:
    stream.write(payload)
    stream.flush()
    os.fsync(stream.fileno())
PY
if (( reused_version == 1 )); then
  cmp -s -- "${manifest_temporary}" "${manifest_path}" \
    || fail "既有安装清单与重新预检的实际环境不一致"
  rm -f -- "${manifest_temporary}"
  manifest_sha256=$(sha256sum -- "${manifest_path}" | awk '{print $1}')
else
  mv -- "${manifest_temporary}" "${manifest_path}"

  manifest_sha256=$(sha256sum -- "${manifest_path}" | awk '{print $1}')
  ready_temporary="${final_directory}/.READY.$$"
  "${final_directory}/venv/bin/python" -I - "${ready_temporary}" "${manifest_sha256}" <<'PY'
import json
import os
from pathlib import Path
import sys

payload = {
    "installation_manifest_sha256": sys.argv[2],
    "schema_version": "production-installation-ready.v1",
}
output = Path(sys.argv[1])
with output.open("w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
PY
  mv -- "${ready_temporary}" "${final_directory}/READY"
  fsync_directory "${final_directory}"
fi

commit_install_transaction
switched_current=0
printf '生产版本 %s 已安装并通过严格环境预检。\n' "${application_version}"
printf '安装清单：%s\n' "${manifest_path}"
printf '安装清单 SHA-256：%s\n' "${manifest_sha256}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  # 参数必须由清洁子 shell 展开。
  # shellcheck disable=SC2016
  exec /usr/bin/env -i \
    PATH="${TRUSTED_COMMAND_PATH}" \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    LANGUAGE=C.UTF-8 \
    /bin/bash -p -c \
    'source "$1"
shift
install_production_main /etc/os-release "${EUID}" "${EUID}" "$@"' \
    production-installer-clean-environment \
    "${BASH_SOURCE[0]}" \
    "$@"
fi
