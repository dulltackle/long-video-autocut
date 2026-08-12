import { useEffect, useMemo, useRef, useState } from "react";

import "./styles.css";

const STAGES = [
  { code: "initialized", label: "初始化", icon: "M12 2v3m0 14v3M4.9 4.9 7 7m10 10 2.1 2.1M2 12h3m14 0h3M4.9 19.1 7 17m10-10 2.1-2.1M8 12a4 4 0 1 0 8 0 4 4 0 0 0-8 0Z" },
  { code: "preflight", label: "预检", icon: "M9 5h6m-6 0a3 3 0 0 1 6 0m-8 0H5v16h14V5h-2m-8 8 2 2 4-5" },
  { code: "source_analysis", label: "素材分析", icon: "M5 3h9l5 5v13H5V3Zm9 0v5h5M9 12l5 3-5 3v-6Z" },
  { code: "transcription", label: "语音识别", icon: "M4 10v4m4-8v12m4-16v20m4-15v10m4-7v4" },
  { code: "candidate_planning", label: "候选规划", icon: "M12 3v6m0 0H6v4m6-4h6v4M3 13h6v6H3v-6Zm12 0h6v6h-6v-6ZM9 3h6v4H9V3Z" },
  { code: "topic_review", label: "主题评审", icon: "M4 4h16v13H9l-5 4V4Zm4 5h8m-8 4h5" },
  { code: "delivery_build", label: "交付构建", icon: "m12 2 8 4.5v11L12 22l-8-4.5v-11L12 2Zm0 9 8-4.5M12 11 4 6.5M12 11v11" },
  { code: "delivery_verification", label: "交付验证", icon: "M12 3 4 6v6c0 5 3.4 8.1 8 10 4.6-1.9 8-5 8-10V6l-8-3Zm-4 9 2.5 2.5L16 9" },
  { code: "publishing", label: "发布", icon: "m3 11 18-8-8 18-2-7-8-3Zm8 3 4-5" },
] as const;

type RunStage = (typeof STAGES)[number]["code"];
type ObservationState = "succeeded" | "failed" | "interrupted" | "unclosed" | "record_corrupt";
type Presence<T> =
  | { status: "available"; value: T }
  | { status: "not_observed" }
  | { status: "not_applicable" };
type DiagnosticIntegrity = {
  state: "complete" | "incomplete" | "corrupt" | "unavailable";
  reason: string;
  verified_event_count: number;
};
type RunIndexItem = {
  run_id: string;
  observation_state: ObservationState;
  started_at: Presence<string>;
  stage: Presence<RunStage>;
  duration_ms: Presence<number>;
  last_event_at: Presence<string>;
  diagnostics: DiagnosticIntegrity;
  snapshot_etag: string;
};
type StageLifecycle =
  | { status: "not_started" }
  | { status: "in_progress"; started_at: string }
  | { status: "succeeded"; observed_at?: string; started_at?: string; ended_at?: string; duration_ms?: number }
  | { status: "failed" | "interrupted"; started_at: string; ended_at: string; duration_ms: number };
type StageSnapshot = {
  stage: RunStage;
  lifecycle: StageLifecycle;
  result: Record<string, unknown>;
  progress: Record<string, unknown>;
  error_ids: string[];
};
type RunSnapshot = {
  schema_version: "live_progress.v1";
  run_id: string;
  observation: {
    state: ObservationState;
    stage: Presence<RunStage>;
    started_at: Presence<string>;
    ended_at: Presence<string>;
    duration_ms: Presence<number>;
    last_event_at: Presence<string>;
  };
  diagnostics: DiagnosticIntegrity;
  stages: StageSnapshot[];
};
type ViewState = "loading" | "empty" | "ready" | "error" | "unsupported";

