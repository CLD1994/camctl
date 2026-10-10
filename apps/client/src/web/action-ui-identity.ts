import type { DraftContent } from "../server/models";
import {
  validPreviewMetadata,
  sameContent,
} from "../shared/automatic-previews";
import { cloneClientJson } from "../shared/json";
import type { VerifiedAppend } from "./followup";
import { parseDraft } from "./editing";

type PositionChange = "preserve" | "append" | "reset" | { remove: number };
function structure(content: DraftContent) {
  // 未完成的整份输入没有可显示的动作结构，不将它解释为空数组。
  let count: number;
  try {
    count = parseDraft(content).actions.length;
  } catch {
    return undefined;
  }
  const metadata = content.automaticPreviews;
  return {
    count,
    ids:
      validPreviewMetadata(metadata) && metadata.actions.length === count
        ? metadata.actions.map((a) => a.id)
        : undefined,
  };
}

/** Editor 生命周期内的身份；不写入草稿，也不依赖动作内容或名称。 */
export class ActionUiIdentity {
  private sequence = 0;
  private previous?: ReturnType<typeof structure>;
  private content?: DraftContent;
  keys: string[] = [];
  constructor(content?: DraftContent) {
    if (content) this.update(content, "reset");
  }
  update(
    content: DraftContent,
    operation?: PositionChange,
    accept?: () => void,
    binding?: DraftContent,
    actual = content,
  ) {
    if (accept) {
      // 先在独立视图中校验映射；接纳拒绝时不改变现有键、折叠或中间身份。
      const prepared = this.copy();
      if (binding) prepared.update(binding, "preserve");
      prepared.update(content, operation);
      prepared.update(actual);
      accept();
      this.adopt(prepared);
      return;
    }
    const next = structure(content);
    if (!next) {
      if (operation === "reset") {
        this.keys = [];
        this.previous = undefined;
      }
      this.content = cloneClientJson(content);
      return;
    }
    const old = this.previous;
    const fresh = () => `ui-action-${this.sequence++}`;
    let keys: string[];
    if (!old || operation === "reset") {
      keys = Array.from({ length: next.count }, fresh);
    } else if (operation === "preserve") {
      if (old.count !== next.count) throw Error("局部编辑的动作位置映射不完整");
      keys = this.keys;
    } else if (old.ids && next.ids) {
      const byId = new Map(old.ids.map((id, i) => [id, this.keys[i]]));
      keys = next.ids.map((id) => byId.get(id) ?? fresh());
    } else if (operation === "append" && next.count === old.count + 1) {
      keys = [...this.keys, fresh()];
    } else if (typeof operation === "object" && next.count === old.count - 1) {
      keys = this.keys.filter((_, i) => i !== operation.remove);
    } else if (old.count === next.count) {
      keys = this.keys;
    } else {
      throw Error("动作结构发生变化，但没有可靠的身份或明确的位置映射");
    }
    this.keys = keys;
    this.previous = next;
    this.content = cloneClientJson(content);
  }
  private copy() {
    const prepared = new ActionUiIdentity();
    prepared.sequence = this.sequence;
    prepared.previous = this.previous;
    prepared.keys = [...this.keys];
    prepared.content = this.content;
    return prepared;
  }
  private adopt(prepared: ActionUiIdentity) {
    this.sequence = prepared.sequence;
    this.previous = prepared.previous;
    this.keys = prepared.keys;
    this.content = prepared.content;
  }
  prepareAppend(transition: VerifiedAppend): () => void {
    if (!this.content || !sameContent(this.content, transition.origin.content))
      throw Error("追加基线与当前动作视图不一致");
    const prepared = this.copy();
    const derived = (next: DraftContent) => {
      const nextStructure = structure(next);
      if (
        !sameContent(prepared.content!, next) &&
        !(prepared.previous?.ids && nextStructure?.ids)
      )
        throw Error("已追加实际版本的派生结构没有可靠身份映射");
      prepared.update(next);
    };
    for (const change of transition.baselineChanges)
      derived(change.actual.content);
    if (
      !sameContent(prepared.content!, transition.baseline.content) ||
      prepared.previous?.count !== transition.index
    )
      throw Error("追加位置与已核实基线链不一致");
    prepared.update(transition.appended, "append");
    // 派生变化必须由独立 ID 解释；完整相等只用于版本绑定，不用于猜测动作身份。
    for (const next of [
      transition.expected.content,
      transition.actual.content,
    ]) {
      derived(next);
    }
    return () => this.adopt(prepared);
  }
}
