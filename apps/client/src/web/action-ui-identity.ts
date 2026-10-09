import type { DraftContent } from "../server/models";
import { validPreviewMetadata } from "../shared/automatic-previews";
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
  keys: string[] = [];
  constructor(content?: DraftContent) {
    if (content) this.update(content, "reset");
  }
  update(
    content: DraftContent,
    operation?: PositionChange,
    accept?: () => DraftContent,
    binding?: DraftContent,
  ) {
    if (accept) {
      // 先在独立视图中校验映射；接纳拒绝时不改变现有键、折叠或中间身份。
      const prepared = new ActionUiIdentity();
      prepared.sequence = this.sequence;
      prepared.previous = this.previous;
      prepared.keys = [...this.keys];
      if (binding) prepared.update(binding, "preserve");
      prepared.update(content, operation);
      prepared.update(accept());
      this.sequence = prepared.sequence;
      this.previous = prepared.previous;
      this.keys = prepared.keys;
      return;
    }
    const next = structure(content);
    if (!next) {
      if (operation === "reset") {
        this.keys = [];
        this.previous = undefined;
      }
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
  }
}
