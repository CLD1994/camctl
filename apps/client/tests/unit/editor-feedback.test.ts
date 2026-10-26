import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import { Editor } from "../../src/web/Editor";
import { DraftSession } from "../../src/web/session";
import { record } from "../helpers/draft-conflicts";

function editorHtml(session: DraftSession) {
  return renderToStaticMarkup(
    createElement(Editor, {
      session,
      capabilities: null,
      capabilityState: { active: null, error: null, generation: 1 },
      estimateReloadPhase: "idle",
      presets: [],
      reports: [],
      coverage: 0,
      busy: false,
      onExport: vi.fn(),
      onDelete: vi.fn(),
      onCopy: vi.fn(),
      checkDeletion: vi.fn(),
      checkExport: vi.fn(),
      savePreset: vi.fn(),
    }),
  );
}
function session() {
  const draft = record(1, "草稿");
  return new DraftSession(
    draft,
    {
      save: async () => draft,
      read: async () => draft,
    },
    () => {},
  );
}
it("打开草稿不显示正常保存详情和导出校验汇总", () => {
  const html = editorHtml(session());
  expect(html).not.toContain('class="save-summary"');
  expect(html).not.toContain('data-testid="input-save-fact"');
  expect(html).not.toContain('data-testid="validation-issues"');
});
it("等待自动保存时不出现手动保存按钮和保存详情", () => {
  vi.useFakeTimers();
  const draft = session();
  try {
    draft.edit(record(1, "修改").content);
    draft.schedule();
    const html = editorHtml(draft);
    expect(html).not.toContain('class="save-summary"');
    expect(html).not.toContain("保存当前输入");
  } finally {
    vi.useRealTimers();
  }
});
it("尚未安排自动保存的待存输入保留显式保存入口", () => {
  const draft = session();
  draft.edit(record(1, "修改").content);
  expect(editorHtml(draft)).toContain("保存当前输入");
});
it("保存错误仍显示诊断和重试入口", () => {
  const draft = session();
  draft.edit(record(1, "修改").content);
  draft.error = "本地保存失败";
  const html = editorHtml(draft);
  expect(html).toContain("本地保存失败");
  expect(html).toContain("重试保存");
});
