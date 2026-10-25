import { expect, it } from "vitest";
import { createElement as h } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { parseJson } from "../../src/shared/json";
import type { Output } from "../../src/shared/types";
import { OutputFacts } from "../../src/web/OutputFacts";

const base = (): Output => ({
  output_id: "1",
  source_action_instance_id: "1",
  kind: "original",
  availability: "available",
  cleanup: { status: "not_requested" },
  checksum: { status: "not_obtained" },
  media: { check_status: "not_performed", duration: { status: "unknown" } },
});
const render = (output: Output) =>
  renderToStaticMarkup(h(OutputFacts, { output }));

it("未取得校验值和未检查媒体各自呈现为明确的字段和值", () => {
  const html = render(base());
  expect(html).toContain("<dt>源文件 SHA-256</dt><dd>尚未取得</dd>");
  expect(html).toContain("<dt>清理状态</dt><dd>未请求清理</dd>");
  expect(html).toContain("<dt>主机媒体检查</dt><dd>未执行</dd>");
  expect(html).toContain("<dt>媒体时长</dt><dd>未知</dd>");
  expect(html.match(/<dl\b/g)).toHaveLength(1);
  expect(html).not.toContain("check_status");
  expect(html).not.toContain("<dt>状态</dt>");
});

it.each(["0", "0.123456789012345678901", "9007199254740993"])(
  "已取得 SHA-256 和时长 %s 保留精确报告值",
  (seconds) => {
    const hash = "abc0123456789def".repeat(4);
    const output = parseJson(
      JSON.stringify(base())
        .replace(
          '"checksum":{"status":"not_obtained"}',
          `"checksum":{"status":"available","sha256":"${hash}"}`,
        )
        .replace(
          '"check_status":"not_performed","duration":{"status":"unknown"}',
          `"check_status":"completed","duration":{"status":"available","seconds":${seconds}}`,
        ),
    ) as Output;
    const html = render(output);
    expect(html).toContain(
      `<dt>源文件 SHA-256</dt><dd><code>${hash}</code></dd>`,
    );
    expect(html).toContain(
      `<dt>媒体时长</dt><dd><span>${seconds}</span> 秒</dd>`,
    );
    expect(html).toContain("<dt>主机媒体检查</dt><dd>已完成</dd>");
    expect(html).not.toContain("核验通过");
  },
);

it.each(["completed", "failed", "unconfirmed"] as const)(
  "媒体检查 %s 时保留问题及清理错误，不推断检查通过",
  (status) => {
    const output = base();
    const error = {
      code: "media_probe_failed",
      stage: "execution",
      details: { status: "running" },
    };
    output.media = {
      check_status: status,
      duration: { status: "unknown" },
      issues: [error],
      ...(status === "completed" ? {} : { error }),
    };
    output.cleanup = {
      status: "incomplete",
      error: { code: "cleanup_failed", stage: "execution", details: {} },
    };
    const html = render(output);
    expect(html).toContain("发现的媒体问题");
    expect(html).toContain("media_probe_failed");
    expect(html).toContain("清理错误");
    expect(html).toContain("cleanup_failed");
    expect(html).toContain(">running<");
    expect(html).not.toContain("核验通过");
    expect(html.includes("媒体检查错误")).toBe(status !== "completed");
  },
);
