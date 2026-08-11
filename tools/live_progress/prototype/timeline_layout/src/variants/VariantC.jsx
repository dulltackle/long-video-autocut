import { useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, Rows3 } from "lucide-react";
import {
  ActivityFacts,
  DeliverySummary,
  FactList,
  RunOverview,
  StatusGlyph,
  TechnicalDisclosure,
  TerminalBanner,
  TopBar,
} from "../components.jsx";
import { RUNS, STAGES, getStageView } from "../data.js";

export default function VariantC({ scenario, updateCount, onOpenPreview }) {
  const [expanded, setExpanded] = useState(scenario.currentStage);
  useEffect(() => setExpanded(scenario.currentStage), [scenario.currentStage]);
  return (
    <main className="app-frame variant-c">
      <TopBar scenario={scenario} />
      <div className="variant-c__body">
        <nav className="run-strip" aria-label="直播拆条运行列表">
          {RUNS.map((run, index) => <button key={run.id} className={index === 0 ? "is-selected" : ""} type="button"><span><strong>{run.startedAt}</strong><small>{run.id}</small></span><span><b>{run.status}</b><small>{run.stage} · {run.duration}</small></span></button>)}
        </nav>
        <RunOverview scenario={scenario} />
        <TerminalBanner banner={scenario.banner} />
        <DeliverySummary delivery={scenario.delivery} onOpenPreview={onOpenPreview} />
        {scenario.status === "记录损坏" ? <div className="corrupt-strip" role="status"><AlertTriangle size={17} /><strong>记录损坏</strong><span>在 sequence 143 停止解析；下表只显示此前的可信事件前缀。</span><b>诊断完整性：不完整</b></div> : null}
        <section className="stage-ledger" aria-labelledby="stage-ledger-title">
          <header className="stage-ledger__heading"><div><Rows3 size={17} /><h1 id="stage-ledger-title">九阶段事实账</h1></div><p>固定列让主结论与辅助事实可以横向扫读。</p></header>
          <div className="stage-ledger__columns" aria-hidden="true"><span>#</span><span>阶段</span><span>主结论</span><span>辅助事实</span><span>状态 / 耗时</span></div>
          <ol>
            {STAGES.map((stage, index) => {
              const view = getStageView(stage, scenario);
              const isExpanded = expanded === stage.key;
              return (
                <li key={stage.key} className={`ledger-row ledger-row--${view.lifecycle} ${isExpanded ? "is-expanded" : ""}`}>
                  <button type="button" className="ledger-row__summary" onClick={() => setExpanded(isExpanded ? "" : stage.key)} aria-expanded={isExpanded}>
                    <span className="ledger-row__index">{index + 1}</span>
                    <strong className="ledger-row__stage">{stage.label}</strong>
                    <span className="ledger-row__headline">{view.headline}</span>
                    <span className="ledger-row__facts"><FactList facts={view.facts} /></span>
                    <span className="ledger-row__status"><StatusGlyph lifecycle={view.lifecycle} /><small>{view.duration ?? "—"}</small><ChevronDown className={isExpanded ? "is-rotated" : ""} size={15} /></span>
                  </button>
                  {isExpanded ? <div className="ledger-row__expanded">{stage.key !== scenario.currentStage ? <div><small>完整辅助事实</small><FactList facts={view.facts} /></div> : null}{stage.key === scenario.currentStage ? <ActivityFacts scenario={scenario} updateCount={updateCount} /> : null}{(view.lifecycle === "failed" || view.lifecycle === "interrupted" || view.lifecycle === "corrupt") ? <TechnicalDisclosure scenario={scenario} /> : null}</div> : null}
                </li>
              );
            })}
          </ol>
        </section>
      </div>
    </main>
  );
}
