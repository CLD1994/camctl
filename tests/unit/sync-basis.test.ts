import { expect, it } from "vitest";
import { isSyncBasis } from "../../src/shared/action-params";

it.each([
  [{ report_id: 4, to_wm: 20 }, 20, true],
  [{ report_id: 4, to_wm: 20 }, 21, true],
  [{ report_id: 4, to_wm: 20 }, 19, false],
  [{ report_id: 1, to_wm: 0 }, 0, true],
  [{ report_id: 0, to_wm: 0 }, 0, false],
  [{ report_id: 4, to_wm: -1 }, 20, false],
  [{ report_id: 4, to_wm: 20 }, undefined, false],
  [{ report_id: 4, to_wm: 20 }, "20", false],
] as const)(
  "同步依据由合法报告身份与完整覆盖范围共同决定：%j / %j",
  (report, coverage, expected) => {
    expect(isSyncBasis(report, coverage)).toBe(expected);
  },
);
