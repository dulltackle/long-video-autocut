"""仅在无密钥门禁设置审计文件时自动启用 socket guard。"""

import os

if os.environ.get("KEYLESS_GATE_NETWORK_AUDIT"):
    from keyless_gate_network_guard import install

    install()

_scenario = os.environ.get("INSTALLED_ACCEPTANCE_SCENARIO")
if _scenario:
    from installed_acceptance_composition import (
        compose_installed_acceptance_application,
    )
    from video_auto_editor import cli as _candidate_cli

    def _compose_acceptance(*, disclosure_sink=None):
        return compose_installed_acceptance_application(
            scenario=_scenario,
            disclosure_sink=disclosure_sink,
        )

    _candidate_cli.compose_live_application = _compose_acceptance