const OBSERVATION_STATES = ["succeeded", "failed", "interrupted", "unclosed", "record_corrupt"] as const;
const DIAGNOSTIC_STATES = ["complete", "incomplete", "corrupt", "unavailable"] as const;
const DIAGNOSTIC_REASONS = new Set([
  "valid", "event_log_missing", "event_tail_incomplete", "terminal_event_missing", "run_manifest_missing",
  "event_encoding_invalid", "event_json_invalid", "event_duplicate_field", "event_non_finite_number",
  "event_schema_invalid", "event_code_unknown", "event_field_unknown", "event_sequence_mismatch",
  "event_run_id_mismatch", "run_id_directory_mismatch", "manifest_encoding_invalid", "manifest_json_invalid",
  "manifest_duplicate_field", "manifest_non_finite_number", "manifest_schema_invalid", "manifest_field_unknown",
  "manifest_run_id_mismatch", "manifest_event_count_mismatch", "manifest_event_byte_length_mismatch",
  "manifest_event_digest_mismatch", "too_large", "event_log_symlink", "event_log_unreadable",
  "run_manifest_symlink", "run_manifest_unreadable", "run_directory_unsafe",
]);
const RUN_ID = /^run_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const STRONG_ETAG = /^"sha256:[0-9a-f]{64}"$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOneOf<T extends string>(value: unknown, values: readonly T[]): value is T {
  return typeof value === "string" && values.includes(value as T);
}

function parsePresence<T>(value: unknown, parse: (candidate: unknown) => candidate is T): Presence<T> | null {
  if (!isRecord(value) || typeof value.status !== "string") return null;
  if (value.status === "not_observed" || value.status === "not_applicable") return { status: value.status };
  if (value.status !== "available" || !parse(value.value)) return null;
  return { status: "available", value: value.value };
}

const isString = (value: unknown): value is string => typeof value === "string";
const isNumber = (value: unknown): value is number => Number.isInteger(value) && Number(value) >= 0;
const isStage = (value: unknown): value is RunStage => isOneOf(value, STAGES.map(({ code }) => code));
const isJsonValue = (value: unknown): value is unknown => value !== undefined;

function parseDiagnostics(value: unknown): DiagnosticIntegrity | null {
  if (!isRecord(value) || !isOneOf(value.state, DIAGNOSTIC_STATES) || !DIAGNOSTIC_REASONS.has(String(value.reason)) || !isNumber(value.verified_event_count)) return null;
  return { state: value.state, reason: String(value.reason), verified_event_count: value.verified_event_count };
}

function parseIndex(value: unknown): RunIndexItem[] | null {
  if (!isRecord(value) || value.schema_version !== "live_progress.v1" || !Array.isArray(value.runs)) return null;
  const runs: RunIndexItem[] = [];
  for (const candidate of value.runs) {
    if (!isRecord(candidate) || typeof candidate.run_id !== "string" || !RUN_ID.test(candidate.run_id) || !isOneOf(candidate.observation_state, OBSERVATION_STATES) || typeof candidate.snapshot_etag !== "string" || !STRONG_ETAG.test(candidate.snapshot_etag)) return null;
    const startedAt = parsePresence(candidate.started_at, isString);
    const stage = parsePresence(candidate.stage, isStage);
    const duration = parsePresence(candidate.duration_ms, isNumber);
    const lastEvent = parsePresence(candidate.last_event_at, isString);
    const diagnostics = parseDiagnostics(candidate.diagnostics);
    if (!startedAt || !stage || !duration || !lastEvent || !diagnostics) return null;
    runs.push({ run_id: candidate.run_id, observation_state: candidate.observation_state, started_at: startedAt, stage, duration_ms: duration, last_event_at: lastEvent, diagnostics, snapshot_etag: candidate.snapshot_etag });
  }
  return runs;
}

function parseLifecycle(value: unknown, stage: RunStage): StageLifecycle | null {
  if (!isRecord(value) || typeof value.status !== "string") return null;
  if (value.status === "not_started") return { status: "not_started" };
  if (value.status === "in_progress" && typeof value.started_at === "string") return { status: "in_progress", started_at: value.started_at };
  if (value.status === "succeeded" && stage === "initialized" && typeof value.observed_at === "string") return { status: "succeeded", observed_at: value.observed_at };
  if (isOneOf(value.status, ["succeeded", "failed", "interrupted"] as const) && typeof value.started_at === "string" && typeof value.ended_at === "string" && isNumber(value.duration_ms)) {
    return { status: value.status, started_at: value.started_at, ended_at: value.ended_at, duration_ms: value.duration_ms };
  }
  return null;
}

