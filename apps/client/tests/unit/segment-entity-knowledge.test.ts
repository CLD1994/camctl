import { expect, it } from "vitest";
import {
  SegmentReportKnowledge,
  validateReport,
} from "../../src/domain/reports";
import { stringifyJson, parseJson } from "../../src/shared/json";
import {
  permutations,
  segmentEntitiesOnly,
  segmentFactsOnly,
  type SegmentEntityCase,
} from "../helpers/segment-knowledge";
for (const kind of [
  "photo",
  "timelapse",
  "preview",
  "repaired",
  "wrong-source",
] as SegmentEntityCase[]) {
  const orders = permutations(["F", "bad", "good"] as const);
  it.each(orders.map((order, i) => [i, order] as const))(
    `${kind} 的每项实体观察均保留，排列 %i`,
    (_i, order) => {
      const reports = {
        F: segmentFactsOnly(),
        bad: segmentEntitiesOnly(kind),
        good: segmentEntitiesOnly(
          kind === "photo" || kind === "timelapse" ? "record" : "original",
        ),
      };
      const before = stringifyJson(reports);
      const knowledge = new SegmentReportKnowledge();
      for (const name of order) {
        validateReport(reports[name]);
        knowledge.add(reports[name]);
      }
      expect(() => knowledge.validate()).toThrow(/分段.*(来源|保留产物)/);
      expect(stringifyJson(reports)).toBe(before);
    },
  );
}
it.each([
  "record",
  "source-absent",
  "original",
  "output-absent",
] as SegmentEntityCase[])("%s 兼容精确F且保留未知引用与原对象", (kind) => {
  const facts = parseJson(
    stringifyJson(segmentFactsOnly()).replaceAll(
      '"directory_number":1',
      '"directory_number":9007199254740993',
    ),
  ) as any;
  const before = stringifyJson(facts);
  const knowledge = new SegmentReportKnowledge();
  knowledge.add(segmentEntitiesOnly(kind));
  knowledge.add(facts);
  expect(() => knowledge.validate()).not.toThrow();
  expect(stringifyJson(facts)).toBe(before);
});
it("未被分段引用的普通实体观察不新增全历史一致性审计", () => {
  const knowledge = new SegmentReportKnowledge();
  for (const kind of [
    "record",
    "photo",
    "preview",
    "original",
  ] as SegmentEntityCase[])
    knowledge.add(segmentEntitiesOnly(kind));
  expect(() => knowledge.validate()).not.toThrow();
});

it.each([
  "photo",
  "timelapse",
  "preview",
  "repaired",
  "wrong-source",
] as SegmentEntityCase[])("同份%s关联反证在公开报告入口拒绝", (kind) => {
  const report = segmentFactsOnly();
  report.plans![0].actions!.push(
    ...segmentEntitiesOnly(kind).plans![0].actions!,
  );
  expect(() => validateReport(report)).toThrow(/分段.*(来源|保留产物)/);
});

it("已有F时未被分段引用的实体反证仍不触发审计", () => {
  const photo = segmentEntitiesOnly("photo");
  photo.plans![0].actions![0].action_instance_id = "30";
  const preview = segmentEntitiesOnly("preview");
  const source = preview.plans![0].actions![0];
  source.action_instance_id = "30";
  source.outputs![0].source_action_instance_id = "30";
  source.outputs![0].output_id = "99";
  source.outputs![0].preview_of_output_id = "98";
  const knowledge = new SegmentReportKnowledge();
  for (const report of [photo, preview, segmentFactsOnly()]) {
    validateReport(report);
    knowledge.add(report);
  }
  expect(() => knowledge.validate()).not.toThrow();
});
