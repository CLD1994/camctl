import type {
  Draft,
  DraftContent,
  DraftCreation,
} from "../../src/server/models";
import { cloneClientJson, parseClientJson } from "../../src/shared/json";
export function completeDraftInput(): DraftContent {
  return parseClientJson(
    '{"text":"{\\"name\\":\\"local\\",\\"actions\\":[{\\"name\\":\\"A\\",\\"type\\":\\"report_status\\",\\"params\\":{\\"scope\\":\\"full\\"}}]}","pending":{"/amount":{"kind":"number","text":"-"}},"actionVariants":{"0":[{"type":"camera_record","fields":{"n":1.0000000000000001},"pending":{}}]},"parameterVariants":{"0":[{"paramsText":"{\\"params\\":{\\"type\\":\\"photo\\",\\"n\\":1e-999}}","pending":{}}]},"objectVariants":{"0":{"/x":{"discriminator":"mode","variants":[{"valueText":"{\\"value\\":{\\"mode\\":\\"manual\\"}}","pending":{}}]}}},"automaticPreviews":{"namespace":"source","next":1,"intent":"unset","actions":[{"id":"source:0"}]}}',
  ) as DraftContent;
}
export function record(revision: number, name: string): Draft {
  return {
    id: "d1",
    revision,
    content: { text: JSON.stringify({ name, actions: [] }) },
    createdAt: "2026-10-11 10:00:00",
    updatedAt: "2026-10-11 10:00:00",
  };
}
export function creation(input: DraftContent): DraftCreation {
  return {
    attemptId: "copy-1",
    input: cloneClientJson(input),
    created: {
      ...record(1, ""),
      id: "copy-target",
      content: cloneClientJson(input),
    },
  };
}
