import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement as h } from "react";
import { Facts, Badge, label, Issues } from "../../src/web/common";
it("通用事实保留ID、参数、Schema字面值与原始类型", () => {
  const html = renderToStaticMarkup(
    h(Facts, {
      value: {
        source_action_instance_id: "pending",
        input_params: { status: "failed", type: "running" },
        schema: { enum: ["failed", "camera_record"], default: false },
        count: 0,
        none: null,
      },
    }),
  );
  for (const literal of [
    "pending",
    "failed",
    "running",
    "camera_record",
    "false",
    ">0<",
    "null",
  ])
    expect(html).toContain(literal);
  expect(renderToStaticMarkup(h(Badge, { value: "pending" }))).not.toContain(
    ">pending<",
  );
});
it("未登记词与字段仅按字典自身属性匹配", () => {
  expect(label("toString")).toBe("toString");
  const html = renderToStaticMarkup(
    h(Facts, { value: { toString: "toString" } }),
  );
  expect(html.match(/toString/g)).toHaveLength(2);
  expect(
    renderToStaticMarkup(
      h(Issues, {
        issues: [{ path: "/p", code: "toString", message: "raw-message" }],
      }),
    ),
  ).toContain("raw-message");
});
it("明确结果状态本地化而details和未知结构中的同名键保真", () => {
  const html = renderToStaticMarkup(
    h(Facts, {
      business: true,
      value: {
        status: "pending",
        items: [
          {
            status: "failed",
            source_action_instance_id: "pending",
            error: { code: "failed", details: { status: "running" } },
          },
        ],
        details: { status: "running" },
        custom: { status: "pending" },
        input_params: { type: "running" },
        schema: { enum: ["failed"] },
      },
    }),
  );
  expect(
    renderToStaticMarkup(
      h(Facts, { business: true, value: { status: "pending" } }),
    ),
  ).not.toContain(">pending<");
  expect(html).toContain(">failed<");
  expect(html).toContain(">running<");
  expect(html).toContain(">pending<");
});
