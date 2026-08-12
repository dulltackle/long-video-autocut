import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("空工作区显示九阶段轨道和明确空态", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ schema_version: "live_progress.v1", runs: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    ),
  );

  render(<App />);

  expect(await screen.findByRole("heading", { name: "还没有直播拆条运行" })).toBeVisible();
  for (const stage of [
    "初始化",
    "预检",
    "素材分析",
    "语音识别",
    "候选规划",
    "主题评审",
    "交付构建",
    "交付验证",
    "发布",
  ]) {
    expect(screen.getByText(stage)).toBeVisible();
  }
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.getByText(/此界面仅观察工作区/)).toBeVisible();
});

test("接口失败时不把未知状态伪装成空工作区", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 500 })));

  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取工作区");
  expect(screen.queryByText("还没有直播拆条运行")).not.toBeInTheDocument();
});

const runId = "run_123e4567-e89b-42d3-a456-426614174000";
const available = <T,>(value: T) => ({ status: "available" as const, value });
const notObserved = { status: "not_observed" as const };
const notApplicable = { status: "not_applicable" as const };
const requestProgress = {
  completed_request_count: notApplicable,
  inflight_request_count: notApplicable,
  transport_retry_count: notApplicable,
  latest_retry: notApplicable,
};

function stage(
  code: string,
  lifecycle: Record<string, unknown> = { status: "not_started" },
  result: Record<string, unknown> = {},
  progress: Record<string, unknown> = {},
) {
  return { stage: code, lifecycle, result, progress, error_ids: [] };
}

const snapshot = {
  schema_version: "live_progress.v1",
  run_id: runId,
  observation: {
    state: "unclosed",
    stage: available("source_analysis"),
    started_at: available("2026-08-10T19:24:31.123Z"),
    ended_at: notObserved,
    duration_ms: notObserved,
    last_event_at: available("2026-08-10T19:24:32.923Z"),
  },
  diagnostics: { state: "incomplete", reason: "terminal_event_missing", verified_event_count: 10 },
  terminal: notObserved,
  stages: [
    stage(
      "initialized",
      { status: "succeeded", observed_at: "2026-08-10T19:24:31.123Z" },
      { application_version: available("4.7.0"), ignored_future_member: "ok" },
    ),
    stage(
      "preflight",
      {
        status: "succeeded",
        started_at: "2026-08-10T19:24:31.223Z",
        ended_at: "2026-08-10T19:24:32.423Z",
        duration_ms: 1200,
      },
      {
        certified_platform: available("ubuntu_24_04_amd64"),
        tool_versions: {
          python: available("3.12.3"),
          ffmpeg: available("6.1.1"),
          ffprobe: available("6.1.1"),
        },
        font: available({ family: "Noto Sans CJK SC", available: true }),
        provider_selections: {
          transcription: available({ adapter_id: "stepaudio_https", provider_id: "stepaudio", model_id: "step-audio-2" }),
          topic_review: available({ adapter_id: "stepfun_chat", provider_id: "stepfun", model_id: "step-3.5-flash" }),
          subtitle_optimization: available({ adapter_id: "stepfun_chat", provider_id: "stepfun", model_id: "step-3.5-flash" }),
        },
      },
      { configuration_observed: notObserved, environment_outcome: available("succeeded") },
    ),
    stage(
      "source_analysis",
      {
        status: "succeeded",
        started_at: "2026-08-10T19:24:32.523Z",
        ended_at: "2026-08-10T19:24:32.923Z",
        duration_ms: 400,
      },
      {
        byte_length: available(123456),
        duration_ms: available(98765),
        course_context_provided: available(true),
      },
    ),
    stage("transcription", undefined, {
      transcript_chunk_count: notApplicable,
      coverage_recovery_count: notApplicable,
      transcript_cache: notApplicable,
    }, requestProgress),
    stage("candidate_planning", undefined, { candidate_count: notApplicable }),
    stage("topic_review", undefined, { reviewed_candidate_count: notApplicable, cache_observation: notApplicable }, requestProgress),
    stage("delivery_build", undefined, {
      short_video_count: notApplicable,
      created_artifacts_by_role: notApplicable,
      subtitle_cache_observation: notApplicable,
    }, { ...requestProgress, delivery_state: notApplicable }),
    stage("delivery_verification", undefined, {
      verified_item_count: notApplicable,
      verified_short_video_count: notApplicable,
      verified_items_by_role: notApplicable,
    }, { delivery_state: notApplicable }),
    stage("publishing", undefined, { publication_fact: notApplicable }, { delivery_state: notApplicable }),
  ],
  errors: {
    by_id: {},
    primary_error_id: notObserved,
    associated_error_ids: notObserved,
    recovery_incomplete: notObserved,
  },
  delivery: { status: "not_observed", short_video_count: notObserved, preview: { status: "not_ready" } },
  ignored_future_member: { nested: true },
};

test("已发现运行可打开服务端语义投影并保持九阶段顺序", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/runs") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              schema_version: "live_progress.v1",
              runs: [
                {
                  run_id: runId,
                  observation_state: "unclosed",
                  started_at: available("2026-08-10T19:24:31.123Z"),
                  stage: available("source_analysis"),
                  duration_ms: notObserved,
                  last_event_at: available("2026-08-10T19:24:32.923Z"),
                  diagnostics: snapshot.diagnostics,
                  snapshot_etag: '"sha256:' + "a".repeat(64) + '"',
                  ignored_future_member: true,
                },
              ],
              ignored_future_member: true,
            }),
            { status: 200, headers: { ETag: '"index"' } },
          ),
        );
      }
      return Promise.resolve(new Response(JSON.stringify(snapshot), { status: 200 }));
    }),
  );

  render(<App />);

  expect(await screen.findByText("123,456 字节")).toBeVisible();
  expect(screen.getByText("已提供课程上下文")).toBeVisible();
  expect(screen.getAllByText("状态未闭合").length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: /预检/ }));
  expect(screen.getByText("Ubuntu 24.04 · amd64")).toBeVisible();
  expect(screen.getByText(/StepAudio/)).toBeVisible();
  expect(screen.queryByText(/configuration_fingerprint/)).not.toBeInTheDocument();
});

test("未知判别值停止解释并显示界面版本不兼容", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: "live_progress.v1",
          runs: [{ ...snapshot, observation_state: "running" }],
        }),
        { status: 200 },
      ),
    ),
  );

  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("界面版本不兼容");
});

test("详情中的未知判别值也停止解释", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/runs") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              schema_version: "live_progress.v1",
              runs: [{
                run_id: runId,
                observation_state: "unclosed",
                started_at: snapshot.observation.started_at,
                stage: snapshot.observation.stage,
                duration_ms: snapshot.observation.duration_ms,
                last_event_at: snapshot.observation.last_event_at,
                diagnostics: snapshot.diagnostics,
                snapshot_etag: '"sha256:' + "b".repeat(64) + '"',
              }],
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ ...snapshot, terminal: { status: "future_terminal" } }), { status: 200 }),
      );
    }),
  );

  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("界面版本不兼容");
});
