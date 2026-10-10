import { parseClientJson, stringifyJson } from "../../src/shared/json";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../../src/web/api";

afterEach(() => vi.unstubAllGlobals());
it("API 响应恢复草稿的非法原文，不提前执行公共计划校验", async () => {
  const value = {
    drafts: [
      {
        content: {
          text: '{"name":"\ud800"}',
          pending: { "/actions/0/params": { kind: "json", text: '"\udc00' } },
        },
      },
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(
      async () => new Response(JSON.stringify(value), { status: 200 }),
    ),
  );
  expect(await api("/state")).toEqual(value);
});

it("API 实际请求体与响应保留非整数词元", async () => {
  const input = parseClientJson('{"params":{"value":1.0000000000000001}}');
  const fetcher = vi.fn<typeof fetch>(async (_url, options) => {
    expect(options?.body).toContain("1.0000000000000001");
    return new Response(String(options?.body));
  });
  vi.stubGlobal("fetch", fetcher);
  const output = await api("/presets", "POST", input);
  expect(stringifyJson(output)).toContain("1.0000000000000001");
});
