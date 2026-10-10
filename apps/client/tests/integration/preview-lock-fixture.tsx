import { useState } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { Editor } from "../../src/web/Editor";
import { DraftSession, type DraftTransport } from "../../src/web/session";
import type { Draft, PreviewIntent } from "../../src/server/models";
import { initializePreviewMetadata } from "../../src/shared/automatic-previews";

export type PreviewLock =
  | "busy"
  | "exporting"
  | "export-unknown"
  | "exported"
  | "deleting"
  | "delete-unknown"
  | "deleted"
  | "append"
  | "identity";
export interface PreviewLockFixture {
  reset(intent: PreviewIntent): void;
  lock(state: PreviewLock, synchronous?: boolean): Promise<void>;
  snapshot(): { content: Draft["content"]; version: number; writes: number };
}
declare global {
  interface Window {
    previewLockFixture: PreviewLockFixture;
  }
}

const report = {
  name: "同步",
  type: "report_status",
  params: { scope: "full" },
};
let redraw = () => {};
let busy = false;
let writes = 0;
let session: DraftSession;
let stored: Draft;
const transport: DraftTransport = {
  async save(id, revision, content) {
    if (id !== stored.id || revision !== stored.revision)
      throw new Error("保存依据与测试存储不一致");
    writes++;
    stored = {
      ...stored,
      revision: revision + 1,
      content: structuredClone(content),
    };
    return structuredClone(stored);
  },
  async read(id) {
    if (id !== stored.id) throw new Error("草稿不存在");
    return structuredClone(stored);
  },
};
function reset(intent: PreviewIntent) {
  busy = false;
  writes = 0;
  stored = {
    id: crypto.randomUUID(),
    revision: 1,
    createdAt: "2026-10-11",
    updatedAt: "2026-10-11",
    content: { text: JSON.stringify({ name: "锁态检查", actions: [report] }) },
  };
  if (intent !== "unset")
    stored.content = initializePreviewMetadata(stored.content, intent, "lock");
  session = new DraftSession(stored, transport, () => redraw());
}
function Fixture() {
  const [, render] = useState(0);
  redraw = () => render((n) => n + 1);
  return (
    <Editor
      key={session.draft.id}
      session={session}
      busy={busy}
      capabilities={null}
      capabilityState={{ active: null, error: null, generation: 0 }}
      estimateReloadPhase="idle"
      presets={[]}
      reports={[]}
      coverage={0}
      onCopy={() => {}}
      onDelete={() => {}}
      onExport={() => {}}
      checkDeletion={() => {}}
      checkExport={() => {}}
      savePreset={async () => {
        throw new Error("该场景不保存预设");
      }}
    />
  );
}
window.previewLockFixture = {
  reset(intent) {
    flushSync(() => {
      reset(intent);
      redraw();
    });
  },
  async lock(state, synchronous = true) {
    const change = () => {
      if (state === "busy") {
        busy = true;
        redraw();
      } else if (state === "append") session.lockAppend();
      else if (state === "identity")
        session.accept({
          ...stored,
          content: {
            text: JSON.stringify({
              name: "锁态检查",
              actions: [report, { ...report, name: "新增" }],
            }),
          },
        });
      else if (state === "exporting") session.beginExport();
      else if (state === "export-unknown") {
        session.beginExport();
        session.exportUnknown();
      } else if (state === "exported") {
        session.beginExport();
        session.confirmExport("1");
      } else if (state === "deleting")
        void session.delete(
          () => new Promise<void>(() => {}),
          async () => stored,
        );
      else if (state === "delete-unknown")
        void session
          .delete(
            async () => {
              throw new Error("删除回执未知");
            },
            async () => {
              throw new Error("读取失败");
            },
          )
          .catch(() => {});
      else if (state === "deleted")
        void session.delete(
          async () => {},
          async () => undefined,
        );
    };
    if (synchronous) flushSync(change);
    else change();
    // delete() 会先 await flush()，让真实异步状态转换完成。
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  },
  snapshot() {
    return structuredClone({
      content: session.content,
      version: session.version,
      writes,
    });
  },
};
reset("unset");
createRoot(document.getElementById("root")!).render(<Fixture />);