function validKnownStageResult(stage: RunStage, result: Record<string, unknown>): boolean {
  const presence = (name: string, parser: (value: unknown) => boolean) => {
    const parsed = parsePresence(result[name], (value): value is unknown => parser(value));
    return parsed !== null;
  };
  if (stage === "initialized") return presence("application_version", isString);
  if (stage === "source_analysis") return presence("byte_length", isNumber) && presence("duration_ms", isNumber) && presence("course_context_provided", (value) => typeof value === "boolean");
  if (stage === "preflight") {
    if (!presence("certified_platform", (value) => value === "ubuntu_24_04_amd64") || !isRecord(result.tool_versions) || !isRecord(result.provider_selections)) return false;
    for (const tool of ["python", "ffmpeg", "ffprobe"]) if (!parsePresence(result.tool_versions[tool], isString)) return false;
    if (!parsePresence(result.font, (value): value is unknown => isRecord(value) && typeof value.family === "string" && typeof value.available === "boolean")) return false;
    for (const capability of ["transcription", "topic_review", "subtitle_optimization"]) {
      if (!parsePresence(result.provider_selections[capability], (value): value is unknown => isRecord(value) && typeof value.adapter_id === "string" && typeof value.provider_id === "string" && typeof value.model_id === "string")) return false;
    }
  }
  const laterFields: Partial<Record<RunStage, string[]>> = {
    transcription: ["transcript_chunk_count", "coverage_recovery_count", "transcript_cache"],
    candidate_planning: ["candidate_count"],
    topic_review: ["reviewed_candidate_count", "cache_observation"],
    delivery_build: ["short_video_count", "created_artifacts_by_role", "subtitle_cache_observation"],
    delivery_verification: ["verified_item_count", "verified_short_video_count", "verified_items_by_role"],
    publishing: ["publication_fact"],
  };
  for (const field of laterFields[stage] ?? []) if (!presence(field, isJsonValue)) return false;
  return true;
}

function validKnownStageProgress(stage: RunStage, progress: Record<string, unknown>): boolean {
  const fields: Partial<Record<RunStage, string[]>> = {
    preflight: ["configuration_observed", "environment_outcome"],
    transcription: ["completed_request_count", "inflight_request_count", "transport_retry_count", "latest_retry"],
    topic_review: ["completed_request_count", "inflight_request_count", "transport_retry_count", "latest_retry"],
    delivery_build: ["completed_request_count", "inflight_request_count", "transport_retry_count", "latest_retry", "delivery_state"],
    delivery_verification: ["delivery_state"],
    publishing: ["delivery_state"],
  };
  return (fields[stage] ?? []).every((field) => parsePresence(progress[field], isJsonValue) !== null);
}

function parseTerminal(value: unknown): boolean {
  return parsePresence(
    value,
    (candidate): candidate is Record<string, unknown> => isRecord(candidate)
      && isOneOf(candidate.evidence, ["run_manifest", "terminal_event"] as const)
      && isOneOf(candidate.outcome, ["succeeded", "failed", "interrupted"] as const)
      && parsePresence(candidate.ended_at, isString) !== null
      && parsePresence(candidate.duration_ms, isNumber) !== null
      && parsePresence(candidate.exit_code, isNumber) !== null
      && parsePresence(candidate.result_kind, (result): result is "clips" | "empty" => isOneOf(result, ["clips", "empty"] as const)) !== null
      && parsePresence(candidate.interruption_signal, (signal): signal is "sigint" | "sigterm" => isOneOf(signal, ["sigint", "sigterm"] as const)) !== null,
  ) !== null;
}

