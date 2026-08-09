#!/bin/sh
# CI 必须在仅含 lo 的网络命名空间中运行；本地可降级到 Python guard。

set -eu

internal_root_marker=__keyless_gate_root__
id_command=/usr/bin/id
readlink_command=/usr/bin/readlink
unshare_command=/usr/bin/unshare
ip_command=/usr/sbin/ip
setpriv_command=/usr/bin/setpriv
sudo_command=/usr/bin/sudo
shell_command=/bin/sh

drop_privileges_and_exec() {
    target_uid=$1
    target_gid=$2
    shift 2
    exec "$setpriv_command" \
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
    if [ "$#" -lt 6 ] || [ "$("$id_command" -u)" -ne 0 ]; then
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
    if [ ! -x "$1" ] || [ "$("$readlink_command" /proc/self/ns/net)" != "$parent_netns" ]; then
        echo "无密钥门禁提权参数验证失败" >&2
        exit 2
    fi

    if [ -x "$unshare_command" ] \
        && [ -x "$ip_command" ] \
        && [ -x "$setpriv_command" ] \
        && "$unshare_command" --net -- /usr/bin/true >/dev/null 2>&1; then
        exec "$unshare_command" --net -- "$shell_command" -c '
            set -eu
            uid=$1
            gid=$2
            parent_netns=$3
            shift 3
            /usr/sbin/ip link set lo up
            attestation_fd=${RELEASE_GATE_NETWORK_ATTESTATION_FD:-}
            if [ -n "$attestation_fd" ]; then
                if ! printf "%s\n" "$attestation_fd" \
                    | LC_ALL=C /usr/bin/grep -qxE '[3-9]|[1-9][0-9]+'; then
                    echo "网络命名空间证明文件描述符不合法" >&2
                    exit 2
                fi
                descriptor_target=$(/usr/bin/readlink \
                    "/proc/self/fd/$attestation_fd" 2>/dev/null || true)
                case $descriptor_target in
                    pipe:\[*\]) ;;
                    *)
                        echo "网络命名空间证明通道不合法" >&2
                        exit 2
                        ;;
                esac
                current_netns=$(/usr/bin/readlink /proc/self/ns/net)
                interface_count=$(/usr/sbin/ip -o link show up \
                    | /usr/bin/wc -l)
                if [ "$current_netns" = "$parent_netns" ] \
                    || [ "$interface_count" -ne 1 ] \
                    || ! /usr/sbin/ip -o link show up \
                        | /usr/bin/grep -Eq "^[0-9]+: lo:"; then
                    echo "网络命名空间运行证明无效" >&2
                    exit 1
                fi
                printf "release_gate_network.v1\t%s\t%s\tlo\n" \
                    "$parent_netns" "$current_netns" >&"$attestation_fd"
                eval "exec $attestation_fd>&-"
            fi
            unset RELEASE_GATE_NETWORK_ATTESTATION_FD
            unset RELEASE_GATE_SYSTEMD_HOST_NETNS
            export KEYLESS_GATE_NETWORK_MODE=network_namespace
            export KEYLESS_GATE_PARENT_NETNS="$parent_netns"
            exec /usr/bin/setpriv \
                --nnp \
                --inh-caps=-all \
                --ambient-caps=-all \
                --bounding-set=-all \
                --reuid "$uid" \
                --regid "$gid" \
                --clear-groups \
                -- "$@"
        ' /bin/sh "$target_uid" "$target_gid" "$parent_netns" "$@"
    fi

    if [ "$require_namespace" = "1" ]; then
        echo "当前环境无法建立仅回环网络命名空间" >&2
        exit 1
    fi
    unset RELEASE_GATE_NETWORK_ATTESTATION_FD
    unset RELEASE_GATE_SYSTEMD_HOST_NETNS
    export KEYLESS_GATE_NETWORK_MODE=python_guard
    unset KEYLESS_GATE_PARENT_NETNS
    drop_privileges_and_exec "$target_uid" "$target_gid" "$@"
