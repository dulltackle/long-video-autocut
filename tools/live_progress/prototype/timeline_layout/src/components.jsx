import {
  Activity,
  AlertTriangle,
  Ban,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  CircleDot,
  Clipboard,
  Clock3,
  Film,
  LockKeyhole,
  Play,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useRef } from "react";
import { LIFECYCLE_LABEL, STAGES, getStageView } from "./data.js";

const STATUS_ICON = {
  succeeded: Check,
  in_progress: CircleDot,
  failed: X,
  interrupted: Ban,
  corrupt: AlertTriangle,
  not_started: Circle,
};

export function StatusGlyph({ lifecycle, compact = false }) {
  const Icon = STATUS_ICON[lifecycle] ?? Circle;
  return (
    <span className={`status-glyph status-glyph--${lifecycle} ${compact ? "is-compact" : ""}`}>
      <Icon aria-hidden="true" size={compact ? 13 : 15} strokeWidth={2.25} />
      <span>{LIFECYCLE_LABEL[lifecycle]}</span>
    </span>
  );
}

export function TopBar({ scenario, onRunsOpen }) {
  return (
    <header className="top-bar">
      <div className="brand-lockup">
        {onRunsOpen ? <button className="icon-button mobile-only" type="button" onClick={onRunsOpen} aria-label="打开直播拆条运行列表"><Film size={18} /></button> : null}
        <span className="brand-mark"><Film size={18} aria-hidden="true" /></span>
        <div>
          <strong>直播拆条运行</strong>
          <span>切片流程进度 · 只读观测</span>
        </div>
      </div>
      <div className="top-facts" aria-label="直播拆条运行总览">
        <span><small>观测状态</small><strong data-status={scenario.status}>{scenario.status}</strong></span>
        <span><small>诊断完整性</small><strong>{scenario.integrity}</strong></span>
        <span><small>最后事件</small><strong>{scenario.lastEvent}</strong></span>
      </div>
    </header>
  );
}

export function RunOverview({ scenario, compact = false }) {
  return (
    <section className={`run-overview ${compact ? "run-overview--compact" : ""}`} aria-label="所选直播拆条运行">
      <div>
        <small>直播拆条运行标识</small>
        <code title="7d41af50-926d-4ca1-a14f-ea4d705582ce">7d41af50…0582ce</code>
      </div>
      <div><small>启动时间</small><strong>{scenario.startedAt}</strong></div>
      <div><small>{scenario.status === "状态未闭合" ? "已观测时长" : "终态耗时"}</small><strong>{scenario.elapsed}</strong></div>
      <div><small>当前或终态阶段</small><strong>{STAGES.find((stage) => stage.key === scenario.currentStage)?.label}</strong></div>
    </section>
  );
}

export function TerminalBanner({ banner }) {
  if (!banner) return null;
  const Icon = banner.tone === "danger" ? X : banner.tone === "warning" ? AlertTriangle : Ban;
  return (
    <section className={`terminal-banner terminal-banner--${banner.tone}`} role="status">
      <span className="terminal-banner__icon"><Icon size={20} aria-hidden="true" /></span>
      <div className="terminal-banner__main">
        <strong>{banner.title}</strong>
        <p>{banner.body}</p>
      </div>
      <div className="terminal-banner__action">
        <small>操作员动作建议</small>
        <p>{banner.action}</p>
      </div>
    </section>
  );
}

export function FactList({ facts, empty = "当前没有额外阶段事实。" }) {
  if (facts.length === 0) return <p className="facts-empty">{empty}</p>;
  return (
    <ul className="fact-list">
      {facts.slice(0, 3).map((fact) => <li key={fact}>{fact}</li>)}
    </ul>
  );
}

export function ActivityFacts({ scenario, updateCount = 0 }) {
  const facts = scenario.progressFacts;
  return (
    <div className="activity-facts">
      <div className="subsection-heading"><Activity size={15} aria-hidden="true" /><strong>阶段推进信号</strong></div>
      <FactList facts={facts} empty="终态阶段没有进行中的活动事实。" />
      <p className="evidence-note">只显示已经形成的事实，不表达完成比例、预计剩余时间或进程存活。</p>
    </div>
  );
}

