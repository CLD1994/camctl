import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import {
  PlanRecordList,
  recordListPage,
  pageForRecord,
} from "../../src/web/PlanRecordList";
const records = Array.from({ length: 23 }, (_, i) => ({ id: String(i + 1) }));
it("计划记录分页保持原顺序，每页最多十条", () => {
  expect(recordListPage(records, 2)).toEqual({
    page: 2,
    pageCount: 3,
    records: records.slice(10, 20),
  });
});
it("记录减少后收敛到最后有效页，空列表仍有合法页码", () => {
  expect(recordListPage(records.slice(0, 12), 3).page).toBe(2);
  expect(recordListPage([], 3)).toEqual({ page: 1, pageCount: 1, records: [] });
});
it("从其他入口进入指定记录时可定位所在页，缺失记录不猜测页码", () => {
  expect(pageForRecord(records, "21")).toBe(3);
  expect(pageForRecord(records, "missing")).toBeUndefined();
});
it("列表只渲染当前页，并显示总数和导航", () => {
  const html = renderToStaticMarkup(
    createElement(PlanRecordList, {
      records,
      page: 3,
      selectedId: "21",
      onOpen: vi.fn(),
      onPageChange: vi.fn(),
    }),
  );
  expect(html.match(/data-testid="record-open-button"/g)).toHaveLength(3);
  expect(html).toContain("23");
  expect(html).toContain("3 / 3");
  expect(html).toContain('aria-label="计划记录分页"');
  expect(html).toMatch(/<button[^>]*disabled[^>]*>下一页<\/button>/);
});
