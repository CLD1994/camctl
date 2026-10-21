import { expect, it } from "vitest";
import {
  validateReport,
  validateReportAgainstHistory,
} from "../../src/domain/reports";
import { stringifyJson, parseJson } from "../../src/shared/json";
import {
  knowledgeReport,
  knowledgeCopies,
  atWatermark,
  permutations,
} from "../helpers/segment-knowledge";

for (const copies of [3, 4])
  for (let coordinate = 0; coordinate < 4; coordinate++) {
    const refs: (string | undefined)[][] = Array.from(
      { length: copies },
      () => [],
    );
    refs[1][coordinate] = "2";
    refs[2][coordinate] = "10";
    it.each(permutations(refs).map((p, i) => [i, p] as const))(
      `${copies}副本第${coordinate}侧矛盾排列 %i 一律拒绝`,
      (_i, order) => {
        const r = knowledgeReport([...order]);
        expect(() => validateReport(r)).toThrow(/邻接产物身份/);
      },
    );
  }
it.each(
  permutations([["2"], [, "10"], [, , "11"], [, , , "12"]]).map(
    (p, i) => [i, p] as const,
  ),
)("四份分别补齐两缺口左右侧排列 %i 合法且不改原文", (_i, copies) => {
  const r = knowledgeReport([...copies]);
  const before = stringifyJson(r);
  expect(() => validateReport(r)).not.toThrow();
  expect(stringifyJson(r)).toBe(before);
});
it.each([
  [[], [], []],
  [["2"], [], ["2"]],
  [["99"], [], ["99"]],
])("全省略/同ID/未知实体仍合法 %#", (a, b, c) => {
  const r = knowledgeReport([a, b, c]);
  if (a[0] === "99")
    for (const d of knowledgeCopies(r)) d.retained_output_ids[0] = "99";
  if (a[0] === "99") r.plans![0].actions![0].outputs = [];
  expect(() => validateReport(r)).not.toThrow();
});
it.each(["later", "covered", "same"])(
  "%s 两方向三副本不能漏过已知引用",
  (boundary) => {
    const a = knowledgeReport([[], ["2"]]);
    if (boundary === "covered") a.to_wm = 20;
    const b = atWatermark(
      knowledgeReport([[], ["10"]]),
      "2",
      boundary === "later" ? 10 : 0,
      boundary === "later" ? 20 : 10,
    );
    b.plans![0].actions![1].action_instance_id = "22";
    b.plans![0].actions![1].name = "另一取回";
    expect(() => validateReportAgainstHistory(a, b)).toThrow(/邻接产物身份/);
    expect(() => validateReportAgainstHistory(b, a)).toThrow(/邻接产物身份/);
  },
);
it("精确大整数和成员集合重排保持原token，数学相同可合并、不同拒绝", () => {
  const raw = stringifyJson(knowledgeReport([[], ["2"], ["2"]])).replaceAll(
    '"directory_number":1',
    '"directory_number":9007199254740993',
  );
  const r = parseJson(raw) as any;
  knowledgeCopies(r)[1].retained_output_ids.reverse();
  expect(() => validateReport(r)).not.toThrow();
  const wrong = parseJson(raw) as any;
  const other = knowledgeCopies(wrong)[1];
  other.gaps[0].start_key = parseJson(
    '{"directory_number":9007199254740992,"index":3}',
  ) as any;
  other.gaps[0].end_key = other.gaps[0].start_key;
  other.first_gap = other.gaps[0];
  expect(() => validateReport(wrong)).toThrow(/冻结缺口/);
});
