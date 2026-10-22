import { expect, it } from "vitest";
import {
  actionIssueText,
  resultNotes,
  resultProducts,
} from "../../src/web/result-model";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Facts } from "../../src/web/common";
import { parseJson } from "../../src/shared/json";
import { segmentReport, scopeReport } from "../helpers/segment-reports";

it("缺段说明定位来源、区间、邻接和保留原片", () => {
  const r = segmentReport();
  const a = r.plans![0].actions![0];
  const text = [actionIssueText(a), ...resultNotes(a).map((n) => n.text)].join(
    " ",
  );
  for (const fact of [
    "10",
    "目录 1/索引 3",
    "observed-left",
    "observed-right",
    "2",
    "4",
  ])
    expect(text).toContain(fact);
  expect(text).toMatch(/缺口|缺段/);
  expect(text).toMatch(/默认/);
  expect(text).toMatch(/保留/);
});
it.each(["empty", "unknown"] as const)(
  "范围%s来源失败说明具体状态和保留文件",
  (state) => {
    const r = scopeReport(state);
    const a = r.plans![0].actions![1];
    const text = resultNotes(a)
      .map((n) => n.text)
      .join(" ");
    expect(text).toContain("10");
    expect(text).toContain("4");
    expect(text).toMatch(
      state === "empty" ? /为空|没有.*连续/ : /未确认|无法确认/,
    );
    expect(text).toMatch(/保留/);
  },
);
it("来源失败与已报告原片保持独立，不因无本地文件隐藏", () => {
  const r = segmentReport();
  const a = r.plans![0].actions![0];
  expect(resultProducts(a, r.plans!, [])).toHaveLength(4);
  expect(
    resultProducts(a, r.plans!, []).every((p) => p.state === "waiting"),
  ).toBe(true);
  expect(a.status).toBe("failed");
  expect(actionIssueText(a)).toMatch(/缺口|缺段/);
});
it("Facts对象和数组数字保留原父容器词元", () => {
  const value = parseJson(
    '{"directory_number":9007199254740993,"values":[9007199254740993]}',
  );
  const html = renderToStaticMarkup(createElement(Facts, { value }));
  expect(html.match(/9007199254740993/g)).toHaveLength(2);
  expect(html).not.toContain("9007199254740992");
});
