import { useEffect, useState } from "react";
import { ChevronRight, List, X } from "lucide-react";
import {
  ActivityFacts,
  ClockFact,
  DeliverySummary,
  FactList,
  ReadOnlyMark,
  RunOverview,
  StatusGlyph,
  TerminalBanner,
  TopBar,
} from "../components.jsx";
import { RUNS, STAGES, getStageView } from "../data.js";

function RunList({ open, onClose }) {
  return (
    <aside className={`variant-a__runs ${open ? "is-open" : ""}`} aria-label="直播拆条运行列表">
      <header>
        <div><strong>直播拆条运行</strong><small>按首事件时间倒序</small></div>
        <button className="icon-button mobile-only" type="button" onClick={onClose} aria-label="关闭直播拆条运行列表"><X size={18} /></button>
      </header>
      <nav>
        {RUNS.map((run, index) => (
          <button key={run.id} className={`run-list-row ${index === 0 ? "is-selected" : ""}`} type="button">
            <span className="run-list-row__top"><strong>{run.startedAt}</strong><span>{run.status}</span></span>
            <span>{run.stage} · {run.duration}</span>
            <span>最后事件 {run.lastEvent}</span>
            <code>{run.id}</code>
            {run.integrity !== "完整" ? <em>诊断{run.integrity}</em> : null}
          </button>
        ))}
      </nav>
      <footer><ReadOnlyMark /><span>新运行只更新列表，不抢占当前选择。</span></footer>
    </aside>
  );
}

export default function VariantA({ scenario, updateCount, onOpenPreview }) {
  const [runsOpen, setRunsOpen] = useState(false);
  const [selectedStage, setSelectedStage] = useState(scenario.currentStage);
  useEffect(() => setSelectedStage(scenario.currentStage), [scenario.currentStage]);
  const selected = STAGES.find((stage) => stage.key === selectedStage);
  const selectedView = getStageView(selected, scenario);
  return (
    <main className="app-frame variant-a">
      <TopBar scenario={scenario} onRunsOpen={() => setRunsOpen(true)} />
      <div className="variant-a__shell">
        <RunList open={runsOpen} onClose={() => setRunsOpen(false)} />
        {runsOpen ? <button className="mobile-scrim" type="button" aria-label="关闭列表" onClick={() => setRunsOpen(false)} /> : null}
        <div className="variant-a__main">
          <RunOverview scenario={scenario} />
          <TerminalBanner banner={scenario.banner} />
          <DeliverySummary delivery={scenario.delivery} onOpenPreview={onOpenPreview} />
          <section className="variant-a__timeline" aria-labelledby="variant-a-title">
            <header className="section-heading">
              <div><h1 id="variant-a-title">阶段时间线</h1><p>沿处理顺序连续阅读；状态与耗时位于摘要外框。</p></div>
              <span><List size={15} />九个阶段</span>
            </header>
            <ol>
              {STAGES.map((stage, index) => {
                const view = getStageView(stage, scenario);
                const selectedNow = stage.key === selectedStage;
                return (
                  <li key={stage.key} className={`narrative-stage narrative-stage--${view.lifecycle} ${selectedNow ? "is-selected" : ""}`}>
                    <span className="narrative-stage__rail" aria-hidden="true"><b>{index + 1}</b>{index < STAGES.length - 1 ? <i /> : null}</span>
                    <button type="button" className="narrative-stage__button" onClick={() => setSelectedStage(stage.key)} aria-expanded={selectedNow}>
                      <span className="narrative-stage__title"><strong>{stage.label}</strong><small>{view.headline}</small></span>
                      <span className="narrative-stage__meta"><StatusGlyph lifecycle={view.lifecycle} /><ClockFact>{view.duration ?? "—"}</ClockFact><ChevronRight size={16} aria-hidden="true" /></span>
                    </button>
                    {selectedNow ? <div className="narrative-stage__expanded"><FactList facts={view.facts} /></div> : null}
                  </li>
                );
              })}
            </ol>
          </section>
        </div>
        <aside className="variant-a__context" aria-label="所选阶段上下文">
          <div className="context-sticky">
            <header><small>所选阶段</small><strong>{selected.label}</strong><StatusGlyph lifecycle={selectedView.lifecycle} /></header>
            <section><small>一条主结论</small><h2>{selectedView.headline}</h2>{selected.key !== scenario.currentStage ? <FactList facts={selectedView.facts} /> : null}</section>
            {selected.key === scenario.currentStage ? <ActivityFacts scenario={scenario} updateCount={updateCount} /> : null}
            <p className="context-note">此栏只补充当前选择，不改变时间线节点高度；轮询更新不会推动阅读位置。</p>
          </div>
        </aside>
      </div>
    </main>
  );
}
