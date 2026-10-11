import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { useState } from "react";
import "../../src/web/style.css";
import { Editor } from "../../src/web/Editor";
import { DraftOperationNotice } from "../../src/web/DraftOperationNotice";
import { DraftSession } from "../../src/web/session";
import { DraftCopyOperation } from "../../src/web/draft-copy";
import { prepareCopyTarget } from "../../src/web/draft-copy-target";
import {
  bindSessionWholeView,
  identityForSession,
} from "../../src/web/draft-session-view";
import {
  parseClientJson,
  cloneClientJson,
  stringifyJson,
} from "../../src/shared/json";
import { record, creation } from "../helpers/draft-conflicts";
import type {
  Draft,
  DraftContent,
  DraftCreationResult,
} from "../../src/server/models";
type Kind = "copy" | "adopt" | "replace";
let redraw = () => {},
  source: DraftSession,
  displayed: DraftSession,
  backend: Draft;
let writes = 0,
  creates = 0,
  reads = 0,
  navigation = 0,
  viewCommits = 0;
let fault: "source" | "target" | "validate" | "navigation" | "none" = "source";
type CreationMode =
  "created" | "not_created" | "unknown" | "wrong_identity" | "wrong_input";
let creationMode: CreationMode = "created",
  navigationEpoch = 0;
let sourceDeleted = false, sourceUnavailable = false;
let holdRead = false,
  finishRead: (() => void) | undefined;
let result: DraftCreationResult;
const sessions = new Map<string, DraftSession>();
const complete = () =>
  parseClientJson(
    '{"text":"{\\"name\\":\\"local\\",\\"actions\\":[{\\"name\\":\\"A\\",\\"type\\":\\"report_status\\",\\"params\\":{\\"scope\\":\\"full\\"}}]}","pending":{"/amount":{"kind":"number","text":"-"}},"actionVariants":{"0":[{"type":"camera_record","fields":{"n":1.0000000000000001},"pending":{}}]},"parameterVariants":{"0":[{"paramsText":"{\\"params\\":{\\"type\\":\\"photo\\",\\"n\\":1e-999}}","pending":{}}]},"objectVariants":{"0":{"/x":{"discriminator":"mode","variants":[{"valueText":"{\\"value\\":{\\"mode\\":\\"manual\\"}}","pending":{}}]}}},"automaticPreviews":{"namespace":"source","next":1,"intent":"unset","actions":[{"id":"source:0"}]}}',
  ) as DraftContent;