export function StageFacts({ stage, scenario, includeProgress = false, updateCount = 0 }) {
  const view = getStageView(stage, scenario);
  return (
    <div className="stage-facts">
      <div className="stage-result">
        <small>{view.lifecycle === "in_progress" ? "当前事实" : "阶段主结论"}</small>
        <strong>{view.headline}</strong>
      </div>
      <FactList facts={view.facts} />
      {includeProgress && stage.key === scenario.currentStage ? <ActivityFacts scenario={scenario} updateCount={updateCount} /> : null}
    </div>
  );
}

export function DeliverySummary({ delivery, onOpenPreview }) {
  if (!delivery) return null;
  return (
    <section className="delivery-summary">
      <div className="delivery-summary__icon"><ShieldCheck size={20} aria-hidden="true" /></div>
      <div>
        <small>标准交付物</small>
        <strong>已形成 {delivery.count} 条短视频</strong>
        <span>{delivery.evidence} · 已取得播放资格</span>
      </div>
      <button className="primary-button" type="button" onClick={onOpenPreview}><Play size={15} fill="currentColor" />预览短视频</button>
    </section>
  );
}

export function PreviewPanel({ open, onClose }) {
  const closeButtonRef = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    closeButtonRef.current?.focus();
    const handleKey = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose, open]);
  if (!open) return null;
  return (
    <div className="preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <aside className="preview-panel" role="dialog" aria-modal="true" aria-labelledby="preview-title">
        <header>
          <div><small>已发布 · 短视频 1 / 3</small><h2 id="preview-title">从目标倒推复杂流程</h2></div>
          <button ref={closeButtonRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭短视频预览"><X /></button>
        </header>
        <div className="preview-frame"><Film size={38} aria-hidden="true" /><span>03:18</span><button type="button" aria-label="播放短视频"><Play fill="currentColor" /></button></div>
        <dl>
          <div><dt>主题</dt><dd>复杂流程设计</dd></div>
          <div><dt>源时间范围</dt><dd>01:15:24–01:18:42</dd></div>
          <div><dt>摘要</dt><dd>先固定真正要抵达的结果，再把途中仍需回答的问题逐层展开。</dd></div>
        </dl>
        <div className="preview-items" aria-label="短视频列表">
          {["从目标倒推复杂流程", "用事实而不是静默时间判断状态", "让阶段结论保持可读"].map((title, index) => <button key={title} className={index === 0 ? "is-selected" : ""} type="button"><span>{index + 1}</span>{title}<small>0{index + 2}:{index === 0 ? "18" : index === 1 ? "46" : "58"}</small></button>)}
        </div>
      </aside>
    </div>
  );
}

export function TechnicalDisclosure({ scenario }) {
  return (
    <details className="technical-disclosure">
      <summary><ChevronRight size={15} aria-hidden="true" />技术详情</summary>
      <div>
        <code>{scenario.status === "已失败" ? "topic_review.service_unavailable" : scenario.status === "记录损坏" ? "event.schema_invalid" : "diagnostics.recovery_incomplete"}</code>
        <button type="button" onClick={() => navigator.clipboard?.writeText(`观测状态：${scenario.status}\n诊断完整性：${scenario.integrity}`)}><Clipboard size={14} />复制诊断摘要</button>
      </div>
    </details>
  );
}

export function ExpandHint({ expanded }) {
  return <ChevronDown className={expanded ? "is-rotated" : ""} size={16} aria-hidden="true" />;
}

export function ReadOnlyMark() {
  return <span className="read-only-mark"><LockKeyhole size={13} aria-hidden="true" />只读观测</span>;
}

export function ClockFact({ children }) {
  return <span className="clock-fact"><Clock3 size={13} aria-hidden="true" />{children}</span>;
}
