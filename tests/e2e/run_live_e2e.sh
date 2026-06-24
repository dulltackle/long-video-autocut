#!/usr/bin/env bash
#
# 直播拆条全流程端到端真实跑通脚本。
#
# 用真实素材 + 真实 config.local.json + 真实 StepFun API 完整跑通 live 拆条，
# 一直跑到真实导出（clips/、subtitles/、metadata.json），再调用交付物校验器断言结果。
#
# 注意：这是真实集成跑通，会调用 StepFun API（消耗额度）并执行 ffmpeg 真实剪辑。
# ASR 转写若命中 work-dir 缓存则复用，不重复请求 ASR；主题评审与导出仍真实执行。
#
# 可用环境变量覆盖默认值：
#   E2E_VIDEO   输入视频（默认仓库根目录的宣讲 mp4）
#   E2E_CONFIG  配置文件（默认 config.local.json）
#   E2E_OUT     输出目录（默认 out）
#   E2E_WORK    工作目录（默认 work，复用已有 ASR 缓存）
#   E2E_MAX_CLIPS  可选，传递给 --max-clips，便于快速验证导出链路
#   E2E_ALLOW_UNREVIEWED_EXPORT  可选，仅用于人工兼容验证；标准校验仍要求 reviewed
#   E2E_EXTRA_ARGS  可选，追加额外 CLI 参数；谨慎使用，不改变默认流程
#
# 用法：
#   export STEPFUN_API_KEY=sk-...
#   bash tests/e2e/run_live_e2e.sh

set -euo pipefail

# 定位仓库根目录（脚本位于 <root>/tests/e2e/）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT_DIR}"

E2E_VIDEO="${E2E_VIDEO:-妇美·愉悦技术规范宣讲——张铃院长（2026年6月13日）.mp4}"
E2E_CONFIG="${E2E_CONFIG:-config.local.json}"
E2E_OUT="${E2E_OUT:-out}"
E2E_WORK="${E2E_WORK:-work}"
E2E_MAX_CLIPS="${E2E_MAX_CLIPS:-}"
E2E_ALLOW_UNREVIEWED_EXPORT="${E2E_ALLOW_UNREVIEWED_EXPORT:-}"
E2E_EXTRA_ARGS="${E2E_EXTRA_ARGS:-}"

echo "============================================================"
echo "  直播拆条 全流程端到端真实跑通"
echo "  视频：${E2E_VIDEO}"
echo "  配置：${E2E_CONFIG}"
echo "  输出：${E2E_OUT}    工作目录：${E2E_WORK}"
if [[ -n "${E2E_MAX_CLIPS}" ]]; then
  echo "  最大导出数：${E2E_MAX_CLIPS}"
fi
if [[ -n "${E2E_ALLOW_UNREVIEWED_EXPORT}" ]]; then
  echo "  未评审兼容导出：${E2E_ALLOW_UNREVIEWED_EXPORT}"
fi
if [[ -n "${E2E_EXTRA_ARGS}" ]]; then
  echo "  额外参数：${E2E_EXTRA_ARGS}"
fi
echo "============================================================"

# 1. 校验输入文件存在
if [[ ! -f "${E2E_VIDEO}" ]]; then
  echo "❌ 找不到输入视频：${E2E_VIDEO}" >&2
  exit 1
fi
if [[ ! -f "${E2E_CONFIG}" ]]; then
  echo "❌ 找不到配置文件：${E2E_CONFIG}" >&2
  exit 1
fi

# 2. 选择 python 解释器（优先 python3）
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "❌ 找不到 python3 / python 解释器" >&2
  exit 1
fi

# 3. 选择 CLI 调用方式：优先已安装命令，否则回退模块入口
if command -v video-auto-editor >/dev/null 2>&1; then
  CLI=(video-auto-editor)
else
  echo "ℹ️  未找到 video-auto-editor 命令，回退使用 ${PY} -m video_auto_editor"
  CLI=("${PY}" -m video_auto_editor)
fi

# 4. 结构化环境预检（复用 video_auto_editor.preflight.run_preflight）
echo
echo "🔎 环境预检..."
E2E_CONFIG="${E2E_CONFIG}" "${PY}" - <<'PY'
import importlib.util
import os
import shutil
import sys

from video_auto_editor.config import CONFIG, merge_config_file
from video_auto_editor.preflight import EnvironmentProbe, run_preflight

# CLI 命令未安装但模块可导入时，脚本会用 `python -m video_auto_editor` 兜底，
# 因此把 video-auto-editor 视为可用，避免预检对兜底路径误报 error。
cli_available = shutil.which("video-auto-editor") is not None or (
    importlib.util.find_spec("video_auto_editor") is not None
)
probe = EnvironmentProbe(
    commands={
        "video-auto-editor": cli_available,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }
)

config = merge_config_file(CONFIG, os.environ["E2E_CONFIG"])
result = run_preflight(probe=probe, config=config)
for check in result.checks:
    mark = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(check.status, "?")
    print(f"   {mark} [{check.name}] {check.detail}")
    if check.status in ("warn", "error") and check.hint:
        print(f"      → {check.hint}")
if not result.ready:
    print("❌ 预检未通过，存在 error，终止跑通。")
    sys.exit(1)
print("✅ 预检通过。")
PY

# 5. 真实完整导出（不加 --dry-run）
echo
echo "🚀 运行 live 拆条（完整导出）..."
LIVE_ARGS=(
  live "${E2E_VIDEO}"
  --config-file "${E2E_CONFIG}"
  --output-dir "${E2E_OUT}"
  --work-dir "${E2E_WORK}"
)
if [[ -n "${E2E_MAX_CLIPS}" ]]; then
  LIVE_ARGS+=(--max-clips "${E2E_MAX_CLIPS}")
fi
if [[ "${E2E_ALLOW_UNREVIEWED_EXPORT}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
  LIVE_ARGS+=(--allow-unreviewed-export)
fi
if [[ -n "${E2E_EXTRA_ARGS}" ]]; then
  # shellcheck disable=SC2206
  EXTRA_ARGS=(${E2E_EXTRA_ARGS})
  LIVE_ARGS+=("${EXTRA_ARGS[@]}")
fi
"${CLI[@]}" "${LIVE_ARGS[@]}"

# 6. 校验标准交付物
echo
echo "🧪 校验标准交付物..."
"${PY}" tests/e2e/verify_live_deliverables.py "${E2E_OUT}"

echo
echo "✅ 全流程端到端跑通完成：交付物位于 ${E2E_OUT}/"
