import { cleanup, render, screen } from "@testing-library/react";
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
