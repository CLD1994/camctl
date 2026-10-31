import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { BuiltinFields } from "../../src/web/BuiltinFields";

function renderParams(params: Record<string, unknown>) {
  const content = {
    text: JSON.stringify({
      name: "取回计划",
      actions: [{ name: "取回", type: "obtain_action_outputs", params }],
    }),
  };
  const before = structuredClone(content);
  const html = renderToStaticMarkup(
    createElement(BuiltinFields, {
      content,
      path: ["actions", 0, "params"],
      type: "obtain_action_outputs",
      change: () => {
        throw new Error("显示参数不能改写草稿");
      },
      reports: [],
      coverage: 0,
    }),
  );
  expect(content).toEqual(before);
  return html;
}
it("保存的排除列表只显示排除项数量，并说明主机按默认范围应用", () => {
  const html = renderParams({
    source: { plan_instance_id: "8" },
    exclude_output_ids: ["8", "9", "10", "11", "12", "13", "14", "26", "28"],
  });
  const notice = html.match(/<div class="notice"><p>(.*?)<\/p>/)?.[1];
  expect(notice).toBeDefined();
  expect(notice).toContain("9 个产物排除项");
  expect(notice).toContain("默认取回范围");
});
it("没有排除列表时不显示移除列表按钮", () => {
  expect(renderParams({ source: { plan_instance_id: "8" } })).not.toMatch(
    /<button[^>]*>移除[^<]*列表<\/button>/,
  );
});
