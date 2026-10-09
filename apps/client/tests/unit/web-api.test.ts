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
