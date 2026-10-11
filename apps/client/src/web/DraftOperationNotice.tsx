import { Dialog } from "@base-ui/react/dialog";
import { useEffect, useRef, useState } from "react";
import type { Draft } from "../server/models";
import { stringifyJson } from "../shared/json";
import type { DraftSession } from "./session";
export function DraftOperationNotice({
  session,
  prepareTarget,
  onConfirmation,
  onSource,
  finalFocus,
}: {
  session: DraftSession;
  prepareTarget: (draft: Draft) => { validate: () => void; commit: () => void };
  onConfirmation: () => void;
  onSource: () => void;
  finalFocus?: () => HTMLElement | null;
}) {
  const copy = session.copyDecision,
    selection = session.selectionDecision;
  const independent = session.pendingCopyRecoveries.filter(
    (item) => item.id !== copy?.recoveryId,
  );
  const [open, setOpen] = useState(false),
    [error, setError] = useState("");
  const initialFocus = useRef<HTMLParagraphElement>(null),
    reopen = useRef<HTMLButtonElement>(null);
  const opened = useRef(new Set<string>());
  const visibility = useRef({ open: false, generation: 0 });
  useEffect(
    () => () => {
      visibility.current = {
        open: false,
        generation: visibility.current.generation + 1,
      };
    },
    [session],
  );
  const show = () => {
    visibility.current = {
      open: true,
      generation: visibility.current.generation + 1,
    };
    setOpen(true);
  };
  const hide = () => {
    visibility.current = {
      open: false,
      generation: visibility.current.generation + 1,
    };
    setOpen(false);
  };
  const presentation = visibility.current.generation;
  const identity = copy
    ? `copy:${copy.attemptId}:${copy.recoveryId}:${copy.generation}`
    : selection
      ? `${selection.kind}:${selection.sourceId}:${selection.generation}`
      : "";
  const running = copy?.requestRunning ?? selection?.requestRunning ?? false;
  useEffect(() => {
    if (!identity) {
      hide();
      return;
    }
    if (!running && !opened.current.has(identity)) {
      opened.current.add(identity);
      show();
    }
  }, [identity, running]);
  const recoveries = independent.length > 0 && (
    <section
      className="notice warning"
      data-testid="draft-independent-recoveries"
    >
      <p>
        草稿 {session.draft.id} 还有 {independent.length}{" "}
        份独立另存恢复输入，各份责任单独保留。
      </p>
      {independent.map((item) => (
        <details key={item.id}>
          <summary>恢复输入 {item.id}</summary>
          <pre>{stringifyJson(item.input, 2)}</pre>
        </details>
      ))}
      <button onClick={onSource}>查看原会话完整输入</button>
    </section>
  );
  if (!copy && !selection) return recoveries || null;
  const title = copy
    ? "另存"
    : selection!.kind === "adopt"
      ? "采用已保存版本"
      : "替换已保存版本";
  const retry = async () => {
    if (
      !visibility.current.open ||
      visibility.current.generation !== presentation
    )
      return;
    setError("");
    try {
      if (copy) await session.retryCopy(copy, prepareTarget);
      else if ((await session.retrySelection(selection)) === "confirmation") {
        hide();
        onConfirmation();
      }
    } catch (failure) {
      setError((failure as Error).message);
    }
  };
  const abandon = () => {
    if (
      !visibility.current.open ||
      visibility.current.generation !== presentation
    )
      return;
    setError("");
    try {
      if (copy) session.abandonCopy(copy);
      else session.abandonSelection(selection);
      hide();
      onSource();
    } catch (failure) {
      setError((failure as Error).message);
    }
  };
  const canRetry = copy?.canRetry ?? selection!.canRetry,
    canAbandon = copy?.canAbandon ?? selection!.canAbandon;
  const content = copy?.input ?? selection!.input;
  return (
    <>
      {recoveries}
      <section
        className="notice warning draft-operation-notice"
        data-testid="draft-operation-notice"
      >
        <p role="status">
          草稿 {session.draft.id} 的{title}尚待处理。
          {running
            ? "正在执行或核实，不能重复操作或关闭。"
            : "隐藏弹窗仍保留固定输入及待决定限制。"}
        </p>
        <button ref={reopen} onClick={show}>
          重新打开{title}异常
        </button>
        <details>
          <summary>查看所选操作保留的完整输入</summary>
          <pre>{stringifyJson(content, 2)}</pre>
        </details>
        <Dialog.Root
          open={open}
          modal
          disablePointerDismissal
          onOpenChange={(next, details) => {
            if (next) show();
            else if (
              session.copyDecision?.requestRunning ||
              session.selectionDecision?.requestRunning
            )
              details.cancel();
            else hide();
          }}
        >
          <Dialog.Portal>
            <Dialog.Viewport className="modal-backdrop">
              <Dialog.Popup
                className="modal panel draft-operation-dialog"
                initialFocus={initialFocus}
                finalFocus={() =>
                  session.copyPendingDecision ||
                  session.selectionPendingDecision
                    ? reopen.current
                    : finalFocus ? finalFocus() : document.querySelector<HTMLElement>(
                        '[data-testid="draft-editor"]',
                      )
                }
              >
                <Dialog.Title>{title}需要继续处理</Dialog.Title>
                <Dialog.Description ref={initialFocus} tabIndex={-1}>
                  完整输入仍由原会话保留。重试继续所选操作，放弃停止本次安排或本地处理并保留输入及真实写入。
                </Dialog.Description>
                <div role="status" aria-live="polite">
                  {running && (
                    <p>
                      请求或核实仍在执行，两个操作暂不可用，Escape 不能关闭。
                    </p>
                  )}
                  {copy && (
                    <p>
                      创建阶段：{copy.phase}。
                      {copy.phase === "unknown" || copy.phase === "invalid"
                        ? "当前创建或目标依据仍须按固定身份核实，此前真实创建事实保持。"
                        : copy.targetKind === "editable"
                          ? "新草稿已经可靠创建，仅需完成本地转交。"
                          : copy.targetKind === "deleted" ||
                              copy.targetKind === "exported"
                            ? "新草稿已经可靠创建，当前目标已删除或导出，创建核实已结束。"
                            : copy.phase === "not_created"
                              ? "已可靠确认未创建，重试将以同身份和完整输入创建。"
                              : "创建结果仍须按固定身份核实。"}
                    </p>
                  )}
                  {copy?.retryCreatesNewAttempt && (
                    <p>
                      点击重试将使用保留的完整输入独立另存一份新草稿，以新身份创建；原创建及目标终态保持。
                    </p>
                  )}
                  {selection?.written && (
                    <p>
                      后端已经可靠写入版本 {selection.written.revision}
                      ；重试只完成本地接纳，放弃不会撤销该写入。
                    </p>
                  )}
                  {selection && !selection.written && (
                    <p>
                      {selection.phase === "unknown"
                        ? "替换结果仍未知，重试只核实固定责任。"
                        : "本地接纳尚未完成；确认资格变化时，重试将回到完整差异和新确认。"}
                    </p>
                  )}
                  {!canAbandon && <p>放弃暂不可用，需要先重试核实可靠依据。</p>}
                  {(error ||
                    session.error ||
                    session.copyOperation?.diagnostic) && (
                    <p>
                      {error ||
                        session.error ||
                        session.copyOperation?.diagnostic}
                    </p>
                  )}
                </div>
                <div className="button-row">
                  <button
                    className="primary"
                    disabled={!canRetry}
                    onClick={() => void retry()}
                  >
                    重试
                  </button>
                  <button disabled={!canAbandon} onClick={abandon}>
                    放弃
                  </button>
                </div>
              </Dialog.Popup>
            </Dialog.Viewport>
          </Dialog.Portal>
        </Dialog.Root>
      </section>
    </>
  );
}
