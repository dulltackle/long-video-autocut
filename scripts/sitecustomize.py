"""仅在无密钥门禁设置审计文件时自动启用 socket guard。"""

import os

if os.environ.get("KEYLESS_GATE_NETWORK_AUDIT"):
    from keyless_gate_network_guard import install

    install()