function makeSession(draft: Draft) {
  const session = new DraftSession(
    draft,
    {
      save: async (_id, revision, content) => {
        writes++;
        backend = {
          ...backend,
          revision: revision + 1,
          content: cloneClientJson(content),
        };
        return backend;
      },
      read: async () => backend,
      readObservation: async () => sourceDeleted ? {kind: "deleted"} : sourceUnavailable ? {kind: "unavailable", message: "offline"} : {kind: "available", draft: backend},
    },
    () => redraw(),
  );
  bindSessionWholeView(session);
  return session;
}
function reset() {
  writes = creates = reads = navigation = viewCommits = 0;
  fault = "source";
  creationMode = "created";
  navigationEpoch = 0;
  sourceDeleted = sourceUnavailable = false;
  holdRead = false;
  finishRead = undefined;
  sessions.clear();
  backend = record(2, "remote");
  const initial = { ...record(1, "base"), content: complete() };
  initial.content.text = initial.content.text.replace("local", "base");
  source = makeSession(initial);
  source.edit(complete());
  source.observe(backend);
  displayed = source;
  sessions.set(source.draft.id, source);
}
const prepareTarget = (actual: Draft) => {
  if (fault === "target") throw Error("target view unavailable");
  const prepared = prepareCopyTarget(sessions, actual, makeSession);
  const epoch = navigationEpoch;
  if (fault === "navigation") navigationEpoch++;
  return {
    validate: () => {
      if (epoch !== navigationEpoch)
        throw Error("navigation changed during preparation");
      if (fault === "validate") {
        source.observe({ ...backend, revision: backend.revision + 1 });
      }
      prepared.validate();
    },
    commit: () => {
      prepared.commit();
      displayed = prepared.session;
      navigation++;
    },
  };
};
const transport = {
  create: async (
    attemptId: string,
    input: DraftContent,
  ): Promise<DraftCreationResult> => {
    if (creationMode === "unknown") {
      creates++;
      throw Error("creation receipt unknown");
    }
    if (creationMode === "not_created") {
      creates++;
      result = { kind: "not_created", attemptId };
      return result;
    }
    creates++;
    const first = creation(input);
    first.attemptId = attemptId;
    if (creationMode === "wrong_identity")
      first.attemptId = "different-attempt";
    if (creationMode === "wrong_input")
      first.input.pending = { "/amount": { kind: "number", text: "+" } };
    result = {
      kind: "created",
      creation: first,
      target: { kind: "editable", draft: cloneClientJson(first.created) },
    };
    return cloneClientJson(result);
  },
  read: async (): Promise<DraftCreationResult> => {
    reads++;
    if (creationMode === "unknown") throw Error("creation read unavailable");
    if (holdRead)
      await new Promise<void>((resolve) => {
        finishRead = resolve;
      });
    return cloneClientJson(result);
  },
};
export interface DraftConflictUiFixture {
  reset(): void;
  begin(kind: Kind): Promise<void>;
  fault(value: typeof fault): void;
  creationMode(value: CreationMode): void;
  beginWithoutPaint(kind: Kind): Promise<void>;
  addIndependent(): Promise<void>;
  observe(
    kind: "exported" | "deleted" | "changed" | "unavailable" | "same",
  ): void;
  terminalTarget(kind: "deleted" | "exported"): void;
  holdRead(value: boolean): void;
  finishRead(): void;
  snapshot(): {
    input: string;
    displayedInput: string;
    written: string;
    writes: number;
    creates: number;
    reads: number;
    navigation: number;
    viewCommits: number;
    pending: boolean;
    editable: boolean;
    warned: boolean;
    map: number;
  };
}
declare global {
  interface Window {
    draftConflictUiFixture: DraftConflictUiFixture;
  }
}
function Fixture() {
  const [, render] = useState(0);
  redraw = () => render((n) => n + 1);
  return (
    <>
      <DraftOperationNotice
        session={source}
        prepareTarget={prepareTarget}
        onConfirmation={() => redraw()}
        onSource={() => {
          displayed = source;
          redraw();
          requestAnimationFrame(() =>
            document
              .querySelector<HTMLElement>('[data-testid="draft-editor"]')
              ?.focus(),
          );
        }}
      />
      <Editor
        key={displayed.draft.id}
        session={displayed}
        busy={false}
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
          throw Error("unused");
        }}
      />
    </>
  );
}
window.draftConflictUiFixture = {
  reset: () =>
    flushSync(() => {
      reset();
      redraw();
    }),
  async begin(kind) {
    source.bindWholeView((input) => {
      if (fault === "source") throw Error("source view unavailable");
      const commit = identityForSession(source).prepareWhole(input);
      return () => {
        commit();
        viewCommits++;
      };
    });
    try {
      if (kind === "copy") {
        source.beginCopy(transport, "fixture-attempt");
        await source.retryCopy(source.copyDecision!, prepareTarget);
      } else if (kind === "adopt")
        source.adoptSavedVersion(source.prepareConflictSelection());
      else await source.replaceSavedVersion(source.prepareConflictSelection());
    } catch {
      /* 夹具由实际领域异常留下相应责任，错误在组件中呈现。 */
    }
    flushSync(() => redraw());
  },
  fault: (value) => {
    fault = value;
  },
  creationMode: (value) => {
    creationMode = value;
  },
  async addIndependent() {
    const input = complete();
    input.pending = { "/amount": { kind: "number", text: "independent--" } };
    const operation = new DraftCopyOperation(
      source.draft.id,
      source.version,
      input,
      {
        create: async () => {
          throw Error("unknown independent creation");
        },
        read: async () => {
          throw Error("offline");
        },
      },
      "independent-attempt",
    );
    await operation.advance();
    // 与任务五独立责任反例相同：夹具装配潜在并存责任，界面仍消费真实 getter。
    Reflect.get(source, "registerCopyRecovery").call(source, operation);
    flushSync(() => redraw());
  },
  async beginWithoutPaint(kind) {
    const render = redraw;
    redraw = () => {};
    try {
      if (kind === "copy") source.beginCopy(transport, "unpainted-attempt");
      else if (kind === "adopt") {
        source.bindWholeView(() => {
          throw Error("source view unavailable");
        });
        source.adoptSavedVersion(source.prepareConflictSelection());
      } else {
        source.bindWholeView(() => {
          throw Error("source view unavailable");
        });
        await source.replaceSavedVersion(source.prepareConflictSelection());
      }
    } catch {
      /* 保留真实领域责任，刻意尚未重绘。 */
    } finally {
      redraw = render;
    }
  },
  observe(kind) {
    if (kind === "deleted") { sourceDeleted = true; source.observeTarget({ kind: "deleted" }); }
    else if (kind === "unavailable") {
      sourceUnavailable = true;
      source.observeTarget({ kind: "unavailable", message: "offline" });
    } else {
      sourceUnavailable = false;
      backend = kind === "exported" ? {
              ...backend,
              revision: backend.revision + 1,
              exportedRequestId: "1",
            } : kind === "changed" ? { ...record(backend.revision + 1, "changed") } : backend;
      source.observe(backend);
    }
    flushSync(() => redraw());
  },
  terminalTarget(kind) {
    if (result.kind !== "created") throw Error("creation required");
    result = {
      ...result,
      target:
        kind === "deleted"
          ? { kind: "deleted" }
          : {
              kind: "exported",
              draft: {
                ...result.creation.created,
                revision: 2,
                exportedRequestId: "1",
              },
            },
    };
  },
  holdRead: (value) => {
    holdRead = value;
  },
  finishRead: () => {
    holdRead = false;
    finishRead?.();
  },
  snapshot: () => ({
    input: stringifyJson(source.content),
    displayedInput: stringifyJson(displayed.content),
    written: stringifyJson(backend.content),
    writes,
    creates,
    reads,
    navigation,
    viewCommits,
    pending: source.copyPendingDecision || source.selectionPendingDecision,
    editable: source.canEdit,
    warned: source.needsLeaveWarning,
    map: sessions.size,
  }),
};
reset();
createRoot(document.getElementById("root")!).render(<Fixture />);
