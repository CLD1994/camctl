import { it, expect } from "vitest";
import { editPlanText, setValue } from "../../src/web/editing";
import {
  initializePreviewMetadata,
  previewIntent,
  copyDraftAction,
} from "../../src/shared/automatic-previews";
import { parseClientJson, stringifyJson } from "../../src/shared/json";
it("整份替换有预览资料时必须明确确认，确认后原文与尚未设置一起保留", () => {
  const c = initializePreviewMetadata(
    { text: '{"name":"旧","actions":[]}' },
    "disabled",
    "test",
  );
  expect(() => editPlanText(c, '{"name":')).toThrow();
  const replaced = editPlanText(c, '{"name":', true);
  expect(replaced.text).toBe('{"name":');
  expect(previewIntent(replaced)).toBe("unset");
  expect(c.automaticPreviews!.intent).toBe("disabled");
});
it("局部参数编辑及动作复制保留意图和原数字词元，复制身份独立", () => {
  const c = initializePreviewMetadata(
    {
      text: '{"name":"旧","actions":[{"name":"A","type":"camera_record","params":{"measurement":1.0000000000000001},"policy":{"max_delay_ms":0}}]}',
    },
    "disabled",
    "test",
  );
  const edited = setValue(c, ["actions", 0, "name"], "B");
  const copied = copyDraftAction(edited, 0);
  expect(previewIntent(copied)).toBe("disabled");
  expect(copied.automaticPreviews!.actions[0].id).not.toBe(
    copied.automaticPreviews!.actions[1].id,
  );
  const p = parseClientJson(copied.text) as any;
  expect(stringifyJson(p.actions[1].params)).toContain("1.0000000000000001");
});
