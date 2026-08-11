import { useEffect, useRef, useState } from "react";
import {
  Activity,
  Check,
  ChevronDown,
  ChevronRight,
  Radio,
  ShieldAlert,
  X,
} from "lucide-react";
import {
  DeliverySummary,
  FactList,
  StatusGlyph,
  TechnicalDisclosure,
  TerminalBanner,
  TopBar,
} from "../components.jsx";
import { RUNS, STAGES, getStageView } from "../data.js";

function RunPicker({ scenario, onSelectScenario }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const handlePointer = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    const handleKey = (event) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      requestAnimationFrame(() => triggerRef.current?.focus());
    };
    window.addEventListener("pointerdown", handlePointer);
    window.addEventListener("keydown", handleKey);
    return () => {
      window.removeEventListener("pointerdown", handlePointer);
      window.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  const selectedRun = RUNS.find((run) => run.status === scenario.status) ?? RUNS[0];
  return (
    <section ref={rootRef} className="run-picker" aria-label="选择直播拆条运行">
      <button ref={triggerRef} className="run-picker__trigger" type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <span><small>当前直播拆条运行</small><code>{selectedRun.id}</code></span>
        <span><small>{scenario.startedAt}</small><strong>{scenario.status} · {STAGES.find((stage) => stage.key === scenario.currentStage)?.label}</strong></span>
        <span className="run-picker__action">切换运行<ChevronDown size={16} aria-hidden="true" /></span>
      </button>
      {open ? (
        <div className="run-picker__overlay" role="dialog" aria-modal="false" aria-label="直播拆条运行列表">
          <header><div><strong>直播拆条运行</strong><small>按首个事件时间倒序</small></div><button className="icon-button" type="button" aria-label="关闭运行列表" onClick={() => { setOpen(false); triggerRef.current?.focus(); }}><X size={18} /></button></header>
          <div className="run-picker__list">
            {RUNS.map((run) => (
              <button key={run.id} className={run.scenario === selectedRun.scenario ? "is-selected" : ""} type="button" onClick={() => { setOpen(false); onSelectScenario(run.scenario); }}>
                <span><strong>{run.status}</strong><code>{run.id}</code></span>
                <span><small>{run.startedAt}</small><strong>{run.stage}</strong></span>
                <span><small>{run.integrity}</small><strong>{run.duration}</strong></span>
                {run.scenario === selectedRun.scenario ? <Check size={16} aria-label="当前选择" /> : <ChevronRight size={16} aria-hidden="true" />}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function factLabel(fact) {
  if (fact.includes("供应商") || fact.includes("模型请求")) return "供应商请求";
  if (fact.includes("重试") || fact.includes("补转")) return "重试与退避";
  if (fact.includes("缓存")) return "缓存";
  if (fact.includes("转写") || fact.includes("交付物")) return "阶段产物";
  if (fact.includes("验证")) return "验证事实";
  return "已形成事实";
}

function StageFactRail({ selected, scenario, updateCount }) {
  const view = getStageView(selected, scenario);
  const isCurrent = selected.key === scenario.currentStage;
  const facts = isCurrent
    ? scenario.stageSignals
    : view.facts.slice(0, 3).map((fact) => ({ label: factLabel(fact), value: fact }));
  return (
    <aside className="stage-fact-rail" aria-label={isCurrent ? "阶段推进信号" : "阶段事实"}>
      <header>
        <span><Activity size={15} aria-hidden="true" />{isCurrent ? "阶段推进信号" : "阶段事实"}</span>
        <small>{isCurrent ? `最后事件 ${scenario.lastEvent}` : "已形成的事实"}</small>
      </header>
      {facts.length ? (
        <dl data-update={updateCount}>
          {facts.map((fact) => <div key={`${fact.label}-${fact.value}`}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}
        </dl>
      ) : <p>当前阶段没有额外事实。</p>}
      <footer>只呈现完整事实；不推测静默、完成比例或预计剩余时间。</footer>
    </aside>
  );
}

export default function VariantB({ scenario, updateCount, onOpenPreview, onSelectScenario }) {
  const [selectedStage, setSelectedStage] = useState(scenario.currentStage);
  const selected = STAGES.find((stage) => stage.key === selectedStage);
  const view = getStageView(selected, scenario);
  const hasNewStage = scenario.newStageStarted && selectedStage !== scenario.currentStage;

  return (
    <main className="app-frame variant-b">
      <TopBar scenario={scenario} />
      <div className="variant-b__body">
        <nav className="stage-console" aria-label="九阶段时间线">
          {STAGES.map((stage, index) => {
            const stageView = getStageView(stage, scenario);
            return (
              <button key={stage.key} type="button" className={`stage-console__item stage-console__item--${stageView.lifecycle} ${selectedStage === stage.key ? "is-selected" : ""}`} onClick={() => setSelectedStage(stage.key)} aria-current={selectedStage === stage.key ? "step" : undefined}>
                <span className="stage-console__number">{index + 1}</span>
                <strong>{stage.label}</strong>
                <StatusGlyph lifecycle={stageView.lifecycle} compact />
              </button>
            );
          })}
        </nav>
        <div className="variant-b__workspace">
          <section className="stage-canvas" aria-labelledby="stage-canvas-title">
            <TerminalBanner banner={scenario.banner} />
            <DeliverySummary delivery={scenario.delivery} onOpenPreview={onOpenPreview} />
            <RunPicker scenario={scenario} onSelectScenario={onSelectScenario} />
            {hasNewStage ? (
              <div className="new-stage-notice" role="status">
                <span><Radio size={15} aria-hidden="true" />新阶段已开始</span>
                <button type="button" onClick={() => setSelectedStage(scenario.currentStage)}>查看当前阶段</button>
              </div>
            ) : null}
            <header>
              <div><small>阶段 {STAGES.indexOf(selected) + 1} / 9</small><h1 id="stage-canvas-title">{selected.label}</h1></div>
              <div><StatusGlyph lifecycle={view.lifecycle} /><span>{view.duration ?? "—"}</span></div>
            </header>
            <div className="stage-canvas__conclusion">
              <span className="conclusion-icon">{view.lifecycle === "failed" || view.lifecycle === "corrupt" ? <ShieldAlert /> : <Radio />}</span>
              <div><small>{view.lifecycle === "in_progress" ? "当前已知" : "阶段主结论"}</small><strong>{view.headline}</strong></div>
            </div>
            <div className="stage-canvas__facts">
              <div><h2>辅助事实</h2><FactList facts={view.facts} /></div>
            </div>
            {(view.lifecycle === "failed" || view.lifecycle === "interrupted" || view.lifecycle === "corrupt") ? <TechnicalDisclosure scenario={scenario} /> : null}
            <footer><span>选择阶段只改变详情，不折叠九阶段轨道。</span><button type="button" onClick={() => { const index = STAGES.indexOf(selected); setSelectedStage(STAGES[Math.min(index + 1, STAGES.length - 1)].key); }}>查看下一阶段<ChevronRight size={15} /></button></footer>
          </section>
          <StageFactRail selected={selected} scenario={scenario} updateCount={updateCount} />
        </div>
      </div>
    </main>
  );
}