fi

if [ "$#" -eq 0 ]; then
    echo "用法: run_keyless_gate_network.sh <command> [args...]" >&2
    exit 2
fi

wrapper_path=$("$readlink_command" -f "$0")
case $1 in
    /*) command_path=$1 ;;
    *)
        echo "无密钥门禁命令必须是绝对路径" >&2
        exit 2
        ;;
esac
if [ -z "$wrapper_path" ] || [ ! -x "$command_path" ]; then
    echo "无法解析无密钥门禁入口或命令" >&2
    exit 2
fi
shift
set -- "$command_path" "$@"

caller_uid=$("$id_command" -u)
caller_gid=$("$id_command" -g)
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
    unset RELEASE_GATE_NETWORK_ATTESTATION_FD
    unset RELEASE_GATE_SYSTEMD_HOST_NETNS
    export KEYLESS_GATE_NETWORK_MODE=python_guard
    exec "$@"
fi

parent_netns=$("$readlink_command" /proc/self/ns/net)
unset KEYLESS_GATE_NETWORK_MODE KEYLESS_GATE_PARENT_NETNS
unset KEYLESS_GATE_ORIGINAL_UID KEYLESS_GATE_ORIGINAL_GID

attestation_fd=${RELEASE_GATE_NETWORK_ATTESTATION_FD:-}
if [ -n "$attestation_fd" ]; then
    if ! printf '%s\n' "$attestation_fd" \
        | LC_ALL=C /usr/bin/grep -qxE '[3-9]|[1-9][0-9]+'; then
        echo "网络命名空间证明文件描述符不合法" >&2
        exit 2
    fi
    descriptor_target=$("$readlink_command" "/proc/self/fd/$attestation_fd" 2>/dev/null || true)
    case $descriptor_target in
        pipe:\[*\]) ;;
        *)
            echo "网络命名空间证明通道不合法" >&2
            exit 2
            ;;
    esac
    host_netns=${RELEASE_GATE_SYSTEMD_HOST_NETNS:-}
    if ! printf "%s\n" "$host_netns" \
        | /usr/bin/grep -qxE "net:\[[0-9]+\]"; then
        echo "systemd 主机网络命名空间证明无效" >&2
        exit 2
    fi
    interface_count=$("$ip_command" -o link show up | /usr/bin/wc -l)
    if [ "$parent_netns" = "$host_netns" ] \
        || [ "$interface_count" -ne 1 ] \
        || ! "$ip_command" -o link show up \
            | /usr/bin/grep -Eq "^[0-9]+: lo:"; then
        echo "当前进程不在 systemd 仅回环网络命名空间" >&2
        exit 1
    fi
    printf "release_gate_network.v1\t%s\t%s\tlo\n" \
        "$host_netns" "$parent_netns" >&"$attestation_fd"
    eval "exec $attestation_fd>&-"
    unset RELEASE_GATE_NETWORK_ATTESTATION_FD RELEASE_GATE_SYSTEMD_HOST_NETNS
    export KEYLESS_GATE_NETWORK_MODE=network_namespace
    export KEYLESS_GATE_PARENT_NETNS="$host_netns"
    exec "$@"
fi
unset RELEASE_GATE_NETWORK_ATTESTATION_FD
unset RELEASE_GATE_SYSTEMD_HOST_NETNS

# 命令所需状态均已转为位置参数；拒绝跨 sudo 保留调用者环境。
if [ -x "$sudo_command" ] && "$sudo_command" -n /usr/bin/true >/dev/null 2>&1; then
    exec "$sudo_command" -n "$wrapper_path" "$internal_root_marker" \
        "$caller_uid" "$caller_gid" "$parent_netns" \
        "$require_namespace" "$@"
fi

if [ "$require_namespace" = "1" ]; then
    echo "当前环境无法建立仅回环网络命名空间" >&2
    exit 1
fi

export KEYLESS_GATE_NETWORK_MODE=python_guard
exec "$@"
