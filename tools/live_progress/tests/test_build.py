from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.live_progress.build import BuildError, ensure_web_build


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...], cwd: Path) -> str:
        self.commands.append(command)
        if command == ("node", "--version"):
            return "v24.7.0\n"
        if command == ("npm", "--version"):
            return "11.19.0\n"
        if command == ("npm", "run", "build"):
            (cwd / "dist").mkdir(exist_ok=True)
            (cwd / "dist/index.html").write_text("built", encoding="utf-8")
            return ""
        raise AssertionError(command)


def create_web_project(root: Path, *, dependencies: bool = True) -> Path:
    web = root / "web"
    (web / "src").mkdir(parents=True)
    (web / "public").mkdir()
    for name, content in {
        "package.json": '{"name":"live-progress"}\n',
        "package-lock.json": '{"lockfileVersion":3}\n',
        "vite.config.ts": "export default {}\n",
        "index.html": "<main></main>\n",
        "src/main.tsx": "export {}\n",
        "public/favicon.svg": "<svg/>\n",
    }.items():
        (web / name).write_text(content, encoding="utf-8")
    if dependencies:
        (web / "node_modules").mkdir()
    return web


class BuildContractTests(unittest.TestCase):
    def test_missing_dependencies_only_instructs_the_developer_to_run_npm_ci(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            web = create_web_project(Path(temporary), dependencies=False)
            runner = RecordingRunner()

            with self.assertRaisesRegex(BuildError, r"npm ci"):
                ensure_web_build(web, run=runner)

        self.assertEqual(runner.commands, [])

    def test_builds_once_then_reuses_the_content_identical_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            web = create_web_project(Path(temporary))
            runner = RecordingRunner()

            first = ensure_web_build(web, run=runner)
            second = ensure_web_build(web, run=runner)

        self.assertEqual(first, web / "dist")
        self.assertEqual(second, first)
        self.assertEqual(runner.commands.count(("npm", "run", "build")), 1)
        self.assertFalse((first / ".live-progress-build.json").exists())

    def test_source_change_invalidates_the_build_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            web = create_web_project(Path(temporary))
            runner = RecordingRunner()
            ensure_web_build(web, run=runner)
            (web / "src/main.tsx").write_text("export const changed = true\n")

            ensure_web_build(web, run=runner)

        self.assertEqual(runner.commands.count(("npm", "run", "build")), 2)

    def test_rejects_a_node_or_npm_major_outside_the_fixed_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            web = create_web_project(Path(temporary))

            def wrong_node(command: tuple[str, ...], cwd: Path) -> str:
                del cwd
                return "v22.0.0" if command[0] == "node" else "10.0.0"

            with self.assertRaisesRegex(BuildError, "Node 24"):
                ensure_web_build(web, run=wrong_node)


if __name__ == "__main__":
    unittest.main()
