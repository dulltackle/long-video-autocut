from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.live_progress.__main__ import main as live_progress_main
from tools.live_progress.server import create_http_server
from tools.live_progress.tests.test_startup import create_managed_workspace


_RUN_NEWEST = "run_123e4567-e89b-42d3-a456-426614174000"
_RUN_SAME_TIME = "run_123e4567-e89b-42d3-a456-426614174001"
_RUN_WITHOUT_START = "run_123e4567-e89b-42d3-a456-426614174002"
_EVENT_MESSAGES = {
    "run.initialized": "直播拆条运行已初始化。",
    "run.completed": "直播拆条运行已结束。",
    "stage.started": "直播拆条运行阶段已开始。",
    "stage.completed": "直播拆条运行阶段已完成。",
    "environment.observed": "认证环境的脱敏预检事实已记录。",
    "external_service.selected": "供应商外发计划的脱敏事实已记录。",
    "source.observed": "素材的脱敏来源事实已记录。",
}


def _timestamp(offset_ms: int) -> str:
    value = datetime(2026, 8, 10, 19, 24, 31, 123000, tzinfo=timezone.utc)
    value += timedelta(milliseconds=offset_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event(
    run_id: str,
    sequence: int,
    code: str,
    *,
    stage: str,
    module: str,
    attributes: dict[str, object],
    offset_ms: int,
    level: str = "info",
) -> dict[str, object]:
    return {
        "schema_version": "run_event.v1",
        "timestamp": _timestamp(offset_ms),
        "sequence": sequence,
        "run_id": run_id,
        "level": level,
        "event_code": code,
        "stage": stage,
        "module": module,
        "message": _EVENT_MESSAGES[code],
        "attributes": attributes,
    }


def _write_events(workspace: Path, run_id: str, events: list[dict[str, object]]) -> None:
    run_directory = workspace / "work/runs" / run_id
    run_directory.mkdir(mode=0o700)
    encoded = b"".join(
        json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for event in events
    )
    (run_directory / "events.jsonl").write_bytes(encoded)


def _initialized(run_id: str, *, offset_ms: int = 0) -> dict[str, object]:
    return _event(
        run_id,
        1,
        "run.initialized",
        stage="initialized",
        module="application",
        attributes={
            "application_version": "4.7.0",
            "release": {"status": "unknown"},
        },
        offset_ms=offset_ms,
    )


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

    def test_discovers_sorts_and_projects_trusted_initial_stages(self) -> None:
        events = [
            _initialized(_RUN_NEWEST),
            _event(
                _RUN_NEWEST,
                2,
                "stage.started",
                stage="preflight",
                module="application",
                attributes={},
                offset_ms=100,
            ),
            _event(
                _RUN_NEWEST,
                3,
                "environment.observed",
                stage="preflight",
                module="readiness",
                attributes={
                    "certified_platform": "ubuntu_24_04_amd64",
                    "python_version": "3.12.3",
                    "application_version": "4.7.0",
                    "ffmpeg_version": "6.1.1",
                    "ffprobe_version": "6.1.1",
                    "font": {"family": "Noto Sans CJK SC", "available": True},
                    "installation_fingerprint": "sha256:" + "a" * 64,
                    "preflight_outcome": "succeeded",
                },
                offset_ms=200,
            ),
        ]
        for capability, module, adapter, provider, model in (
            ("transcription", "transcription", "stepaudio_https", "stepaudio", "step-audio-2"),
            ("topic_review", "topic_review", "stepfun_chat", "stepfun", "step-3.5-flash"),
            (
                "subtitle_optimization",
                "subtitle_optimization",
                "stepfun_chat",
                "stepfun",
                "step-3.5-flash",
            ),
        ):
            events.append(
                _event(
                    _RUN_NEWEST,
                    len(events) + 1,
                    "external_service.selected",
                    stage="preflight",
                    module=module,
                    attributes={
                        "capability": capability,
                        "adapter_id": adapter,
                        "provider_id": provider,
                        "model_id": model,
                        "configuration_fingerprint": "sha256:" + "b" * 64,
                        "endpoint": {"status": "not_applicable"},
                        "allowed_data_categories": [],
                        "transport": "local",
                    },
                    offset_ms=200 + len(events),
                )
            )
        events.extend(
            [
                _event(
                    _RUN_NEWEST,
                    len(events) + 1,
                    "stage.completed",
                    stage="preflight",
                    module="application",
                    attributes={
                        "duration_ms": 1200,
                        "outcome": "succeeded",
                        "work_item_count": 4,
                    },
                    offset_ms=1300,
                ),
                _event(
                    _RUN_NEWEST,
                    len(events) + 2,
                    "stage.started",
                    stage="source_analysis",
                    module="application",
                    attributes={},
                    offset_ms=1400,
                ),
                _event(
                    _RUN_NEWEST,
                    len(events) + 3,
                    "source.observed",
                    stage="source_analysis",
                    module="source_analysis",
                    attributes={
                        "byte_length": 123_456,
                        "duration_ms": 98_765,
                        "course_context_provided": True,
                    },
                    offset_ms=1500,
                ),
                _event(
                    _RUN_NEWEST,
                    len(events) + 4,
                    "stage.completed",
                    stage="source_analysis",
                    module="application",
                    attributes={
                        "duration_ms": 400,
                        "outcome": "succeeded",
                        "work_item_count": 1,
                    },
                    offset_ms=1800,
                ),
            ]
        )
        _write_events(self.workspace, _RUN_NEWEST, events)
        _write_events(self.workspace, _RUN_SAME_TIME, [_initialized(_RUN_SAME_TIME)])
        (self.workspace / "work/runs" / _RUN_WITHOUT_START).mkdir(mode=0o700)

        with self.request("/api/v1/runs") as response:
            index = json.load(response)

        self.assertEqual(
            [item["run_id"] for item in index["runs"]],
            [_RUN_NEWEST, _RUN_SAME_TIME, _RUN_WITHOUT_START],
        )
        item = index["runs"][0]
        self.assertEqual(item["observation_state"], "unclosed")
        self.assertEqual(item["started_at"], {"status": "available", "value": _timestamp(0)})
        self.assertEqual(item["stage"], {"status": "available", "value": "source_analysis"})
        self.assertEqual(item["duration_ms"], {"status": "not_observed"})
        self.assertEqual(item["last_event_at"], {"status": "available", "value": _timestamp(1800)})

        with self.request(f"/api/v1/runs/{_RUN_NEWEST}") as response:
            raw_snapshot = response.read()
            snapshot = json.loads(raw_snapshot)
            snapshot_etag = response.headers["ETag"]

        self.assertEqual(item["snapshot_etag"], snapshot_etag)
        self.assertEqual(
            [stage["stage"] for stage in snapshot["stages"]],
            [
                "initialized",
                "preflight",
                "source_analysis",
                "transcription",
                "candidate_planning",
                "topic_review",
                "delivery_build",
                "delivery_verification",
                "publishing",
            ],
        )
        initialized, preflight, source = snapshot["stages"][:3]
        self.assertEqual(
            initialized,
            {
                "stage": "initialized",
                "lifecycle": {"status": "succeeded", "observed_at": _timestamp(0)},
                "result": {
                    "application_version": {"status": "available", "value": "4.7.0"}
                },
                "progress": {},
                "error_ids": [],
            },
        )
        self.assertEqual(preflight["lifecycle"]["status"], "succeeded")
        self.assertEqual(
            preflight["result"],
            {
                "certified_platform": {"status": "available", "value": "ubuntu_24_04_amd64"},
                "tool_versions": {
                    "python": {"status": "available", "value": "3.12.3"},
                    "ffmpeg": {"status": "available", "value": "6.1.1"},
                    "ffprobe": {"status": "available", "value": "6.1.1"},
                },
                "font": {
                    "status": "available",
                    "value": {"family": "Noto Sans CJK SC", "available": True},
                },
                "provider_selections": {
                    "transcription": {
                        "status": "available",
                        "value": {
                            "adapter_id": "stepaudio_https",
                            "provider_id": "stepaudio",
                            "model_id": "step-audio-2",
                        },
                    },
                    "topic_review": {
                        "status": "available",
                        "value": {
                            "adapter_id": "stepfun_chat",
                            "provider_id": "stepfun",
                            "model_id": "step-3.5-flash",
                        },
                    },
                    "subtitle_optimization": {
                        "status": "available",
                        "value": {
                            "adapter_id": "stepfun_chat",
                            "provider_id": "stepfun",
                            "model_id": "step-3.5-flash",
                        },
                    },
                },
            },
        )
        self.assertEqual(
            preflight["progress"],
            {
                "configuration_observed": {"status": "not_observed"},
                "environment_outcome": {"status": "available", "value": "succeeded"},
            },
        )
        self.assertEqual(
            source["result"],
            {
                "byte_length": {"status": "available", "value": 123_456},
                "duration_ms": {"status": "available", "value": 98_765},
                "course_context_provided": {"status": "available", "value": True},
            },
        )
        self.assertNotIn(str(self.workspace), raw_snapshot.decode("utf-8"))
        self.assertNotIn("installation_fingerprint", raw_snapshot.decode("utf-8"))
        self.assertNotIn("configuration_fingerprint", raw_snapshot.decode("utf-8"))
        for forbidden in (
            '"event_code"',
            '"sequence"',
            '"module"',
            '"message"',
            '"endpoint"',
            '"process"',
            '"process_alive"',
            '"pid"',
        ):
            self.assertNotIn(forbidden, raw_snapshot.decode("utf-8"))

    def test_terminal_tail_corruption_and_detail_conditionals_are_safe(self) -> None:
        succeeded = [
            _initialized(_RUN_NEWEST),
            _event(
                _RUN_NEWEST,
                2,
                "stage.started",
                stage="preflight",
                module="application",
                attributes={},
                offset_ms=100,
            ),
            _event(
                _RUN_NEWEST,
                3,
                "stage.completed",
                stage="preflight",
                module="application",
                attributes={"duration_ms": 500, "outcome": "succeeded", "work_item_count": 0},
                offset_ms=600,
            ),
            _event(
                _RUN_NEWEST,
                4,
                "run.completed",
                stage="preflight",
                module="application",
                attributes={"duration_ms": 700, "exit_code": 0, "outcome": "succeeded", "result_kind": {"status": "available", "value": "clips"}},
                offset_ms=700,
            ),
        ]
        _write_events(self.workspace, _RUN_NEWEST, succeeded)
        _write_events(self.workspace, _RUN_SAME_TIME, [_initialized(_RUN_SAME_TIME)])
        tail = self.workspace / "work/runs" / _RUN_SAME_TIME / "events.jsonl"
        with tail.open("ab") as stream:
            stream.write(b'{"schema_version":"run_event.v1"')
        _write_events(self.workspace, _RUN_WITHOUT_START, [_initialized(_RUN_WITHOUT_START)])
        corrupt = self.workspace / "work/runs" / _RUN_WITHOUT_START / "events.jsonl"
        with corrupt.open("ab") as stream:
            stream.write(b'{"secret_exception":"/private/path","broken":}\n')

        with self.request(f"/api/v1/runs/{_RUN_NEWEST}") as response:
            snapshot = json.load(response)
            etag = response.headers["ETag"]
        self.assertEqual(snapshot["observation"]["state"], "succeeded")
        self.assertEqual(snapshot["terminal"]["value"]["evidence"], "terminal_event")
        self.assertEqual(snapshot["terminal"]["value"]["result_kind"], {"status": "available", "value": "clips"})

        with self.assertRaises(urllib.error.HTTPError) as not_modified:
            self.request(
                f"/api/v1/runs/{_RUN_NEWEST}",
                headers={"If-None-Match": etag},
            )
        self.assertEqual(not_modified.exception.code, 304)
        self.assertEqual(not_modified.exception.read(), b"")

        with self.request(f"/api/v1/runs/{_RUN_SAME_TIME}") as response:
            tail_snapshot = json.load(response)
        self.assertEqual(
            tail_snapshot["diagnostics"],
            {
                "state": "incomplete",
                "reason": "event_tail_incomplete",
                "verified_event_count": 1,
            },
        )

        with self.request(f"/api/v1/runs/{_RUN_WITHOUT_START}") as response:
            corrupt_body = response.read()
            corrupt_snapshot = json.loads(corrupt_body)
        self.assertEqual(corrupt_snapshot["observation"]["state"], "record_corrupt")
        self.assertEqual(corrupt_snapshot["diagnostics"]["reason"], "event_json_invalid")
        self.assertNotIn(b"secret_exception", corrupt_body)
        self.assertNotIn(b"private", corrupt_body)

        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.request("/api/v1/runs/not-a-run-secret-path")
        self.assertEqual(invalid.exception.code, 400)
        invalid_body = invalid.exception.read()
        self.assertNotIn(b"secret-path", invalid_body)

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

    def test_projection_failures_use_the_safe_json_error_envelope(self) -> None:
        _write_events(self.workspace, _RUN_NEWEST, [_initialized(_RUN_NEWEST)])

        with mock.patch(
            "tools.live_progress.server.http.project_run",
            side_effect=ValueError("secret_exception: /private/workspace"),
        ):
            with self.assertRaises(urllib.error.HTTPError) as captured:
                self.request(f"/api/v1/runs/{_RUN_NEWEST}")

        self.assertEqual(captured.exception.code, 500)
        body = captured.exception.read()
        self.assertEqual(
            json.loads(body),
            {
                "schema_version": "live_progress.v1",
                "error": {
                    "code": "internal_error",
                    "message": "工作区安全状态已变化，无法继续读取",
                },
            },
        )
        self.assertNotIn(b"secret_exception", body)
        self.assertNotIn(b"private", body)

    def test_replaced_runs_parent_cannot_escape_the_workspace(self) -> None:
        escaped = Path(self.temporary.name) / "escaped-runs"
        escaped.mkdir()
        _write_events(self.workspace, _RUN_NEWEST, [_initialized(_RUN_NEWEST)])
        original_runs = self.workspace / "work/runs"
        moved_runs = self.workspace / "work/original-runs"
        original_runs.rename(moved_runs)
        original_runs.symlink_to(escaped, target_is_directory=True)

        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request("/api/v1/runs")

        self.assertEqual(captured.exception.code, 500)
        payload = json.loads(captured.exception.read())
        self.assertEqual(payload["schema_version"], "live_progress.v1")
        self.assertEqual(payload["error"]["code"], "internal_error")


class ModuleEntryHttpContractTests(unittest.TestCase):
    def test_formal_module_entry_serves_discovered_runs_over_real_http(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = create_managed_workspace(root)
            _write_events(workspace, _RUN_NEWEST, [_initialized(_RUN_NEWEST)])
            static_root = root / "dist"
            static_root.mkdir()
            (static_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
            created = threading.Event()
            servers = []

            def capture_server(*args: object, **kwargs: object):
                kwargs["port"] = 0
                server = create_http_server(*args, **kwargs)
                servers.append(server)
                created.set()
                return server

            with (
                mock.patch(
                    "tools.live_progress.__main__.ensure_web_build",
                    return_value=static_root,
                ),
                mock.patch(
                    "tools.live_progress.__main__.create_http_server",
                    side_effect=capture_server,
                ),
            ):
                thread = threading.Thread(
                    target=live_progress_main,
                    args=((str(workspace), "--port", "4173"),),
                    daemon=True,
                )
                thread.start()
                self.assertTrue(created.wait(timeout=2))
                try:
                    port = servers[0].server_port
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/v1/runs", timeout=2
                    ) as response:
                        payload = json.load(response)
                    self.assertEqual(payload["runs"][0]["run_id"], _RUN_NEWEST)
                finally:
                    servers[0].shutdown()
                    thread.join(timeout=2)
                    servers[0].server_close()


if __name__ == "__main__":
    unittest.main()
