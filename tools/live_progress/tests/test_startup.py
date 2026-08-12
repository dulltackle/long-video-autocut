from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.live_progress.startup import StartupError, validate_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def create_managed_workspace(parent: Path) -> Path:
    root = parent / "course.autocut"
    for relative in (
        ".",
        "delivery",
        "delivery.previous",
        "work",
        "work/cache",
        "work/runs",
        "work/tmp",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / ".video-auto-editor-workspace.json").write_text(
        '{"schema_version":"workspace.v1"}\n', encoding="utf-8"
    )
    (root / "work/.workspace.lock").touch(mode=0o600)
    return root


class StartupContractTests(unittest.TestCase):
    def test_module_entry_requires_exactly_one_workspace_position(self) -> None:
        completed = subprocess.run(
            (sys.executable, "-m", "tools.live_progress"),
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("WORKSPACE", completed.stderr)

    def test_accepts_an_existing_owned_managed_autocut_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = create_managed_workspace(Path(temporary))

            validated = validate_workspace(root)

        self.assertEqual(validated, root.resolve())
        self.assertTrue(validated.is_absolute())

    def test_rejects_a_workspace_path_that_is_a_symbolic_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = create_managed_workspace(parent)
            link = parent / "linked.autocut"
            link.symlink_to(root, target_is_directory=True)

            with self.assertRaisesRegex(StartupError, "符号链接"):
                validate_workspace(link)

    def test_rejects_an_unmanaged_or_wrongly_named_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            unmanaged = parent / "foreign.autocut"
            unmanaged.mkdir()
            managed_wrong_name = create_managed_workspace(parent)
            renamed = parent / "course.workspace"
            managed_wrong_name.rename(renamed)

            with self.assertRaisesRegex(StartupError, "受管"):
                validate_workspace(unmanaged)
            with self.assertRaisesRegex(StartupError, r"\.autocut"):
                validate_workspace(renamed)

    def test_rejects_non_regular_or_linked_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = create_managed_workspace(parent)
            marker = root / ".video-auto-editor-workspace.json"
            marker.unlink()
            marker.mkdir()

            with self.assertRaisesRegex(StartupError, "普通文件"):
                validate_workspace(root)

            marker.rmdir()
            outside = parent / "outside.json"
            outside.write_text(json.dumps({"schema_version": "workspace.v1"}))
            marker.symlink_to(outside)
            with self.assertRaisesRegex(StartupError, "符号链接"):
                validate_workspace(root)

    @unittest.skipUnless(hasattr(os, "getuid"), "仅 POSIX 支持所有权契约")
    def test_rejects_a_managed_workspace_with_a_different_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = create_managed_workspace(Path(temporary))
            foreign_uid = os.getuid() + 1
            with mock.patch(
                "tools.live_progress.startup.os.getuid",
                return_value=foreign_uid,
            ):
                with self.assertRaisesRegex(StartupError, "所有权"):
                    validate_workspace(root)


if __name__ == "__main__":
    unittest.main()