function parseSnapshot(value: unknown, expectedRunId: string): RunSnapshot | null {
  if (!isRecord(value) || value.schema_version !== "live_progress.v1" || value.run_id !== expectedRunId || !isRecord(value.observation) || !isOneOf(value.observation.state, OBSERVATION_STATES) || !Array.isArray(value.stages)) return null;
  const observationStage = parsePresence(value.observation.stage, isStage);
  const startedAt = parsePresence(value.observation.started_at, isString);
  const endedAt = parsePresence(value.observation.ended_at, isString);
  const duration = parsePresence(value.observation.duration_ms, isNumber);
  const lastEvent = parsePresence(value.observation.last_event_at, isString);
  const diagnostics = parseDiagnostics(value.diagnostics);
  if (!observationStage || !startedAt || !endedAt || !duration || !lastEvent || !diagnostics || value.stages.length !== STAGES.length) return null;
  const stages: StageSnapshot[] = [];
  for (let index = 0; index < STAGES.length; index += 1) {
    const candidate = value.stages[index];
    const expectedStage = STAGES[index].code;
    if (!isRecord(candidate) || candidate.stage !== expectedStage || !isRecord(candidate.result) || !isRecord(candidate.progress) || !Array.isArray(candidate.error_ids) || !candidate.error_ids.every(isString)) return null;
    const lifecycle = parseLifecycle(candidate.lifecycle, expectedStage);
    if (!lifecycle || !validKnownStageResult(expectedStage, candidate.result) || !validKnownStageProgress(expectedStage, candidate.progress)) return null;
    stages.push({ stage: expectedStage, lifecycle, result: candidate.result, progress: candidate.progress, error_ids: candidate.error_ids });
  }
  if (!parseTerminal(value.terminal) || !isRecord(value.errors) || !isRecord(value.errors.by_id) || !parsePresence(value.errors.primary_error_id, isJsonValue) || !parsePresence(value.errors.associated_error_ids, isJsonValue) || !parsePresence(value.errors.recovery_incomplete, isJsonValue)) return null;
  if (!isRecord(value.delivery) || !isOneOf(value.delivery.status, ["not_observed", "building", "awaiting_verification", "verification_failed", "verified", "publishing", "publication_failed", "published", "valid_empty"] as const) || !parsePresence(value.delivery.short_video_count, isNumber) || !isRecord(value.delivery.preview) || !isOneOf(value.delivery.preview.status, ["not_ready", "not_available", "not_applicable", "available"] as const)) return null;
  return { schema_version: "live_progress.v1", run_id: expectedRunId, observation: { state: value.observation.state, stage: observationStage, started_at: startedAt, ended_at: endedAt, duration_ms: duration, last_event_at: lastEvent }, diagnostics, stages };
}

function StageIcon({ path }: { path: string }) {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d={path} /></svg>;
}

function EmptyIcon() {
  return <svg className="empty-icon" aria-hidden="true" viewBox="0 0 64 64"><path d="M18 9h23l9 9v31a6 6 0 0 1-6 6H18a6 6 0 0 1-6-6V15a6 6 0 0 1 6-6Z" /><path d="M41 9v11h11M22 30h19M22 39h13" /><circle cx="47" cy="47" r="12" /><path d="m42.5 42.5 9 9m0-9-9 9" /></svg>;
}

const STATE_LABELS: Record<ObservationState, string> = { succeeded: "已成功", failed: "已失败", interrupted: "已中断", unclosed: "状态未闭合", record_corrupt: "记录损坏" };
const LIFECYCLE_LABELS = { not_started: "未开始", in_progress: "进行中", succeeded: "成功", failed: "失败", interrupted: "中断" } as const;

function valueOf<T>(value: unknown): T | undefined {
  return isRecord(value) && value.status === "available" ? value.value as T : undefined;
}

