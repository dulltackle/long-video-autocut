import { useEffect, useState } from "react";

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

type ViewState = "loading" | "empty" | "error" | "unsupported";

function StageIcon({ path }: { path: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <path d={path} />
    </svg>
  );
}

function EmptyIcon() {
  return (
    <svg className="empty-icon" aria-hidden="true" viewBox="0 0 64 64">
      <path d="M18 9h23l9 9v31a6 6 0 0 1-6 6H18a6 6 0 0 1-6-6V15a6 6 0 0 1 6-6Z" />
      <path d="M41 9v11h11M22 30h19M22 39h13" />
      <circle cx="47" cy="47" r="12" />
      <path d="m42.5 42.5 9 9m0-9-9 9" />
    </svg>
  );
}

function loadRunIndex(signal: AbortSignal): Promise<ViewState> {
  return fetch("/api/v1/runs", { signal, headers: { Accept: "application/json" } })
    .then(async (response) => {
      if (!response.ok) return "error";
      const value: unknown = await response.json();
      if (
        typeof value !== "object" ||
        value === null ||
        !("schema_version" in value) ||
        value.schema_version !== "live_progress.v1"
      ) {
        return "unsupported";
      }
      if (!("runs" in value) || !Array.isArray(value.runs)) return "unsupported";
      return value.runs.length === 0 ? "empty" : "unsupported";
    })
    .catch((error: unknown) => {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      return "error";
    });
}

export function App() {
  const [view, setView] = useState<ViewState>("loading");

  useEffect(() => {
    const controller = new AbortController();
    void loadRunIndex(controller.signal).then(setView).catch(() => undefined);
    return () => controller.abort();
  }, []);

  return (
    <div className="app-shell">
      <header className="topbar">
        <h1>直播拆条</h1>
        <span aria-hidden="true" className="title-rule" />
        <p>阶段控制台</p>
      </header>

      <nav className="stage-viewport" aria-label="直播拆条阶段">
        <ol className="stage-track">
          {STAGES.map((stage) => (
            <li key={stage.code} className="stage stage-not-started">
              <span className="stage-node" />
              <span className="stage-icon"><StageIcon path={stage.icon} /></span>
              <span className="stage-label">{stage.label}</span>
            </li>
          ))}
        </ol>
      </nav>

      <main className="main-content">
        {view === "loading" ? (
          <p className="state-message" aria-live="polite">正在读取工作区…</p>
        ) : null}
        {view === "empty" ? (
          <section className="empty-state">
            <EmptyIcon />
            <h2>还没有直播拆条运行</h2>
            <p>工作区已就绪。启动一次直播拆条后，这里会显示已形成的阶段事实。</p>
          </section>
        ) : null}
        {view === "error" ? (
          <section className="state-message state-error" role="alert">
            <h2>暂时无法读取工作区</h2>
            <p>请检查阶段控制台服务后重新加载页面。</p>
          </section>
        ) : null}
        {view === "unsupported" ? (
          <section className="state-message state-error" role="alert">
            <h2>界面版本不兼容</h2>
            <p>当前界面无法安全解释服务返回的运行事实。</p>
          </section>
        ) : null}
      </main>

      <footer>
        <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-10v.1" /></svg>
        <p>此界面仅观察工作区，不会启动或修改直播拆条运行。</p>
      </footer>
    </div>
  );
}
