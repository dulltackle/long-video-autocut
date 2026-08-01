#!/usr/bin/env python3
"""以显式测试依赖调用生产安装主函数。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = PROJECT_ROOT / "scripts" / "install-production.sh"


def main() -> None:
    os_release = os.environ.get(
        "INSTALL_PRODUCTION_TEST_OS_RELEASE_FILE",
        "/etc/os-release",
    )
    os.execv(
        "/bin/bash",
        (
            "bash",
            "-c",
            'source "$1"; shift; install_production_main "$1" 0 "$2" "${@:3}"',
            "production-installer-test",
            str(INSTALLER),
            os_release,
            str(os.geteuid()),
            *sys.argv[1:],
        ),
    )


if __name__ == "__main__":
    main()