function StageFacts({ snapshot }: { snapshot: StageSnapshot }) {
  const metadata = STAGES.find(({ code }) => code === snapshot.stage)!;
  if (snapshot.stage === "initialized") {
    return <section className="fact-card"><h2>{metadata.label}</h2><p className="fact-lead">直播拆条运行已经初始化</p><dl><div><dt>CLI 底座版本</dt><dd>{valueOf<string>(snapshot.result.application_version) ?? "尚未观察到"}</dd></div></dl></section>;
  }
  if (snapshot.stage === "preflight") {
    const platform = valueOf<string>(snapshot.result.certified_platform);
    const tools = isRecord(snapshot.result.tool_versions) ? snapshot.result.tool_versions : {};
    const providers = isRecord(snapshot.result.provider_selections) ? snapshot.result.provider_selections : {};
    const transcription = valueOf<Record<string, string>>(providers.transcription);
    return <section className="fact-card"><h2>{metadata.label}</h2><p className="fact-lead">{snapshot.lifecycle.status === "succeeded" ? "认证环境预检已经完成" : "正在观察认证环境事实"}</p><dl><div><dt>认证平台</dt><dd>{platform === "ubuntu_24_04_amd64" ? "Ubuntu 24.04 · amd64" : "尚未观察到"}</dd></div><div><dt>工具版本</dt><dd>Python {valueOf<string>(tools.python) ?? "—"} · FFmpeg {valueOf<string>(tools.ffmpeg) ?? "—"} · FFprobe {valueOf<string>(tools.ffprobe) ?? "—"}</dd></div><div><dt>供应商能力</dt><dd>{transcription ? `StepAudio · ${transcription.model_id}` : "尚未观察到"}</dd></div></dl></section>;
  }
  if (snapshot.stage === "source_analysis") {
    const bytes = valueOf<number>(snapshot.result.byte_length);
    const duration = valueOf<number>(snapshot.result.duration_ms);
    const context = valueOf<boolean>(snapshot.result.course_context_provided);
    return <section className="fact-card"><h2>{metadata.label}</h2><p className="fact-lead">{snapshot.lifecycle.status === "succeeded" ? "素材分析已经形成可信事实" : "正在观察素材事实"}</p><dl><div><dt>素材大小</dt><dd>{bytes === undefined ? "尚未观察到" : `${bytes.toLocaleString("zh-CN")} 字节`}</dd></div><div><dt>素材时长</dt><dd>{duration === undefined ? "尚未观察到" : `${(duration / 1000).toFixed(3)} 秒`}</dd></div><div><dt>课程上下文</dt><dd>{context === undefined ? "尚未观察到" : context ? "已提供课程上下文" : "未提供课程上下文"}</dd></div></dl></section>;
  }
  return <section className="fact-card"><h2>{metadata.label}</h2><p className="fact-lead">{snapshot.lifecycle.status === "not_started" ? "该阶段尚未开始" : `阶段${LIFECYCLE_LABELS[snapshot.lifecycle.status]}`}</p></section>;
}

