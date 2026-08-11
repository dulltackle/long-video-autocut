import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, RefreshCw } from "lucide-react";
import { PreviewPanel } from "./components.jsx";
import { SCENARIOS } from "./data.js";
import VariantA from "./variants/VariantA.jsx";
import VariantB from "./variants/VariantB.jsx";
import VariantC from "./variants/VariantC.jsx";

const VARIANTS = [
  { key: "A", name: "纵向叙事" },
  { key: "B", name: "阶段控制台" },
  { key: "C", name: "审计长表" },
];
const VALID_VARIANTS = new Set(VARIANTS.map((item) => item.key));
const VALID_SCENARIOS = new Set(Object.keys(SCENARIOS));

function readRoute() {
  const params = new URLSearchParams(window.location.search);
  const variant = params.get("variant")?.toUpperCase();
  const scenario = params.get("scenario");
  return {
    variant: VALID_VARIANTS.has(variant) ? variant : "B",
    scenario: VALID_SCENARIOS.has(scenario) ? scenario : "live",
  };
}

function writeRoute(next, replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("variant", next.variant);
  url.searchParams.set("scenario", next.scenario);
  window.history[replace ? "replaceState" : "pushState"]({}, "", url);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function PrototypeSwitcher({ route, onRouteChange, onSimulate }) {
  const currentIndex = VARIANTS.findIndex((item) => item.key === route.variant);
  const cycle = (direction) => {
    const next = VARIANTS[(currentIndex + direction + VARIANTS.length) % VARIANTS.length];
    onRouteChange({ ...route, variant: next.key });
  };
  return (
    <div className="prototype-switcher" aria-label="原型控制器">
      <span className="prototype-label">PROTOTYPE</span>
      <button type="button" onClick={() => cycle(-1)} aria-label="上一个布局方案"><ArrowLeft size={16} /></button>
      <strong>{route.variant} — {VARIANTS[currentIndex].name}</strong>
      <button type="button" onClick={() => cycle(1)} aria-label="下一个布局方案"><ArrowRight size={16} /></button>
      <label>
        <span>状态场景</span>
        <select value={route.scenario} onChange={(event) => onRouteChange({ ...route, scenario: event.target.value })}>
          {Object.entries(SCENARIOS).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}
        </select>
      </label>
      <button className="simulate-button" type="button" onClick={onSimulate}><RefreshCw size={14} />模拟新事实</button>
    </div>
  );
}

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [updateCount, setUpdateCount] = useState(0);
  const [announcement, setAnnouncement] = useState("");
  const previewTriggerRef = useRef(null);
  const previewScrollRef = useRef(0);

  useEffect(() => {
    const syncRoute = () => setRoute(readRoute());
    window.addEventListener("popstate", syncRoute);
    return () => window.removeEventListener("popstate", syncRoute);
  }, []);

  const onRouteChange = useCallback((next) => {
    writeRoute(next);
    setPreviewOpen(false);
    setUpdateCount(0);
  }, []);

  useEffect(() => {
    const handleKey = (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if (event.target.closest("input, textarea, select, [contenteditable='true']")) return;
      const index = VARIANTS.findIndex((item) => item.key === route.variant);
      const direction = event.key === "ArrowRight" ? 1 : -1;
      const next = VARIANTS[(index + direction + VARIANTS.length) % VARIANTS.length];
      onRouteChange({ ...route, variant: next.key });
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onRouteChange, route]);

  const scenario = useMemo(() => {
    const base = SCENARIOS[route.scenario];
    if (route.scenario !== "live" || updateCount === 0) return base;
    if (updateCount > 1) {
      return {
        ...base,
        currentStage: "candidate_planning",
        lastEvent: "刚刚",
        elapsed: "约 15 分钟",
        stageHeadline: "候选规划已开始",
        progressFacts: ["候选规划开始事件已记录", "已形成 12 个候选窗口", "沿用已验证的整场转写"],
        stageSignals: [
          { label: "阶段推进", value: "候选规划开始事件已记录" },
          { label: "候选集合", value: "已形成 12 个候选窗口" },
          { label: "输入事实", value: "沿用已验证的整场转写" },
        ],
        newStageStarted: true,
      };
    }
    return {
      ...base,
      lastEvent: "刚刚",
      progressFacts: ["供应商请求完成 9 次", "当前在飞请求 0 次", "重试 2 次 · 退避等待累计 18 秒"],
      stageSignals: [
        { label: "供应商请求", value: "完成 9 次 · 当前在飞 0 次" },
        { label: "重试与退避", value: "重试 2 次 · 等待累计 18 秒" },
        { label: "缓存", value: "整场转写缓存未命中" },
      ],
    };
  }, [route.scenario, updateCount]);
  const commonProps = {
    scenario,
    updateCount,
    onOpenPreview: (event) => {
      previewTriggerRef.current = event.currentTarget;
      previewScrollRef.current = window.scrollY;
      setPreviewOpen(true);
    },
    onSelectScenario: (nextScenario) => onRouteChange({ ...route, scenario: nextScenario }),
  };
  const closePreview = useCallback(() => {
    setPreviewOpen(false);
    requestAnimationFrame(() => {
      window.scrollTo({ top: previewScrollRef.current, behavior: "instant" });
      previewTriggerRef.current?.focus({ preventScroll: true });
    });
  }, []);
  const simulate = () => {
    setUpdateCount((count) => count + 1);
    setAnnouncement(route.scenario === "live"
      ? updateCount === 0
        ? "已收到一条新的完整事实；阅读位置和所选阶段保持不变。"
        : "候选规划阶段已开始；所选阶段保持不变，可按提示查看当前阶段。"
      : "当前终态没有需要合并的新事实。所选阶段保持不变。");
  };

  return (
    <>
      {route.variant === "A" ? <VariantA key={route.scenario} {...commonProps} /> : null}
      {route.variant === "B" ? <VariantB key={route.scenario} {...commonProps} /> : null}
      {route.variant === "C" ? <VariantC key={route.scenario} {...commonProps} /> : null}
      <PreviewPanel open={previewOpen} onClose={closePreview} />
      {import.meta.env.DEV ? <PrototypeSwitcher route={route} onRouteChange={onRouteChange} onSimulate={simulate} /> : null}
      <div className="sr-only" aria-live="polite">{announcement}</div>
    </>
  );
}
