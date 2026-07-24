"""PROTOTYPE — 语音识别模块契约交互壳。

运行：python3 -m video_auto_editor.prototype_transcription_contract

这是用于回答接口设计问题的 throwaway TUI，不是生产命令。
"""

from __future__ import annotations

import json

from video_auto_editor.prototype_transcription_contract_logic import (
    PrototypeState,
    interface_summary,
    run_selected_scenario,
    select_scenario,
)


BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
CLEAR = "\x1b[2J\x1b[H"


def render(state: PrototypeState) -> None:
    print(CLEAR, end="")
    print(f"{BOLD}PROTOTYPE — 供应商无感知的语音识别模块契约{RESET}")
    print(
        f"{DIM}问题：两动作接口 + 统一结果/失败，是否足以让编排层保持供应商无感知？{RESET}\n"
    )

    print(f"{BOLD}候选接口{RESET}")
    print(json.dumps(interface_summary(), ensure_ascii=False, indent=2))

    scenario = state.scenario
    print(f"\n{BOLD}当前场景{RESET}")
    print(f"  key: {scenario.key}")
    print(f"  title: {scenario.title}")
    print(f"  intent: {scenario.intent}")
    print(f"  phase: {state.phase}")

    print(f"\n{BOLD}编排层观察到的完整状态{RESET}")
    observation = {
        "readiness": state.readiness,
        "result": state.result,
        "failure": state.failure,
        "orchestration_decision": state.orchestration_decision,
    }
    print(json.dumps(observation, ensure_ascii=False, indent=2))

    print(f"\n{BOLD}[r]{RESET} 运行  {BOLD}[n]{RESET} 下一场景  "
          f"{BOLD}[p]{RESET} 上一场景  {BOLD}[q]{RESET} 退出")


def main() -> None:
    state = PrototypeState()
    while True:
        render(state)
        command = input(f"\n{DIM}选择：{RESET}").strip().lower()
        if command == "q":
            return
        if command == "n":
            state = select_scenario(state, 1)
        elif command == "p":
            state = select_scenario(state, -1)
        elif command == "r":
            state = run_selected_scenario(state)


if __name__ == "__main__":
    main()
