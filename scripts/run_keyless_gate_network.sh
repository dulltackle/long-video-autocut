#!/bin/sh
# CI 必须在仅含 lo 的网络命名空间中运行；本地可降级到 Python guard。

set -eu

internal_root_marker=__keyless_gate_root__

drop_privileges_and_exec() {
    target_uid=$1
    target_gid=$2
    shift 2
    exec setpriv \
        --nnp \
        --inh-caps=-all \
        --ambient-caps=-all \
        --bounding-set=-all \
        --reuid "$target_uid" \
        --regid "$target_gid" \
        --clear-groups \
        -- "$@"
}

if [ "${1:-}" = "$internal_root_marker" ]; then
    if [ "$#" -lt 6 ] || [ "$(id -u)" -ne 0 ]; then
        echo "非法的无密钥门禁提权入口" >&2
        exit 2
    fi
    target_uid=$2
    target_gid=$3
    parent_netns=$4
    require_namespace=$5
    shift 5
    case $target_uid in
        ''|*[!0-9]*)
            echo "无密钥门禁调用者身份不合法" >&2
            exit 2
            ;;
    esac
    case $target_gid in
        ''|*[!0-9]*)
            echo "无密钥门禁调用者身份不合法" >&2
            exit 2
            ;;
    esac
    if [ "$target_uid" -eq 0 ]; then
        echo "无密钥门禁拒绝以 root 身份运行候选代码" >&2
        exit 2
    fi
    case $require_namespace in
        0|1) ;;
        *)
            echo "无密钥门禁 namespace 要求不合法" >&2
            exit 2
            ;;
    esac
    case $1 in
        /*) ;;
        *)
            echo "无密钥门禁命令必须是绝对路径" >&2
            exit 2
            ;;
    esac
    if [ ! -x "$1" ] || [ "$(readlink /proc/self/ns/net)" != "$parent_netns" ]; then
        echo "无密钥门禁提权参数验证失败" >&2
        exit 2
    fi

    if command -v unshare >/dev/null 2>&1 \
        && command -v ip >/dev/null 2>&1 \
        && command -v setpriv >/dev/null 2>&1 \
        && unshare --net -- true >/dev/null 2>&1; then
        exec unshare --net -- sh -c '
            set -eu
            uid=$1
            gid=$2
            parent_netns=$3
            shift 3
            ip link set lo up
            export KEYLESS_GATE_NETWORK_MODE=network_namespace
            export KEYLESS_GATE_PARENT_NETNS="$parent_netns"
            exec setpriv \
                --nnp \
                --inh-caps=-all \
                --ambient-caps=-all \
                --bounding-set=-all \
                --reuid "$uid" \
                --regid "$gid" \
                --clear-groups \
                -- "$@"
        ' sh "$target_uid" "$target_gid" "$parent_netns" "$@"
    fi

    if [ "$require_namespace" = "1" ]; then
        echo "当前环境无法建立仅回环网络命名空间" >&2
        exit 1
    fi
    export KEYLESS_GATE_NETWORK_MODE=python_guard
    unset KEYLESS_GATE_PARENT_NETNS
    drop_privileges_and_exec "$target_uid" "$target_gid" "$@"
fi

if [ "$#" -eq 0 ]; then
    echo "用法: run_keyless_gate_network.sh <command> [args...]" >&2
    exit 2
fi

wrapper_path=$(readlink -f "$0")
resolved_command=$(command -v "$1" 2>/dev/null || true)
if [ -z "$wrapper_path" ] || [ -z "$resolved_command" ]; then
    echo "无法解析无密钥门禁入口或命令" >&2
    exit 2
fi
case $resolved_command in
    /*) command_path=$resolved_command ;;
    *) command_path=$(pwd -P)/$resolved_command ;;
esac
if [ ! -x "$command_path" ]; then
    echo "无密钥门禁命令不可执行" >&2
    exit 2
fi
shift
set -- "$command_path" "$@"

caller_uid=$(id -u)
caller_gid=$(id -g)
if [ "$caller_uid" -eq 0 ]; then
    echo "无密钥门禁拒绝以 root 身份运行候选代码" >&2
    exit 2
fi

require_namespace=${KEYLESS_GATE_REQUIRE_NAMESPACE:-0}
if [ "${KEYLESS_GATE_FORCE_PYTHON_GUARD:-0}" = "1" ]; then
    if [ "$require_namespace" = "1" ]; then
        echo "已要求网络命名空间，不能强制使用 Python guard" >&2
        exit 1
    fi
    unset KEYLESS_GATE_NETWORK_MODE KEYLESS_GATE_PARENT_NETNS
    unset KEYLESS_GATE_ORIGINAL_UID KEYLESS_GATE_ORIGINAL_GID
    export KEYLESS_GATE_NETWORK_MODE=python_guard
    exec "$@"
fi

parent_netns=$(readlink /proc/self/ns/net)
unset KEYLESS_GATE_NETWORK_MODE KEYLESS_GATE_PARENT_NETNS
unset KEYLESS_GATE_ORIGINAL_UID KEYLESS_GATE_ORIGINAL_GID

if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    exec sudo -n -E "$wrapper_path" "$internal_root_marker" \
        "$caller_uid" "$caller_gid" "$parent_netns" \
        "$require_namespace" "$@"
fi

if [ "$require_namespace" = "1" ]; then
    echo "当前环境无法建立仅回环网络命名空间" >&2
    exit 1
fi

export KEYLESS_GATE_NETWORK_MODE=python_guard
exec "$@"
