import { expect, it } from "vitest";
import { presentDraftConflict } from "../../src/web/draft-conflict-presentation";
import { record } from "../helpers/draft-conflicts";
import { cloneClientJson, parseClientJson } from "../../src/shared/json";
import type { DraftContent } from "../../src/server/models";
const sections = [
  "text",
  "pending",
  "actionVariants",
  "parameterVariants",
  "objectVariants",
  "automaticPreviews",
] as const;
const complete = parseClientJson(
  '{"text":"{\\"name\\":\\"计划\\",\\"actions\\":[{\\"name\\":\\"A\\",\\"type\\":\\"report_status\\"}]}","pending":{"":{"kind":"json","text":"["}},"actionVariants":{"0":[{"type":"camera_record","fields":{"n":1.0000000000000001},"pending":{}}]},"parameterVariants":{"0":[{"paramsText":"{\\"params\\":{\\"type\\":\\"photo\\"}}","pending":{}}]},"objectVariants":{"0":{"/x":{"discriminator":"mode","variants":[{"valueText":"{\\"value\\":{\\"mode\\":\\"manual\\",\\"n\\":1e-999}}","pending":{}}]}}},"automaticPreviews":{"namespace":"n","next":1,"intent":"unset","actions":[{"id":"n:0"}]}}',
) as DraftContent;
it.each(sections)("完整差异独立识别 %s，不按正文猜一致", (section) => {
  const remote = { ...record(2, "计划"), content: complete },
    local = cloneClientJson(complete);
  if (section === "text") local.text = '{"name":"另一份","actions":[]}';
  else delete local[section];
  const shown = presentDraftConflict({
    draftId: remote.id,
    localVersion: 3,
    local,
    remote,
    observationEpoch: 1,
  });
  expect(shown.differences.map((item) => item.section)).toEqual(sections);
  expect(
    shown.differences
      .filter((item) => item.different)
      .map((item) => item.section),
  ).toEqual([section]);
  expect(shown.savedAt).toBe(remote.updatedAt);
  expect(shown.remoteDetails).toContain("1.0000000000000001");
  expect(shown.remoteDetails).toContain("1e-999");
});
it("两侧各自保持动作顺序、同名、改名和非对象事实", () => {
  const remote = record(2, "remote");
  remote.content.text =
    '{"name":"远端","actions":[{"name":"same","type":"report_status"},{"name":"renamed"},4]}';
  const local = {
    text: '{"name":"本页<script>","actions":[null,{"name":"same"},{"name":"same"}]}',
  };
  const shown = presentDraftConflict({
    draftId: remote.id,
    localVersion: 1,
    local,
    remote,
    observationEpoch: 0,
  });
  expect(shown.local.title).toBe("本页<script>");
  expect(shown.local.actions?.map((item) => [item.index, item.name])).toEqual([
    [0, undefined],
    [1, "same"],
    [2, "same"],
  ]);
  expect(shown.remote.actions?.map((item) => [item.index, item.name])).toEqual([
    [0, "same"],
    [1, "renamed"],
    [2, undefined],
  ]);
  expect(shown.local.raw).toBe(local.text);
});
it("不可解析和未完成原文保持诊断，不生成空动作列表", () => {
  const remote = record(2, "remote"),
    local = {
      text: "{",
      pending: { "": { kind: "json" as const, text: "[" } },
    };
  const shown = presentDraftConflict({
    draftId: remote.id,
    localVersion: 1,
    local,
    remote,
    observationEpoch: 0,
  });
  expect(shown.local.actions).toBeUndefined();
  expect(shown.local.parseError).toBeTruthy();
  expect(shown.local.raw).toBe("{");
  expect(shown.localDetails).toContain('"["');
});
it("多个部分同时不同保留全部分类与原事实", () => {
  const remote = { ...record(2, "remote"), content: complete },
    local = { text: "[" };
  const shown = presentDraftConflict({
    draftId: remote.id,
    localVersion: 1,
    local,
    remote,
    observationEpoch: 0,
  });
  expect(shown.differences.every((item) => item.different)).toBe(true);
  expect(shown.remoteDetails).toContain('"automaticPreviews"');
});