export function App() {
  const [view, setView] = useState<ViewState>("loading");
  const [runs, setRuns] = useState<RunIndexItem[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [selectedStage, setSelectedStage] = useState<RunStage>("initialized");
  const userSelectedStage = useRef(false);
  const indexEtag = useRef<string | null>(null);
  const detailEtag = useRef<string | null>(null);
  const selectedRunIdRef = useRef<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const loadIndex = async () => {
      if (document.visibilityState === "hidden") return;
      try {
        const headers: Record<string, string> = { Accept: "application/json" };
        if (indexEtag.current) headers["If-None-Match"] = indexEtag.current;
        const indexResponse = await fetch("/api/v1/runs", { signal: controller.signal, headers });
        if (indexResponse.status === 304) return;
        if (!indexResponse.ok) { setView("error"); return; }
        const parsedRuns = parseIndex(await indexResponse.json());
        if (parsedRuns === null) { setView("unsupported"); return; }
        indexEtag.current = indexResponse.headers.get("ETag");
        if (parsedRuns.length === 0) { setRuns([]); selectedRunIdRef.current = null; setSelectedRunId(null); setSnapshot(null); setView("empty"); return; }
        setRuns(parsedRuns);
        const current = selectedRunIdRef.current;
        if (!current || !parsedRuns.some(({ run_id }) => run_id === current)) {
          const queryRunId = new URLSearchParams(window.location.search).get("run");
          const next = parsedRuns.some(({ run_id }) => run_id === queryRunId) ? queryRunId! : parsedRuns[0].run_id;
          userSelectedStage.current = false;
          detailEtag.current = null;
          selectedRunIdRef.current = next;
          setSnapshot(null);
          setSelectedRunId(next);
          setView("loading");
        }
      } catch (error: unknown) {
        if (!(error instanceof DOMException && error.name === "AbortError")) setView("error");
      }
    };
    const onVisibility = () => { if (document.visibilityState === "visible") void loadIndex(); };
    void loadIndex();
    const timer = window.setInterval(() => { void loadIndex(); }, 1000);
    document.addEventListener("visibilitychange", onVisibility);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, []);

  const selectedIndexEtag = runs.find(({ run_id }) => run_id === selectedRunId)?.snapshot_etag ?? null;

  useEffect(() => {
    if (!selectedRunId) return;
    const controller = new AbortController();
    const loadDetail = async () => {
      if (document.visibilityState === "hidden") return;
      try {
        const headers: Record<string, string> = { Accept: "application/json" };
        if (detailEtag.current) headers["If-None-Match"] = detailEtag.current;
        const response = await fetch(`/api/v1/runs/${selectedRunId}`, { signal: controller.signal, headers });
        if (response.status === 304) return;
        if (!response.ok) { setView("error"); return; }
        const parsed = parseSnapshot(await response.json(), selectedRunId);
        if (parsed === null) { setView("unsupported"); return; }
        detailEtag.current = response.headers.get("ETag");
        setSnapshot(parsed);
        if (!userSelectedStage.current && parsed.observation.stage.status === "available") setSelectedStage(parsed.observation.stage.value);
        setView("ready");
      } catch (error: unknown) {
        if (!(error instanceof DOMException && error.name === "AbortError")) setView("error");
      }
    };
    void loadDetail();
    const timer = window.setInterval(() => {
      if (!snapshot || snapshot.observation.state === "unclosed" || snapshot.observation.state === "record_corrupt") void loadDetail();
    }, 1000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [selectedRunId, selectedIndexEtag, snapshot?.observation.state]);

  const selectedSnapshot = useMemo(() => snapshot?.stages.find(({ stage }) => stage === selectedStage) ?? null, [snapshot, selectedStage]);

  return (
    <div className="app-shell">
      <header className="topbar"><h1>直播拆条</h1><span aria-hidden="true" className="title-rule" /><p>阶段控制台</p></header>
      <nav className="stage-viewport" aria-label="直播拆条阶段">
        <ol className="stage-track">
          {STAGES.map((stage, index) => {
            const lifecycle = snapshot?.stages[index].lifecycle.status ?? "not_started";
            const content = <><span className="stage-node" /><span className="stage-icon"><StageIcon path={stage.icon} /></span><span className="stage-label">{stage.label}</span>{snapshot ? <span className="stage-status">{LIFECYCLE_LABELS[lifecycle]}</span> : null}</>;
            return <li key={stage.code} className={`stage stage-${lifecycle.replace("_", "-")} ${selectedStage === stage.code && snapshot ? "stage-selected" : ""}`}>{snapshot ? <button type="button" aria-pressed={selectedStage === stage.code} onClick={() => { userSelectedStage.current = true; setSelectedStage(stage.code); }}>{content}</button> : content}</li>;
          })}
        </ol>
      </nav>
      <main className="main-content">
        {view === "loading" ? <p className="state-message" aria-live="polite">正在读取工作区…</p> : null}
        {view === "empty" ? <section className="empty-state"><EmptyIcon /><h2>还没有直播拆条运行</h2><p>工作区已就绪。启动一次直播拆条后，这里会显示已形成的阶段事实。</p></section> : null}
        {view === "error" ? <section className="state-message state-error" role="alert"><h2>暂时无法读取工作区</h2><p>请检查阶段控制台服务后重新加载页面。</p></section> : null}
        {view === "unsupported" ? <section className="state-message state-error" role="alert"><h2>界面版本不兼容</h2><p>当前界面无法安全解释服务返回的运行事实。</p></section> : null}
        {view === "ready" && snapshot && selectedSnapshot ? <div className="run-console"><aside className="run-index" aria-label="直播拆条运行"><p className="eyebrow">全部运行 · {runs.length}</p>{runs.map((run) => <button type="button" key={run.run_id} className={run.run_id === selectedRunId ? "run-selected" : ""} onClick={() => { if (run.run_id === selectedRunId) return; userSelectedStage.current = false; detailEtag.current = null; selectedRunIdRef.current = run.run_id; setSnapshot(null); setSelectedRunId(run.run_id); setView("loading"); const query = new URLSearchParams(window.location.search); query.set("run", run.run_id); window.history.replaceState(null, "", `${window.location.pathname}?${query}`); }}><span>{run.run_id.slice(4, 12)}</span><strong>{STATE_LABELS[run.observation_state]}</strong></button>)}</aside><article className="run-detail"><header><div><p className="eyebrow">运行 {snapshot.run_id.slice(4, 12)}</p><h2>{STATE_LABELS[snapshot.observation.state]}</h2></div><p className={`state-pill state-${snapshot.observation.state}`}>{STATE_LABELS[snapshot.observation.state]}</p></header><StageFacts snapshot={selectedSnapshot} /></article></div> : null}
      </main>
      <footer><svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-10v.1" /></svg><p>此界面仅观察工作区，不会启动或修改直播拆条运行。</p></footer>
    </div>
  );
}
