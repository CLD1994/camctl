import { Dialog } from "@base-ui/react/dialog";
import { useEffect, useRef, useState } from "react";
import type { ConflictSelectionSnapshot } from "./draft-conflicts";
import {
  presentDraftConflict,
  draftSectionLabels,
  type DraftSideSummary,
} from "./draft-conflict-presentation";
import { stringifyJson } from "../shared/json";
interface Props {
  snapshot: ConflictSelectionSnapshot;
  selectable: boolean;
  canCopy: boolean;
  busy: boolean;
  close: () => void;
  adopt: (snapshot: ConflictSelectionSnapshot) => void;
  replace: (snapshot: ConflictSelectionSnapshot) => void;
  copy: () => void;
  verify: () => void;
  finalFocus: () => HTMLElement | null | false;
}
export function DraftConflictDialog(props: Props) {
  const shown = presentDraftConflict(props.snapshot),
    focus = useRef<HTMLParagraphElement>(null);
  const [confirmation, setConfirmation] = useState<"adopt" | "replace">();
  const basis = stringifyJson(props.snapshot);
  const visibility = useRef({ active: true, basis });
  visibility.current.basis = basis;
  useEffect(
    () => () => {
      visibility.current.active = false;
    },
    [],
  );
  const qualified = () =>
    visibility.current.active && visibility.current.basis === basis;
  const close = () => {
    if (!qualified() || props.busy) return;
    visibility.current.active = false;
    props.close();
  };
  useEffect(() => setConfirmation(undefined), [basis]);
  return (
    <Dialog.Root
      open
      modal
      disablePointerDismissal
      onOpenChange={(open, details) => {
        if (open) return;
        if (props.busy) details.cancel();
        else close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Viewport className="modal-backdrop">
          <Dialog.Popup
            className="modal panel draft-conflict-dialog"
            initialFocus={focus}
            finalFocus={props.finalFocus}
          >
            <Dialog.Title>选择完整草稿版本</Dialog.Title>
            <Dialog.Description ref={focus} tabIndex={-1}>
              向原草稿的自动保存已暂停。本页完整输入仍保留在当前会话中，三个选择均处理整份内容。
            </Dialog.Description>
            <p role="status">
              已保存版本更新于 {shown.savedAt}。
              {props.selectable
                ? "可以确认采用或替换。"
                : "当前资格不足，请重新核实；原草稿终态不能覆盖。"}
            </p>
            <div className="draft-difference-sides">
              <Side title="本页版本" side={shown.local} />
              <Side title="已保存版本" side={shown.remote} />
            </div>
            <ul>
              {shown.differences.map((item) => (
                <li key={item.section}>
                  {draftSectionLabels[item.section]}：
                  {item.different ? "存在差异" : "一致"}
                </li>
              ))}
            </ul>
            <details>
              <summary>完整技术详情</summary>
              <div className="draft-difference-sides">
                <section>
                  <h3>本页全部资料</h3>
                  <pre>{shown.localDetails}</pre>
                </section>
                <section>
                  <h3>后端全部资料</h3>
                  <pre>{shown.remoteDetails}</pre>
                </section>
              </div>
            </details>
            {confirmation && (
              <section className="notice warning" role="status">
                <p>
                  {confirmation === "adopt"
                    ? "采用后将放弃本页全部未保存内容，包括停用类型、模式、未完成原文和自动预览资料。"
                    : "替换会把本页全部资料写入当前展示的后端版本；对方版本不另行归档。"}
                </p>
                <button
                  className="primary"
                  disabled={props.busy || !props.selectable}
                  onClick={() => {
                    if (qualified() && !props.busy && props.selectable)
                      (confirmation === "adopt" ? props.adopt : props.replace)(
                        props.snapshot,
                      );
                  }}
                >
                  确认
                  {confirmation === "adopt"
                    ? "使用已保存版本"
                    : "替换已保存版本"}
                </button>
                <button
                  disabled={props.busy}
                  onClick={() => setConfirmation(undefined)}
                >
                  返回比较
                </button>
              </section>
            )}
            <div className="button-row">
              <button
                className="primary"
                disabled={props.busy || !props.canCopy}
                onClick={() => {
                  if (qualified() && !props.busy && props.canCopy) props.copy();
                }}
              >
                将本页另存为新草稿（推荐）
              </button>
              <button
                disabled={props.busy || !props.selectable}
                onClick={() => {
                  if (qualified() && props.selectable) setConfirmation("adopt");
                }}
              >
                使用已保存版本
              </button>
              <button
                disabled={
                  props.busy ||
                  !props.selectable ||
                  !shown.differences.some((item) => item.different)
                }
                onClick={() => {
                  if (qualified() && props.selectable)
                    setConfirmation("replace");
                }}
              >
                用本页版本替换已保存版本
              </button>
              <button
                disabled={props.busy}
                onClick={() => {
                  if (qualified() && !props.busy) props.verify();
                }}
              >
                重新核实
              </button>
              <button disabled={props.busy} onClick={close}>
                关闭比较
              </button>
            </div>
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
function Side({ title, side }: { title: string; side: DraftSideSummary }) {
  return (
    <section>
      <h3>{title}</h3>
      <p>{side.title ?? "计划名称无法解释"}</p>
      {side.actions ? (
        <>
          <p>{side.actions.length} 个动作，按该版本自己的顺序列出：</p>
          <ol>
            {side.actions.map((action) => (
              <li key={action.index}>
                {action.name ?? "名称无法解释"}
                {action.type && `（${action.type}）`}
              </li>
            ))}
          </ol>
        </>
      ) : (
        <p role="status">{side.parseError}</p>
      )}
      <details>
        <summary>查看实际正文</summary>
        <pre>{side.raw}</pre>
      </details>
    </section>
  );
}
