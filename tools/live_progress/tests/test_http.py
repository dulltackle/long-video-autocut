from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tools.live_progress.server import create_http_server
from tools.live_progress.tests.test_startup import create_managed_workspace


class HttpContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = create_managed_workspace(root)
        self.static_root = root / "dist"
        self.static_root.mkdir()
        (self.static_root / "index.html").write_text(
            "<!doctype html><title>直播拆条 · 阶段控制台</title>"
            "<main>还没有直播拆条运行</main>",
            encoding="utf-8",
        )
        self.server = create_http_server(
            self.workspace, self.static_root, host="127.0.0.1", port=0
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(
        self, path: str, *, method: str = "GET", headers: dict[str, str] | None = None
    ) -> urllib.response.addinfourl:
        return urllib.request.urlopen(
            urllib.request.Request(
                self.origin + path, method=method, headers=headers or {}
            ),
            timeout=2,
        )

    def test_empty_workspace_serves_deterministic_run_index_and_strong_etag(
        self,
    ) -> None:
        with self.request("/api/v1/runs") as response:
            body = response.read()
            etag = response.headers["ETag"]

        self.assertEqual(
            json.loads(body),
            {"schema_version": "live_progress.v1", "runs": []},
        )
        self.assertEqual(
            response.headers["Content-Type"], "application/json; charset=utf-8"
        )
        self.assertRegex(etag, r'^"sha256:[0-9a-f]{64}"$')

        request = urllib.request.Request(
            self.origin + "/api/v1/runs", headers={"If-None-Match": etag}
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(captured.exception.code, 304)
        self.assertEqual(captured.exception.read(), b"")

    def test_same_origin_serves_the_frontend_empty_state(self) -> None:
        with self.request("/") as response:
            body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("还没有直播拆条运行", body)

    def test_unknown_runs_methods_and_escaped_static_paths_are_rejected(self) -> None:
        cases = (
            ("/api/v1/runs/run_123e4567-e89b-42d3-a456-426614174000", "GET", 404),
            ("/api/v1/runs", "POST", 405),
            ("/%2e%2e/pyproject.toml", "GET", 404),
            ("/.live-progress-build.json", "GET", 404),
        )
        for path, method, expected in cases:
            with self.subTest(path=path, method=method):
                with self.assertRaises(urllib.error.HTTPError) as captured:
                    self.request(path, method=method)
                self.assertEqual(captured.exception.code, expected)
                if path.startswith("/api/"):
                    payload = json.loads(captured.exception.read())
                    self.assertEqual(payload["schema_version"], "live_progress.v1")
                    self.assertEqual(set(payload), {"schema_version", "error"})


if __name__ == "__main__":
    unittest.main()
