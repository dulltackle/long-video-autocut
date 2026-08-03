# 生产发布真实门禁操作手册

状态：操作基线

适用平台：Ubuntu 24.04 LTS `amd64`

固定 APT snapshot：`20260725T000000Z`

## 1. 目的与议题边界

本手册把[生产就绪规格第 14 节](production-readiness-spec.md#14-生产验收矩阵)、
[分层验收证据 ADR](adr/0025-approve-production-releases-with-layered-acceptance-evidence.md)
和[敏感数据 ADR](adr/0024-unify-sensitive-data-provider-disclosure-and-local-retention-contract.md)
落实为可复核的操作步骤。

必须区分两个议题：

- [Issue #48](https://github.com/dulltackle/long-video-autocut/issues/48) 只准备并验证
  门禁工具、凭据桥接、严格证据契约和本手册。完成 #48 **不代表**已经
  联系真实供应商、执行真实素材门禁、人工观看、封存证据、签名 tag 或
  创建 GitHub Release。
- [Issue #49](https://github.com/dulltackle/long-video-autocut/issues/49) 才由发布操作员在
  认证主机上使用真实中文素材和受控凭据，依次完成冷运行、逐条人工复核、同工作区
  零请求覆盖复跑、证据封存，并由维护者创建签名发布。

因此，本手册第 3—11 节是 #49 将来执行的操作契约，不是 #48 已执行过这些
步骤的声明。#48 不产生真实运行记录或已封存的生产证据。

#49 只允许使用**同一次成功 CI artifact**的 `release-bundle.tar` 中的
以下六个 release tools：

- `install-production.sh`；
- `run_keyless_gate_network.sh`；
- `run_release_gate.py`；
- `systemd_credential_bridge.py`；
- `validate_installed_delivery.py`；
- `validate_release_evidence.py`。

CI 失败时另行上传的 `keyless-gate-failure-<run-id>-<run-attempt>` 只用于诊断，
其中索引明确标记 `release_eligible: false` 和
`ci_gate_failure_non_retryable`。不得从该诊断 artifact 取 wheel、证据或工具进入
#49；应修复失败并重新取得成功的 `release-bundle.tar`。

禁止直接执行下载 artifact 解包得到的普通文件，也禁止从任意 checkout、本地分支或
另一次 artifact 执行同名文件。必须先核对 GitHub 记录的 artifact digest 和包内
`BUNDLE-SHA256SUMS`，然后由 root 解包到只读的受保护根 `$RELEASE_TOOLS_ROOT`。
`keyless-gate-evidence.json` 记录六个工具的 SHA-256；`plan.json` 锁定同一组
摘要与实际路径；封存器再从计划和原始记录重算，并在公开
`release-evidence.json` 的 `artifacts.release_tools` 中固化六个摘要。

任何硬门禁都不可备注放行、手工豁免或通过编辑证据改成成功。代码、commit、wheel、
锁文件、snapshot、输入或执行程序任一变化，都必须建立新候选并重新开始。

## 2. 不可变候选与职责

同一候选至少由以下事实共同锁定：

- 40 或 64 位小写十六进制 commit SHA；
- 已构建 wheel 的文件名、版本和 SHA-256；
- `requirements-build.lock` 与 `requirements-runtime.lock` 的文件名和 SHA-256；
- CI 自动安装验收的 `installation-manifest.json`、`READY` 与 snapshot
  `20260725T000000Z`；
- 认证主机使用同一 wheel、同一运行锁、同一 snapshot 重新执行 artifact 内
  `install-production.sh` 后生成的另一份 `installation-manifest.json` 和 `READY`；
- 同一次自动门禁的 URL、`keyless-gate-evidence.json` 和
  `installed-acceptance-evidence.json`；
- 认证主机证明、真实素材摘要、固定配置、课程上下文、预期转写和执行程序摘要。

自动门禁必须先通过。无密钥门禁的五层均须非零收集、全部通过，且失败、错误、
筛除、跳过、xfail、xpass、重跑和退出码都为零；安装验收的全部用例也必须通过。
CI 不得接收真实 StepFun 凭据。

两份安装清单不得混用：`request.json` 的
`candidate.installation_manifest` 和 `candidate.installation_ready` 指向**认证主机**
重新安装生成的文件，供真实运行的环境指纹使用；第 9 节封存命令的
`--installation-manifest` 则指向**CI 自动安装验收**上传的安装清单，
用来复核 `installed-acceptance-evidence.json`。最终公开证据同时固化 CI 安装清单
与认证主机安装清单的摘要，并把两套完整环境和依赖清单分别放在
`installation.ci_installed_acceptance` / `installation.certified_host` 与
`dependencies.ci_installed_acceptance` / `dependencies.certified_host`，不得把 CI 依赖
冒充认证主机实际依赖。

发布操作员负责认证主机、私有输入、真实门禁、人工复核和源证据；独立校验器以不含
凭据的环境复核交付；维护者只在 #49 的全部证据通过后签名 tag 并创建 Release。

## 3. 私有工作区与数据边界

先关闭 shell xtrace，并使用私有默认权限：

```bash
set -euo pipefail
set +x
umask 077
readonly TRUSTED_PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export PATH="$TRUSTED_PATH"
export LC_ALL="C.UTF-8"

DOWNLOADED_ARTIFACT_ROOT="/绝对路径/已下载的同一次-CI-artifact"
RELEASE_BUNDLE="$DOWNLOADED_ARTIFACT_ROOT/release-bundle.tar"
RELEASE_TOOLS_ROOT="/opt/video-auto-editor-release-tools/<CI-run-id>-<run-attempt>"
CANDIDATE_ROOT="$RELEASE_TOOLS_ROOT/candidate"
CI_EVIDENCE_ROOT="$RELEASE_TOOLS_ROOT/evidence"
CANDIDATE_SLUG="<只含小写字母数字和连字符的候选标识>"
[[ "$CANDIDATE_SLUG" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]]
PRIVATE_ROOT="/绝对路径/私有发布目录/$CANDIDATE_SLUG"
STAGING_ROOT="$PRIVATE_ROOT/staging"
WORKSPACE_PARENT="$PRIVATE_ROOT/workspaces"
REQUEST_PATH="$PRIVATE_ROOT/request.json"
DRAFT_PLAN_PATH="$PRIVATE_ROOT/plan.json"
LOCKED_PLAN_ROOT="/var/lib/video-auto-editor-release-plans"
LOCKED_PLAN_DIRECTORY="$LOCKED_PLAN_ROOT/$CANDIDATE_SLUG"
PLAN_PATH="$LOCKED_PLAN_DIRECTORY/plan.json"
SOURCE_EVIDENCE_PATH="$PRIVATE_ROOT/release-evidence-source.json"
PUBLIC_ROOT="/绝对路径/公开发布目录/$CANDIDATE_SLUG"
FINAL_EVIDENCE_PATH="$PUBLIC_ROOT/release-evidence.json"
SNAPSHOT_ID="20260725T000000Z"
RELEASE_PYTHON="/usr/bin/python3.12"
RELEASE_OPERATOR_NAME="$(id -un)"
RELEASE_OPERATOR_GROUP="$(id -gn)"
RELEASE_OPERATOR_UID="$(id -u)"
RELEASE_OPERATOR_GID="$(id -g)"
test "$RELEASE_OPERATOR_UID" -gt 0
test "$RELEASE_OPERATOR_GID" -gt 0
test "$(readlink -f -- "$RELEASE_PYTHON")" = "$RELEASE_PYTHON"
test "$(stat -c '%F:%u' -- "$RELEASE_PYTHON")" = "regular file:0"
mapfile -t PRIMARY_GROUP_USERS < <(
  getent passwd | awk -F: -v gid="$RELEASE_OPERATOR_GID" '$4 == gid { print $1 }'
)
test "${#PRIMARY_GROUP_USERS[@]}" = 1
test "${PRIMARY_GROUP_USERS[0]}" = "$RELEASE_OPERATOR_NAME"
RELEASE_GROUP_MEMBERS="$(getent group "$RELEASE_OPERATOR_GID" | cut -d: -f4)"
test -z "$RELEASE_GROUP_MEMBERS" || \
  test "$RELEASE_GROUP_MEMBERS" = "$RELEASE_OPERATOR_NAME"

install -d -m 0700 "$PRIVATE_ROOT" "$STAGING_ROOT" "$WORKSPACE_PARENT"
install -d -m 0700 "$PUBLIC_ROOT"
test "$(stat -c '%a' "$PRIVATE_ROOT")" = 700
test "$(stat -c '%a' "$WORKSPACE_PARENT")" = 700
test -z "${STEPFUN_API_KEY+x}"
```

`DOWNLOADED_ARTIFACT_ROOT` 只用来存放未执行的下载物；该目录下的文件不是可执行
根。`RELEASE_TOOLS_ROOT` 必须是第 4 节在摘要验证后由 root 创建和解包的唯一可信根；
不得为了方便把它或任一子目录指向 checkout、未受保护的下载目录或其他 artifact。

发布操作员账号及其主组必须专用于本次发布，主组不能被第二个账号共享；运行真实门禁
期间该 UID 下不得有无关进程。所有含素材、上下文、转写、交付、诊断或尝试记录的
私有目录必须为 `0700`，文件必须为 `0600`。门禁先以 `0600` 独占创建草案计划，
管理员再把完全相同的字节锁定为 `$LOCKED_PLAN_ROOT/$CANDIDATE_SLUG/plan.json`：文件
必须是 `root:<专用发布组>`/`0440`，直接父目录必须是
`root:<专用发布组>`/`0710`，受信根及其祖先由 root 控制且不可被操作员重命名。
凭据运行和最终封存只能使用锁定副本。尝试记录和分类记录由门禁以 `0600` 独占创建；
不得先建同名文件。最终脱敏证据由封存器独占创建为 `0444`。

以下内容永远不得上传到 GitHub、工单、聊天、日志附件或 Release：

- 明文或加密的供应商凭据、systemd credential 文件及其明文副本；
- 真实 MP4、配置正文、课程上下文、预期转写；
- `request.json`、`plan.json`、`release-evidence-source.json`；
- 整个 `workspaces/`，包括处理缓存、运行诊断、冷/热交付物、独立校验原始结果、
  人工复核原始记录、失败尝试与本地路径。

可公开的是严格封存后的 `release-evidence.json` 及已由自动门禁设计为公开的脱敏证据。
上传前仍须按第 10 节执行允许清单核对。

## 4. 认证主机与候选前置核验

只在已登记的 Ubuntu 24.04 LTS `amd64` 认证主机上执行 #49。

### 4.1 验证并安装 release bundle

CI 以 `archive: false` 上传单一 `release-bundle.tar`，以便 tar 保留 release tools 的
`0444`/`0555` mode。下载后不得直接执行下载目录中的任何文件。先从 GitHub
Actions artifact 元数据中独立取得 `sha256:<64 位小写十六进制>` digest；不得把
下载物内自述的值当作 GitHub artifact digest。再校验 tar 内的 `BUNDLE-SHA256SUMS`：

```bash
test -f "$RELEASE_BUNDLE"
GITHUB_ARTIFACT_DIGEST="sha256:<从-GitHub-Actions-artifact-元数据复核的值>"
[[ "$GITHUB_ARTIFACT_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
test "$(sha256sum -- "$RELEASE_BUNDLE" | awk '{print $1}')" = \
  "${GITHUB_ARTIFACT_DIGEST#sha256:}"

mapfile -t BUNDLE_MEMBERS < <(
  tar --list --file "$RELEASE_BUNDLE" | LC_ALL=C sort
)
mapfile -t BUNDLE_WHEELS < <(
  printf '%s\n' "${BUNDLE_MEMBERS[@]}" \
    | sed -n '/^candidate\/[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl$/p'
)
test "${#BUNDLE_WHEELS[@]}" = 1
EXPECTED_BUNDLE_MEMBERS=(
  BUNDLE-SHA256SUMS
  "${BUNDLE_WHEELS[0]}"
  candidate/commit-sha
  candidate/requirements-build.lock
  candidate/requirements-runtime.lock
  evidence/READY
  evidence/installation-manifest.json
  evidence/installed-acceptance-evidence.json
  evidence/keyless-gate-evidence.json
  scripts/install-production.sh
  scripts/run_keyless_gate_network.sh
  scripts/run_release_gate.py
  scripts/systemd_credential_bridge.py
  scripts/validate_installed_delivery.py
  scripts/validate_release_evidence.py
)
mapfile -t EXPECTED_BUNDLE_MEMBERS < <(
  printf '%s\n' "${EXPECTED_BUNDLE_MEMBERS[@]}" | LC_ALL=C sort
)
test "$(printf '%s\n' "${BUNDLE_MEMBERS[@]}")" = \
  "$(printf '%s\n' "${EXPECTED_BUNDLE_MEMBERS[@]}")"

BUNDLE_VERIFY_ROOT="$(mktemp -d /tmp/video-auto-editor-bundle-verify.XXXXXX)"
trap 'rm -rf -- "$BUNDLE_VERIFY_ROOT"' EXIT
tar --extract --file "$RELEASE_BUNDLE" --directory "$BUNDLE_VERIFY_ROOT"
test -f "$BUNDLE_VERIFY_ROOT/BUNDLE-SHA256SUMS"
mapfile -t CHECKSUM_MEMBERS < <(
  awk 'NF == 2 { print $2 }' "$BUNDLE_VERIFY_ROOT/BUNDLE-SHA256SUMS" \
    | LC_ALL=C sort
)
mapfile -t EXPECTED_CHECKSUM_MEMBERS < <(
  printf '%s\n' "${EXPECTED_BUNDLE_MEMBERS[@]}" \
    | sed '/^BUNDLE-SHA256SUMS$/d' | LC_ALL=C sort
)
test "$(printf '%s\n' "${CHECKSUM_MEMBERS[@]}")" = \
  "$(printf '%s\n' "${EXPECTED_CHECKSUM_MEMBERS[@]}")"
(
  cd "$BUNDLE_VERIFY_ROOT"
  sha256sum --check --strict BUNDLE-SHA256SUMS
)
```

两层摘要都通过后，才由 root 把同一 tar 解包到不存在的专用目录，改为 root
所有并移除所有写权限。安装后再校验一次包内清单和两种精确 mode：

```bash
test ! -e "$RELEASE_TOOLS_ROOT"
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 0755 "$RELEASE_TOOLS_ROOT"
/usr/bin/sudo /usr/bin/tar --extract --file "$RELEASE_BUNDLE" \
  --directory "$RELEASE_TOOLS_ROOT" --no-same-owner --same-permissions
/usr/bin/sudo /usr/bin/chown -R root:root "$RELEASE_TOOLS_ROOT"
/usr/bin/sudo /usr/bin/chmod -R a-w "$RELEASE_TOOLS_ROOT"

(
  cd "$RELEASE_TOOLS_ROOT"
  sha256sum --check --strict BUNDLE-SHA256SUMS
)
test "$(stat -c '%a' "$RELEASE_TOOLS_ROOT/scripts/install-production.sh")" = 555
test "$(stat -c '%a' "$RELEASE_TOOLS_ROOT/scripts/run_keyless_gate_network.sh")" = 555
test "$(stat -c '%a:%u:%g' "$RELEASE_TOOLS_ROOT/BUNDLE-SHA256SUMS")" = 444:0:0
test "$(stat -c '%u:%g' "$RELEASE_TOOLS_ROOT")" = 0:0
for release_tool in \
  install-production.sh \
  run_keyless_gate_network.sh \
  run_release_gate.py \
  systemd_credential_bridge.py \
  validate_installed_delivery.py \
  validate_release_evidence.py; do
  test "$(stat -c '%u:%g' "$RELEASE_TOOLS_ROOT/scripts/$release_tool")" = 0:0
done
for release_tool in \
  run_release_gate.py \
  systemd_credential_bridge.py \
  validate_installed_delivery.py \
  validate_release_evidence.py; do
  test "$(stat -c '%a' "$RELEASE_TOOLS_ROOT/scripts/$release_tool")" = 444
done

mapfile -t PROTECTED_CANDIDATE_WHEELS < <(
  find "$RELEASE_TOOLS_ROOT/candidate" \
    -maxdepth 1 -type f -name '*.whl' -print
)
test "${#PROTECTED_CANDIDATE_WHEELS[@]}" = 1
for protected_file in \
  "${PROTECTED_CANDIDATE_WHEELS[0]}" \
  "$RELEASE_TOOLS_ROOT/candidate/commit-sha" \
  "$RELEASE_TOOLS_ROOT/candidate/requirements-build.lock" \
  "$RELEASE_TOOLS_ROOT/candidate/requirements-runtime.lock" \
  "$RELEASE_TOOLS_ROOT/evidence/READY" \
  "$RELEASE_TOOLS_ROOT/evidence/installation-manifest.json" \
  "$RELEASE_TOOLS_ROOT/evidence/installed-acceptance-evidence.json" \
  "$RELEASE_TOOLS_ROOT/evidence/keyless-gate-evidence.json"; do
  test "$(stat -c '%a:%u:%g' "$protected_file")" = 444:0:0
done

rm -rf -- "$BUNDLE_VERIFY_ROOT"
trap - EXIT
```

`BUNDLE-SHA256SUMS` 校验的是 bundle 内的候选、锁、CI 证据和六个 release tools；
`keyless-gate-evidence.json.release_tools` 会在后续 `prepare`、`verify` 与封存时再校验
六个工具的语义身份。任一摘要、mode、所有者或文件集不符都必须废弃该下载物。

### 4.2 认证主机安装

确认主机和 root 受保护 bundle：

```bash
test "$(. /etc/os-release && printf '%s' "$VERSION_ID")" = "24.04"
test "$(dpkg --print-architecture)" = "amd64"
test "$SNAPSHOT_ID" = "20260725T000000Z"
test -x "$RELEASE_PYTHON"
test ! -L "$RELEASE_PYTHON"
test "$(stat -c '%u:%a' "$RELEASE_PYTHON")" = 0:755
for release_tool in \
  install-production.sh \
  run_keyless_gate_network.sh \
  run_release_gate.py \
  systemd_credential_bridge.py \
  validate_installed_delivery.py \
  validate_release_evidence.py; do
  test -f "$RELEASE_TOOLS_ROOT/scripts/$release_tool"
done
test -z "$(find "$WORKSPACE_PARENT" -mindepth 1 -maxdepth 1 -print -quit)"
```

从已完成两层摘要核对和 root 受保护安装的同一 CI release bundle 取得候选 wheel、
两份锁文件、CI 安装清单与
`READY`、无密钥证据、已安装验收证据和六个 release tools。不得在认证
主机重建 wheel，也不得从 checkout、sdist 或 editable 安装替代它。先为这些
文件设定唯一路径：

```bash
mapfile -t CANDIDATE_WHEELS < <(
  find "$CANDIDATE_ROOT" -maxdepth 1 -type f -name '*.whl' -print
)
test "${#CANDIDATE_WHEELS[@]}" = 1
CANDIDATE_WHEEL="${CANDIDATE_WHEELS[0]}"
BUILD_LOCK="$CANDIDATE_ROOT/requirements-build.lock"
RUNTIME_LOCK="$CANDIDATE_ROOT/requirements-runtime.lock"
CI_INSTALLATION_MANIFEST="$CI_EVIDENCE_ROOT/installation-manifest.json"
CI_INSTALLATION_READY="$CI_EVIDENCE_ROOT/READY"
KEYLESS_EVIDENCE="$CI_EVIDENCE_ROOT/keyless-gate-evidence.json"
INSTALLED_EVIDENCE="$CI_EVIDENCE_ROOT/installed-acceptance-evidence.json"

test -f "$BUILD_LOCK"
test -f "$RUNTIME_LOCK"
test -f "$CI_INSTALLATION_MANIFEST"
test -f "$CI_INSTALLATION_READY"
test -f "$KEYLESS_EVIDENCE"
test -f "$INSTALLED_EVIDENCE"
```

CI 安装清单只证明自动安装验收。真实运行前，必须在认证主机上使用上述同一
wheel、运行锁、snapshot 和 artifact 内的安装器重新安装。`HOST_WHEELHOUSE` 必须
只包含 `requirements-runtime.lock` 指定且摘要匹配的 wheel；当前运行锁无第三方
Python 依赖，因此该目录必须为空：

```bash
HOST_INSTALL_PREFIX="/opt/video-auto-editor-release"
HOST_WHEELHOUSE="$PRIVATE_ROOT/runtime-wheelhouse"
install -d -m 0700 "$HOST_WHEELHOUSE"
test -z "$(find "$HOST_WHEELHOUSE" -mindepth 1 -maxdepth 1 -print -quit)"

WHEEL_SHA256="$(sha256sum -- "$CANDIDATE_WHEEL" | awk '{print $1}')"
RUNTIME_LOCK_SHA256="$(sha256sum -- "$RUNTIME_LOCK" | awk '{print $1}')"
/usr/bin/sudo "$RELEASE_TOOLS_ROOT/scripts/install-production.sh" \
  --wheel "$CANDIDATE_WHEEL" \
  --wheel-sha256 "$WHEEL_SHA256" \
  --wheelhouse "$HOST_WHEELHOUSE" \
  --runtime-lock "$RUNTIME_LOCK" \
  --runtime-lock-sha256 "$RUNTIME_LOCK_SHA256" \
  --apt-snapshot-id "$SNAPSHOT_ID" \
  --prefix "$HOST_INSTALL_PREFIX"

HOST_VERSION_ROOT="$(readlink -f -- "$HOST_INSTALL_PREFIX/current")"
HOST_INSTALLATION_MANIFEST="$HOST_VERSION_ROOT/installation-manifest.json"
HOST_INSTALLATION_READY="$HOST_VERSION_ROOT/READY"
HOST_CONSOLE="$HOST_VERSION_ROOT/venv/bin/video-auto-editor"
test -f "$HOST_INSTALLATION_MANIFEST"
test -f "$HOST_INSTALLATION_READY"
test -x "$HOST_CONSOLE"
```

认证主机安装清单必须证明平台为 Ubuntu 24.04 `amd64`，并绑定同一 wheel、
运行锁和 snapshot。`request.json` 使用 `HOST_INSTALLATION_MANIFEST` 与
`HOST_INSTALLATION_READY`；封存器使用 `CI_INSTALLATION_MANIFEST`。保存 `sha256sum`
输出只用于本地复核，不把私有路径上传。

### 4.3 systemd 系统服务的最小特权契约

冷运行和复跑都必须由 system systemd manager 启动固定命令；不能用 user manager、
普通 shell 中的伪 credential mount 或直接调用门禁替代。推荐由认证主机管理员亲自
执行第 6、7 节的两条命令。若必须委托给发布操作员，管理员只能按候选安装两条**完整
参数精确匹配**的 sudoers 命令：一条固定 `...-cold` 与 `PrivateNetwork=no`，另一条
固定 `...-rerun` 与 `PrivateNetwork=yes`。命令必须逐字绑定：

- `/usr/bin/systemd-run`、符合 `[a-z0-9][a-z0-9-]{0,63}` 的固定候选 slug 所形成的
  unit 名、已按第 6 节校验的加密 credential 绝对路径；
- `UMask=0077`、`NoNewPrivileges=yes`、`LimitCORE=0`、`PrivateUsers=no` 与对应网络属性；
- root 所有且不可写的 `/usr/bin/python3.12`、桥接器、门禁，以及固定 `/var/lib` 受信根
  下 `root:<专用发布组>`/`0440` 的 `plan.json` 绝对路径；
- 固定的非零发布操作员 UID/GID，以及精确的 `execute` 或 `rerun` 子命令。

sudoers 规则不得含通配符、`SETENV`、`-E`、任意 `systemd-run` 属性、root shell、
任意 bridge/gate 参数或 `ALL`。管理员用
`visudo -f /etc/sudoers.d/<候选固定文件名>` 编辑，文件必须为 `root:root`/`0440`，并在
启动前运行 `visudo -c`。任何参数或新尝试路径变化都必须由管理员重新生成精确规则；
不能给发布操作员泛化的 `systemd-run` 权限，因为它等价于任意系统服务创建能力。

## 5. 创建并锁定门禁方案

### 5.1 请求 JSON

`request.json` 是封闭 schema；缺字段、多字段、重复字段、相对路径、符号链接或非普通
文件都会失败。先以 `0600` 创建并用不会记录编辑正文的本地编辑器填写：

```bash
install -m 0600 /dev/null "$REQUEST_PATH"
"${EDITOR:?请设置本地编辑器}" "$REQUEST_PATH"
"$RELEASE_PYTHON" -m json.tool "$REQUEST_PATH" >/dev/null
```

字段必须与以下骨架完全一致；尖括号值必须替换，凭据绝不能填入其中：

```json
{
  "schema_version": "release_gate_request.v1",
  "candidate": {
    "commit_sha": "<commit-sha>",
    "wheel": "/受保护-release-tools-根/candidate/video_auto_editor-<版本>-py3-none-any.whl",
    "build_lock": "/受保护-release-tools-根/candidate/requirements-build.lock",
    "runtime_lock": "/受保护-release-tools-根/candidate/requirements-runtime.lock",
    "installation_manifest": "/认证主机安装绝对路径/installation-manifest.json",
    "installation_ready": "/认证主机安装绝对路径/READY",
    "keyless_gate_evidence": "/受保护-release-tools-根/evidence/keyless-gate-evidence.json",
    "installed_acceptance_evidence": "/受保护-release-tools-根/evidence/installed-acceptance-evidence.json"
  },
  "certified_host": {
    "attestation_id": "<认证主机稳定标识>",
    "apt_snapshot_id": "20260725T000000Z"
  },
  "automation": {
    "run_url": "https://github.com/dulltackle/long-video-autocut/actions/runs/<数字运行号>"
  },
  "inputs": {
    "source": {
      "path": "/私有绝对路径/<真实中文素材>.mp4",
      "asset_id": "<素材稳定标识>",
      "version": "<素材版本稳定标识>",
      "language": "zh-CN",
      "content_summary": "<不超过 500 字的真实中文内容摘要>",
      "duration_ms": 1
    },
    "configuration": "/私有绝对路径/<真实中文素材同名>.config.json",
    "course_context": "/私有绝对路径/<真实中文素材同名>.context.json",
    "expected_transcript": "/私有绝对路径/<预期转写>.json"
  },
  "execution": {
    "console": "/已安装绝对路径/bin/video-auto-editor",
    "independent_validator": "/受保护-release-tools-根/scripts/validate_installed_delivery.py",
    "credential_bridge": "/受保护-release-tools-根/scripts/systemd_credential_bridge.py",
    "network_guard": "/受保护-release-tools-根/scripts/run_keyless_gate_network.sh",
    "workspace_parent": "/私有绝对路径/<候选标识>/workspaces"
  },
  "release": {
    "version": "<MAJOR.MINOR.PATCH>",
    "tag": "v<MAJOR.MINOR.PATCH>"
  }
}
```

`duration_ms` 要填真实正整数而非示例值；`language` 必须精确为 `zh-CN`，
`content_summary` 必须是非空、最多 500 字且不含私有路径或敏感正文的真实中文素材
摘要。`asset_id`、素材 `version`、主机证明标识和路径都必须稳定。配置和课程上下文
不是可任意选择的 JSON：若素材为
`/private/course.mp4`，则它们的路径必须分别为同目录同名的
`/private/course.config.json` 和 `/private/course.context.json`。门禁通过
`Path.with_suffix()` 严格发现这两个 sidecar，其他名称或路径会失败。六个 release
tools 的路径必须全部来自同一 CI artifact。完整可执行请求夹具见
[真实门禁测试](../tests/test_release_gate.py) 的 `_release_gate_fixture`。

`automation.run_url` 只能是本仓库的正整数 Actions run URL，或其正整数 attempt URL；
不得带查询、片段、userinfo 或端口。配置必须固定 StepAudio
`stepaudio-2.5-asr`、`https://api.stepfun.com/v1/audio/asr/sse`，以及 StepFun
`step-2-mini`、`https://api.stepfun.com/v1`；转写与文本能力的 credential 环境变量名都
必须精确为 `STEPFUN_API_KEY`。计划准备、每次验证、原始供应商请求证据和最终封存都会
重复核对这些值，任意自定义 endpoint 都会在发送凭据前失败。

### 5.2 prepare 与 verify

`prepare` 必须在没有 `STEPFUN_API_KEY` 的普通环境中执行，目标 `plan.json` 必须不存在：

```bash
test -z "${STEPFUN_API_KEY+x}"
"$RELEASE_PYTHON" -I "$RELEASE_TOOLS_ROOT/scripts/run_release_gate.py" prepare \
  --request "$REQUEST_PATH" \
  --plan "$DRAFT_PLAN_PATH"
test "$(stat -c '%a' "$DRAFT_PLAN_PATH")" = 600

"$RELEASE_PYTHON" -I "$RELEASE_TOOLS_ROOT/scripts/run_release_gate.py" verify \
  --plan "$DRAFT_PLAN_PATH"

test ! -e "$LOCKED_PLAN_DIRECTORY"
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 0755 \
  "$LOCKED_PLAN_ROOT"
test "$(stat -c '%u:%g:%a' "$LOCKED_PLAN_ROOT")" = "0:0:755"
/usr/bin/sudo /usr/bin/mkdir --mode=0710 "$LOCKED_PLAN_DIRECTORY"
/usr/bin/sudo /usr/bin/chown root:"$RELEASE_OPERATOR_GID" \
  "$LOCKED_PLAN_DIRECTORY"
/usr/bin/sudo /usr/bin/install \
  -o root -g "$RELEASE_OPERATOR_GID" -m 0440 \
  "$DRAFT_PLAN_PATH" "$PLAN_PATH"
test "$(stat -c '%u:%g:%a' "$LOCKED_PLAN_DIRECTORY")" = \
  "0:$RELEASE_OPERATOR_GID:710"
test "$(stat -c '%u:%g:%a' "$PLAN_PATH")" = \
  "0:$RELEASE_OPERATOR_GID:440"
test "$(sha256sum -- "$DRAFT_PLAN_PATH" | awk '{print $1}')" = \
  "$(sha256sum -- "$PLAN_PATH" | awk '{print $1}')"

"$RELEASE_PYTHON" -I "$RELEASE_TOOLS_ROOT/scripts/run_release_gate.py" verify \
  --plan "$PLAN_PATH"
```

生成的 `release_gate_plan.v1` 草案不得手工编辑；管理员锁定后，后续命令必须把
`PLAN_PATH` 指向锁定副本，不能再使用草案。计划严格包含：

- `candidate`：`commit_sha`、`version`，以及 wheel、构建锁、运行锁各自的
  `filename`、规范绝对 `path`、`sha256`；
- `certified_host`：主机证明、snapshot，以及安装清单和 `READY` 的文件事实；
- `automation`：运行 URL、无密钥证据和已安装验收证据的文件事实；
- `inputs`：素材的文件事实、稳定标识、字节数、真实时长，以及三份固定输入的文件事实；
- `execution`：已安装 console、独立校验器、空 workspace 父目录，固定声明
  `new_with_empty_processing_cache`、`systemd_credentials`、`stepfun_api_key` 和
  `cold_then_overwrite: true`；
- `release`：与 wheel 元数据一致的版本和 `v<版本>` tag 名。

其中 `certified_host.installation` 固化认证主机安装清单、`READY` 和 console；
`automation.release_tools` 从 `keyless-gate-evidence.json` 取得六个工具摘要；
`execution` 再把实际使用的独立校验器、凭据桥接器与网络 guard 与这些摘要逐一对齐。
以后每个阶段都会重新计算摘要并拒绝漂移。`verify` 通过不是可以修改输入的许可。

## 6. systemd credential 临时注入

凭据必须由主机管理员在带外写入 systemd 加密 credential 文件，并以
`stepfun_api_key` 为 credential ID。不得在命令行读取、粘贴、`export`、写 JSON、写
shell 配置或通过标准输入传给桥接器。凭据 UTF-8 内容必须非空、不含 NUL/换行，且不
超过 4096 字节。

每次需要真实供应商访问时，都使用固定桥接命令。下面只有加密文件路径进入 argv，
明文值不会进入 argv；执行前再次确认 xtrace 已关闭：

```bash
set +x
test -z "${STEPFUN_API_KEY+x}"
ENCRYPTED_CREDENTIAL="/etc/credstore.encrypted/video-auto-editor/$CANDIDATE_SLUG"
test "$ENCRYPTED_CREDENTIAL" = "$(readlink -f -- "$ENCRYPTED_CREDENTIAL")"
test -f "$ENCRYPTED_CREDENTIAL"
test ! -L "$ENCRYPTED_CREDENTIAL"
test "$(stat -c '%u:%g:%a' -- "$ENCRYPTED_CREDENTIAL")" = "0:0:400"
credential_parent="$(dirname -- "$ENCRYPTED_CREDENTIAL")"
while :; do
  read -r parent_uid parent_gid parent_mode < <(
    stat -c '%u %g %a' -- "$credential_parent"
  )
  test "$parent_uid" = 0
  test "$parent_gid" = 0
  (( (8#$parent_mode & 0022) == 0 ))
  test ! -L "$credential_parent"
  test "$credential_parent" != / || break
  credential_parent="$(dirname -- "$credential_parent")"
done

/usr/bin/sudo /usr/bin/systemd-run --wait --collect --pipe \
  --unit="video-auto-editor-release-$CANDIDATE_SLUG-cold" \
  --property="UMask=0077" \
  --property="NoNewPrivileges=yes" \
  --property="LimitCORE=0" \
  --property="PrivateUsers=no" \
  --property="PrivateNetwork=no" \
  --property="LoadCredentialEncrypted=stepfun_api_key:$ENCRYPTED_CREDENTIAL" \
  "$RELEASE_PYTHON" -I \
  "$RELEASE_TOOLS_ROOT/scripts/systemd_credential_bridge.py" \
  --operator-uid "$RELEASE_OPERATOR_UID" \
  --operator-gid "$RELEASE_OPERATOR_GID" -- \
  "$RELEASE_PYTHON" -I \
  "$RELEASE_TOOLS_ROOT/scripts/run_release_gate.py" execute \
  --plan "$PLAN_PATH"
```

[credential bridge](../scripts/systemd_credential_bridge.py) 只接受 `--` 后的绝对命令，
并且自身必须先以 initial user namespace 中的 host root 身份运行。它要求进程 cgroup
精确属于 `/system.slice/video-auto-editor-release-<候选标识>-<cold|rerun>.service`，
`CREDENTIALS_DIRECTORY` 精确位于对应 `/run/credentials/<unit>.service`，再只打开其中的
`stepfun_api_key` 普通文件。目录和凭据必须为 root 所有、只读文件系统，目录不得给组
或其他用户任何权限，凭据模式必须精确为 `0400`；user systemd、用户 namespace、
自建只读 bind/FUSE mount 和直接伪造 FD 都会失败。

桥接器不会把凭据正文复制到环境变量。它以 root 打开真实 systemd credential 文件，
复核固定命令和运行上下文，然后清空补充组、降到固定非零操作员 UID/GID。它移除
`CREDENTIALS_DIRECTORY`，再通过只含文件描述符编号的
`RELEASE_GATE_SYSTEMD_CREDENTIAL_FD` 把该**原始只读 credential 文件描述符**继承给门禁。
门禁立即移除该描述符和 host network namespace 环境变量，复核自己已是非 root、
仍在 initial user namespace 和同一 system service cgroup，并通过文件描述符验证
root 所有普通文件、精确 `0400`、非空且最多 4096 字节、只读文件系统、精确 systemd
credential 路径、未删除状态与读取前后身份不变。验证后关闭描述符，只向真实
`live` 子进程临时注入 `STEPFUN_API_KEY`。

桥接命令也是封闭的：只接受与桥接器使用同一个受保护目录的固定门禁，以及与桥接器
使用同一个**已解析普通文件路径**的受保护
Python 3.12 解释器（不接受 `/usr/bin/python3` 等符号链接）的
`run_release_gate.py execute --plan <绝对路径>` 或 `rerun --plan <绝对路径>`。它在传递
凭据文件描述符前会把计划路径硬绑定到
`/var/lib/video-auto-editor-release-plans/<候选标识>/plan.json`，校验受信根及目录链不可
由操作员替换、`plan.json` 为 `root:<专用发布组>`/`0440`、直接父目录为
`root:<专用发布组>`/`0710`，并把计划中的桥接器/门禁路径和 SHA-256 与 root 所有、
不可写的同目录 bundle 文件交叉核对。子进程只得到
`/usr/sbin:/usr/bin:/sbin:/bin` 的固定 `PATH`，core limit 固定为零。
预先存在的 `STEPFUN_API_KEY`、任意绝对命令、伪造 credential 系统服务、符号链接、
非普通文件、stdin 兜底、凭据参数或凭据 JSON 都必须失败。不要把 systemd 日志或命令
输出重定向到公开位置。

## 7. 冷运行、人工复核与零请求复跑

必须严格保持以下业务顺序，且三阶段都绑定同一 `plan.json`、同一候选和同一
`attempt-NNNN.workspace`：

1. `execute` 从新建、空处理缓存 workspace 做真实冷运行；StepAudio 转写、StepFun
   主题评审和字幕优化都必须产生成功的真实请求，至少发布一条短视频，并完成内建和
   独立交付校验。
2. 操作员逐条观看冷运行的全部短视频，并对照原素材和忠实转写记录人工复核。
3. 人工复核固化后才允许复跑；复跑在同一 workspace 显式 `--overwrite`，三种远程
   能力请求数必须严格为零，整场转写、主题评审、字幕优化必须命中。
4. 冷交付必须保留为 `delivery.previous/`，复跑交付为 `delivery/`；两者分别通过
   无凭据独立校验，业务投影 SHA-256 完全相同。

`execute` 成功后会独占创建 `attempt-NNNN.cold.json`，状态必须是
`awaiting_manual_review`。此时只有一次运行，只有 `delivery/`，还没有最终
`attempt-NNNN.json` 或 `delivery.previous/`。子进程成功消息应为
`真实冷运行已通过，等待人工复核`。以第一次尝试为例：

```bash
COLD_RECORD="$WORKSPACE_PARENT/attempt-0001.cold.json"
test -f "$COLD_RECORD"
test ! -e "$WORKSPACE_PARENT/attempt-0001.json"
test ! -e "$WORKSPACE_PARENT/attempt-0001.workspace/delivery.previous"
"$RELEASE_PYTHON" -m json.tool "$COLD_RECORD" >/dev/null
```

人工复核必须覆盖每个 ordinal，且六项全部为 `true`：

- `topic_complete`：主题完整；
- `boundaries_natural`：首尾自然；
- `audio_video_normal`：音画正常；
- `subtitles_faithful_readable`：烧录字幕忠实、可读；
- `title_summary_grounded`：标题和摘要有素材依据、无虚构；
- `excluded_content_absent`：课程上下文声明的排除内容没有泄漏。

人工复核输入必须是 `0600` 普通文件，字段严格如下。`run_id` 必须来自冷运行记录，
`clips` 必须与冷运行短视频数相同，ordinal 从 1 连续递增；下面只展示一条，实际必须
逐条填写全部短视频：

```json
{
  "schema_version": "release_gate_manual_review.v1",
  "operator_id": "<稳定操作员标识>",
  "reviewed_at": "<YYYY-MM-DDTHH:MM:SS.mmmZ>",
  "run_id": "<冷运行-run_id>",
  "source_and_transcript_compared": true,
  "clips": [
    {
      "ordinal": 1,
      "checks": {
        "topic_complete": true,
        "boundaries_natural": true,
        "audio_video_normal": true,
        "subtitles_faithful_readable": true,
        "title_summary_grounded": true,
        "excluded_content_absent": true
      }
    }
  ],
  "conclusion": "passed"
}
```

人工观看完成后，在无凭据环境中固化复核记录：

```bash
MANUAL_REVIEW_PATH="$PRIVATE_ROOT/attempt-0001.manual-review.json"
install -m 0600 /dev/null "$MANUAL_REVIEW_PATH"
"${EDITOR:?请设置本地编辑器}" "$MANUAL_REVIEW_PATH"
"$RELEASE_PYTHON" -m json.tool "$MANUAL_REVIEW_PATH" >/dev/null
test -z "${STEPFUN_API_KEY+x}"

"$RELEASE_PYTHON" -I "$RELEASE_TOOLS_ROOT/scripts/run_release_gate.py" record-review \
  --plan "$PLAN_PATH" \
  --review "$MANUAL_REVIEW_PATH"
test -f "$WORKSPACE_PARENT/attempt-0001.review.json"
```

任一检查不是 `true`、未对照原素材与忠实转写、结论不是 `passed`，都会以
`review.failed` 失败，且不会创建可供复跑的记录。复核记录通过后，才可用同一
systemd credential 桥接执行 `rerun`。`record-review` 的成功消息应为
`人工内容复核已不可变记录`：

```bash
set +x
test -z "${STEPFUN_API_KEY+x}"

/usr/bin/sudo /usr/bin/systemd-run --wait --collect --pipe \
  --unit="video-auto-editor-release-$CANDIDATE_SLUG-rerun" \
  --property="UMask=0077" \
  --property="NoNewPrivileges=yes" \
  --property="LimitCORE=0" \
  --property="PrivateUsers=no" \
  --property="PrivateNetwork=yes" \
  --property="LoadCredentialEncrypted=stepfun_api_key:$ENCRYPTED_CREDENTIAL" \
  "$RELEASE_PYTHON" -I \
  "$RELEASE_TOOLS_ROOT/scripts/systemd_credential_bridge.py" \
  --operator-uid "$RELEASE_OPERATOR_UID" \
  --operator-gid "$RELEASE_OPERATOR_GID" -- \
  "$RELEASE_PYTHON" -I \
  "$RELEASE_TOOLS_ROOT/scripts/run_release_gate.py" rerun \
  --plan "$PLAN_PATH"

mapfile -t SUCCESSFUL_ATTEMPTS < <(
  "$RELEASE_PYTHON" -I - "$WORKSPACE_PARENT" <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in sorted(root.glob("attempt-*.json")):
    if re.fullmatch(r"attempt-[0-9]{4}\.json", path.name) is None:
        continue
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        continue
    if value.get("schema_version") == "release_gate_attempt.v1" and value.get(
        "status"
    ) == "passed":
        print(path)
PY
)
test "${#SUCCESSFUL_ATTEMPTS[@]}" = 1
SUCCESSFUL_ATTEMPT_PATH="${SUCCESSFUL_ATTEMPTS[0]}"
test "$SUCCESSFUL_ATTEMPT_PATH" = \
  "$(readlink -f -- "$SUCCESSFUL_ATTEMPT_PATH")"
SUCCESSFUL_ATTEMPT_ID="$(basename -- "$SUCCESSFUL_ATTEMPT_PATH" .json)"
test -d \
  "$WORKSPACE_PARENT/$SUCCESSFUL_ATTEMPT_ID.workspace/delivery.previous"
```

`rerun` 的成功消息应为 `零请求缓存复跑已通过`；最终
`attempt-NNNN.json` 必须是 `release_gate_attempt.v1`、`status: passed`，并同时绑定
冷运行、人工复核、复跑、语义等价、三次独立校验和凭据泄漏扫描结论。

复跑不是直接执行 console。第 7 节固定的 system systemd service 使用
`PrivateNetwork=yes`，因此在 credential bridge 启动前就已建立独立网络命名空间。
桥接器仍以 host root 身份验证当前 namespace 与 `/proc/1/ns/net` 不同、接口集合精确
只有已启用的 `lo`，随后才降到固定操作员 UID/GID，并把经过验证的 host namespace
标识交给门禁。任何直接调用 bridge、伪造环境变量或使用 user systemd 的做法都会因
系统 service cgroup、credential mount 或 user namespace 不匹配而失败。

`plan.json` 已把 `execution.network_guard` 的路径与 SHA-256 绑定到同一 CI artifact 中
`run_keyless_gate_network.sh` 的 `release_tools` 摘要。门禁在内部用该 guard 包装
`live --overwrite`，并创建匿名 pipe，只把写端文件描述符传给绑定 guard。生产复跑
分支不再调用 sudo 或 `unshare`；guard 只在已建立的 systemd namespace 中重新核对
当前 namespace 与桥接器提供的 host namespace 不同、接口集精确只有已启用的 `lo`。
通过后，它从 pipe 写出一条
`release_gate_network.v1<TAB><host-netns><TAB><isolated-netns><TAB>lo`，在执行 console 前关闭
证明 FD。不得降级成 Python guard、仅依赖零请求计数、替换脚本，或给门禁/guard
任何提权环境；无法证明 systemd 隔离时必须失败关闭。

门禁只从 pipe 读取最多 4096 字节，重新验证 schema、host 精确等于桥接器传入的
受信 namespace、isolated 与 host 不同且格式合法、接口只有 `lo`。仅该证明成功时，
最终尝试才记录
`cache_rerun.network_isolation.{mode: linux_network_namespace, external_blocked: true, loopback_allowed: true, attestation_verified: true, guard_sha256: <绑定摘要>}`。
伪 guard、空/格式错误证明、继承旧环境声明或只有零请求计数都不能产生该记录。

独立校验必须在没有 `STEPFUN_API_KEY` 的环境中验证摘要、精确文件集合、忠实转写、
MP4、路径安全、引用和 schema。语义等价不是只比较数量：门禁规范化冷/热
`transcript.json`、`plan.json` 和 `metadata.json`，只排除运行 ID、业务 UUID 与路径
等非业务差异，再比较规范 JSON 的 SHA-256。任一远程请求、缓存损坏/基础设施错误、
必要未命中、上一版缺失、独立校验失败或语义漂移都失败关闭。

任何子命令失败都以 `真实门禁失败：<稳定原因码>` 写入 stderr 并非零退出。已创建
尝试时，工具会独占写最终 `attempt-NNNN.json`，以 `failure_phase` 区分
`cold_run`、`manual_review` 或 `cache_rerun`：

- 冷运行失败后不得直接 `record-review` 或 `rerun`；
- 尚未提交人工复核就调用 `rerun` 只返回 `review.required`，不会伪造复跑证据；
- 人工复核任一项失败会以 `manual_review` 阶段拒绝候选，不能改 JSON 后复用该尝试；
- 复跑发生远程请求、未命中、上一版丢失或语义漂移时，以 `cache_rerun` 阶段拒绝候选；
- 只有第 8 节封闭分类成功后才能创建下一编号尝试；下一尝试必须重新冷运行并重新
  逐条人工复核，不能继承前一次结果。

## 8. 失败分类与复跑

失败尝试记录是不可变事实，不能删除、覆盖、改写或复制成成功。默认分类为
`candidate_rejected`，禁止同一候选复跑。只有脚本明确把尝试保持为 `unclassified`，
并列出许可分类时，操作员才能在无凭据环境中创建一次独占分类记录：

```bash
test -z "${STEPFUN_API_KEY+x}"
"$RELEASE_PYTHON" -I "$RELEASE_TOOLS_ROOT/scripts/run_release_gate.py" classify \
  --plan "$PLAN_PATH" \
  --attempt "$WORKSPACE_PARENT/attempt-0001.json" \
  --classification provider_transient \
  --operator-id "<稳定操作员标识>"
```

封闭分类只有：

- `provider_transient`：只允许来自 `cold_run`，且对应失败 `run.json` 已记录同一个稳定
  供应商短暂错误码，例如限流、请求超时或服务暂不可用；热复跑中的任何供应商请求或
  供应商错误都属于硬门禁失败，不能归入此类；
- `certified_host_infrastructure`：工具明确允许归类的认证主机基础设施故障。

不能凭操作员判断给任意失败套用分类；`classify` 会同时复核候选、输入指纹、计划
摘要、允许集合和目标路径。未分类失败不能复跑，分类文件存在前不能创建下一尝试。
缓存远程请求、语义漂移、凭据泄漏、候选/输入漂移、schema 错误和其他硬门禁均不可
分类放行。若需修改任何候选或输入，使用新的私有根目录重新 `prepare`。

## 9. 汇总源证据并独占封存

### 9.1 `release_evidence_source.v1`

人工复核与零请求复跑都成功，并已把唯一成功尝试固定为
`SUCCESSFUL_ATTEMPT_PATH` 后，以 `0600` 创建私有源证据：

```bash
install -m 0600 /dev/null "$SOURCE_EVIDENCE_PATH"
"${EDITOR:?请设置本地编辑器}" "$SOURCE_EVIDENCE_PATH"
"$RELEASE_PYTHON" -m json.tool "$SOURCE_EVIDENCE_PATH" >/dev/null
```

源证据是封闭 schema，所有值必须从计划、不可变尝试记录、运行/交付清单、独立校验
结果和人工复核记录逐项**人工转录**，不得猜测或补造。它只是待验证的
摘要声明，不是可以被封存器盲信的根证据。封存器会使用 `--plan` 与 `--attempt`
定位计划、冷运行、人工复核、最终尝试、工作区、交付、运行诊断和独立校验
原件，重新计算事实与 SHA-256 后才与转录值交叉比对。精确可通过夹具见
[发布证据测试](../tests/test_release_evidence.py) 的 `_create_valid_inputs`。字段集合如下：

- 顶层：`schema_version`、`candidate`、`locks`、`apt_snapshot_id`、
  `automatic_gate_runs`、`inputs`、`runs`、`independent_validations`、
  `semantic_equivalence`、`manual_review`、`retry_attempts`、`known_limitations`；
- `candidate`：`application_version`、`commit_sha`、`wheel_filename`、
  `wheel_sha256`；`locks.build` 与 `locks.runtime` 各为 `filename`、`sha256`；
- `automatic_gate_runs.keyless` 与 `installed_acceptance` 各只含无查询参数的 `url`，
  且两者必须是同一个 GitHub Actions run 或 attempt URL；
- `inputs.source`：`asset_id`、`version`、`language`、`content_summary`、`sha256`、
  `byte_length`、`duration_ms`；`language` 精确为 `zh-CN`，`content_summary` 是非空、
  不超过 500 字且可公开的真实中文素材摘要；
  `configuration`、`course_context`、`expected_transcript` 各只含对应
  `schema_version` 和 `sha256`；
- `runs.cold` 与 `runs.warm`：各含 `run_id`、`terminal`、
  `diagnostic_manifest_sha256`、`delivery`、`environment`、`configuration`、
  `providers`、`cache`；
- `terminal` 精确为成功、退出码 `0`、`clips`；`delivery` 含
  `manifest_sha256`、`result_kind`、正 `artifact_count` 和正
  `short_video_count`；
- `environment` 逐项公开真实运行环境：认证平台、Python、应用、FFmpeg/FFprobe、
  字体、preflight 结论和安装指纹；`configuration` 公开配置指纹，以及课程上下文是否
  提供、是否含归因、优先主题数和排除内容数。冷/热两次必须完全一致，并与各自原始
  `run.json`、计划及认证主机安装清单交叉核对；
- `providers` 精确含 `transcription`、`topic_review`、
  `subtitle_optimization`。每项含 `provider_id`、`model_id` 和
  `requests.{count,succeeded,failed,attempt_count_total}`；冷运行三项请求均非零且全成功，
  复跑三项请求严格为零；认证组合固定为 StepAudio `stepaudio-2.5-asr` 和两项
  StepFun `step-2-mini`；
- `cache` 精确含 `transcript`、`transcription_shard`、`topic_review`、
  `subtitle_optimization`。每项统计精确含 `queries`、`hits`、`misses`、
  `corrupt_quarantined`、`writes_published`、`writes_already_present`、
  `infrastructure_failures`、`singleflight_wait_count`、
  `singleflight_wait_ms_total`；冷运行全为 miss 并发布写入，复跑除 shard 为全零外均为
  全 hit、零 miss、零写入；
- `independent_validations.cold` 与 `.warm`：各含
  `independent_delivery_validation.v1` 的成功结论、对应 `run_id`、结果类型、短视频数、
  工件数、`evidence_sha256`，以及 `digests`、`exact_file_set`、
  `faithful_transcript`、`mp4`、
  `path_safety`、`references`、`schema` 七项全真检查；
- `semantic_equivalence`：`equivalent: true`，且冷/热投影 SHA-256 相等；
- `manual_review`：`release_gate_manual_review.v1`、稳定 `operator_id`、UTC
  `reviewed_at`、冷运行 `run_id`、`source_and_transcript_compared: true`、连续从 1 开始的
  全部 `clips` 和 `conclusion: passed`，每条含本手册第 7 节的六项全真检查；
  封存器还要求尝试开始、冷运行结束、人工复核、复核固化、尝试结束按时间单调，
  且尝试结束不得超过当前时间五分钟容差；
- `retry_attempts`：所有允许复跑的失败，每项精确含 `run_id`、UTC `occurred_at`、
  封闭 `classification`、稳定 `stable_error_code`、以及同一
  `candidate.{commit_sha,wheel_sha256}`；没有则为空数组；
- `known_limitations`：必须是只含下面唯一精确对象的数组，不得删除、增加或
  改写：

  ```json
  [
    {
      "code": "certified_platform_scope",
      "statement": "首次生产版本只认证 Ubuntu 24.04 amd64。"
    }
  ]
  ```

常用转录映射如下；仍以严格校验器和测试夹具为准：

| 源证据字段 | 不可变本地来源 |
| --- | --- |
| 候选、锁、snapshot、输入摘要 | `plan.json` 与对应普通文件的实际摘要 |
| `runs.cold` | `attempt-NNNN.cold.json`、冷 `work/runs/<run_id>/run.json`、冷交付清单 |
| `manual_review` | `attempt-NNNN.review.json`；保留 `reviewed_at` 与全部 clips |
| `runs.warm` | 最终 `attempt-NNNN.json`、热 `work/runs/<run_id>/run.json`、热交付清单 |
| `independent_validations` | `attempt-NNNN.private/cold-validation.json`、`previous-validation.json` 与 `warm-validation.json`；源证据转录 cold/warm，封存器分别重算 cold/previous/warm 三份原件 |
| `semantic_equivalence` | 最终尝试的业务投影摘要，冷/热字段填同一 SHA-256 |
| `retry_attempts` | 所有已分类失败记录、分类记录和对应失败运行诊断 |

### 9.2 独占封存

输出父目录可以预先存在，但 `release-evidence.json` 必须不存在；封存器绝不覆盖：

```bash
test ! -e "$FINAL_EVIDENCE_PATH"
test -z "${STEPFUN_API_KEY+x}"

"$RELEASE_PYTHON" -I "$RELEASE_TOOLS_ROOT/scripts/validate_release_evidence.py" \
  --source "$SOURCE_EVIDENCE_PATH" \
  --plan "$PLAN_PATH" \
  --attempt "$SUCCESSFUL_ATTEMPT_PATH" \
  --wheel "$CANDIDATE_WHEEL" \
  --build-lock "$BUILD_LOCK" \
  --runtime-lock "$RUNTIME_LOCK" \
  --installation-manifest "$CI_INSTALLATION_MANIFEST" \
  --keyless-evidence "$KEYLESS_EVIDENCE" \
  --installed-evidence "$INSTALLED_EVIDENCE" \
  --output "$FINAL_EVIDENCE_PATH"

test "$(stat -c '%a' "$FINAL_EVIDENCE_PATH")" = 444
sha256sum "$FINAL_EVIDENCE_PATH"
```

[封存器](../scripts/validate_release_evidence.py) 会严格交叉核对 wheel、两份锁、CI 安装
清单、认证主机安装清单与 `READY`、snapshot、自动门禁、六个 release tools、
输入 sidecar、原始运行诊断与交付清单、冷/上一版/热三份独立校验、人工复核、
已分类重试历史与候选身份。它会在公开证据中写入源证据、计划、最终尝试、
冷运行记录、复核记录、认证主机安装产物和 release tools 的脱敏摘要；
`artifacts.release_evidence_source` 只证明哪份人工转录参与了封存，不替代上述原件重算。
`installation` 和 `dependencies` 分别以 `ci_installed_acceptance`、`certified_host` 两个
scope 公开平台、Python、媒体环境、snapshot packages、完整系统包和 wheelhouse；两份
清单都必须通过同一封闭 producer schema，且认证主机清单还要绑定真实运行环境指纹。
全部核对通过后，封存器使用独占链接创建
固定名称 `release-evidence.json`，`fsync` 文件和目录并设为 `0444`。任何失败都不得
保留或上传部分输出；目标已存在时也必须失败，不能删除旧证据后重试。

## 10. #49 签名发布交接

#48 在交付并验证工具与契约后即停止，不执行独占封存。只有 #49 的操作员真实
完成冷运行、人工复核、复跑和独占封存，并与维护者完成以下全部复核后，
才进入签名发布：

- `release-evidence.json`、计划、wheel 元数据和预定 tag 的版本完全一致；
- commit SHA、wheel SHA-256、构建锁、运行锁、安装清单和最终证据 SHA-256 已双人核对；
- 上传允许清单只含**同一个已验收 wheel**、两份哈希锁、脱敏安装/依赖清单、中文
  发布说明和最终 `release-evidence.json`；
- 中文发布说明不含素材、转写、上下文、本地路径、凭据、日志正文或私有附件；
- 维护者创建 `vMAJOR.MINOR.PATCH` 的签名 annotated tag，tag message 记录 commit、
  wheel 与最终证据摘要；随后创建绑定该 tag 和同一 wheel 的 GitHub Release；
- 发布阶段禁止再次运行构建命令、替换 wheel、移动 tag 或改写证据。任何变更或补验
  都使用新版本并从自动门禁重新开始。

## 11. 操作员最终检查表

- [ ] 认证主机是 Ubuntu 24.04 LTS `amd64`，snapshot 是 `20260725T000000Z`。
- [ ] `release-bundle.tar` 的 GitHub artifact digest、精确文件集和
  `BUNDLE-SHA256SUMS` 全部通过；wheel、两份锁、两份自动证据与六个 release tools
  来自该同一 CI artifact。
- [ ] 所有 release tools 只从 root 所有、无写权的 `$RELEASE_TOOLS_ROOT` 使用，
  没有直接执行下载文件或 checkout 中的同名工具。
- [ ] CI 安装清单只用于封存自动安装验收；请求绑定的是认证主机重新安装
  生成的安装清单和 `READY`。
- [ ] 私有目录为 `0700`、私有文件为 `0600`；专用发布 UID/主组未共享，固定 `PATH`，
  shell xtrace 已关闭。
- [ ] 草案计划与锁定计划字节相同；锁定计划位于固定 `/var/lib` 受信根，文件为
  `root:<专用发布组>`/`0440`、父目录为 `root:<专用发布组>`/`0710`，目录链不可替换。
- [ ] 加密 credential 是规范绝对路径、无符号链接、`root:root`/`0400` 且全部父目录
  root 控制无组/其他写；systemd credential 目录只读、明文凭据文件为 `0400`；原始只读
  文件描述符校验通过，
  `STEPFUN_API_KEY` 不在父环境、argv、JSON、历史、日志或附件中。
- [ ] 自动门禁、安装验收、候选 commit 与 wheel 摘要绑定且全部通过。
- [ ] `prepare` 和每次 `verify` 均通过，候选、输入与执行程序没有漂移。
- [ ] 冷运行真实联系 StepAudio/StepFun，至少产生一条短视频并通过独立校验。
- [ ] 全部冷运行短视频完成六项逐条人工复核。
- [ ] 本次尝试只由管理员直接运行两条固定 systemd 命令，或 sudoers 逐字授权这两条
  完整命令；无通配符、环境保留、任意属性、root shell 或可变 bridge/gate 参数，且
  `LimitCORE=0`、候选 slug、`root:root`/`0440` 与 `visudo -c` 已复核。
- [ ] 同 workspace 覆盖复跑由 `PrivateNetwork=yes` 进入仅回环 Linux network
  namespace，绑定 network guard 无提权地复核该隔离，
  pipe 证明 `attestation_verified: true`，供应商请求为零、必要缓存全命中、上一版保留。
- [ ] 冷交付、保留的上一版和热交付三份独立校验原件均被封存器重算核对，
  冷/热业务投影语义等价。
- [ ] CI 与认证宿主两套完整平台、Python、媒体环境、snapshot packages、系统包和
  wheelhouse 已分别校验并以不同 scope 公开。
- [ ] 失败尝试完整保留；只有封闭分类允许同一候选复跑。
- [ ] `release-evidence.json` 以独占方式创建为 `0444`，SHA-256 已复核。
- [ ] 私有素材、正文、工作区、原始记录和任何凭据均未上传。
- [ ] #48 没有创建 tag 或 Release；#49 不重建 wheel，只发布同一验收构建物。
