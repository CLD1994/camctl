import { Menu } from "@base-ui/react/menu";
import { useId, useRef, useState } from "react";
import type { PreviewIntent } from "../server/models";

export function PreviewControl({
  intent,
  disabled,
  hasAutomatic,
  issues,
  onChoose,
}: {
  intent: PreviewIntent;
  disabled: boolean;
  hasAutomatic: boolean;
  issues: string[];
  onChoose: (intent: "enabled" | "disabled") => void;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const description = useId();
  const menuDisabled = disabled || intent !== "unset";
  // 锁态与已确定意图都关闭菜单；重新恢复编辑也不自动弹出。
  if (menuDisabled && open) setOpen(false);
  const label =
    intent === "enabled"
      ? "已启用预览"
      : intent === "disabled"
        ? "已禁用预览"
        : "设置自动预览（尚未设置）";
  const next = intent === "enabled" ? "disabled" : "enabled";
  return (
    <section className="preview-control" aria-label="自动获取预览文件">
      <Menu.Root
        disabled={menuDisabled}
        open={open && !menuDisabled}
        onOpenChange={(value) => setOpen(value && !menuDisabled)}
      >
        {intent === "unset" ? (
          <Menu.Trigger
            ref={trigger}
            disabled={disabled}
            data-testid="preview-intent"
            aria-describedby={description}
          >
            {label}
          </Menu.Trigger>
        ) : (
          <button
            ref={(element) => {
              trigger.current = element;
            }}
            data-testid="preview-intent"
            disabled={disabled}
            aria-describedby={description}
            onClick={() => {
              if (!disabled) onChoose(next);
            }}
          >
            {label}
          </button>
        )}
        <Menu.Portal>
          <Menu.Positioner
            className="preview-menu-positioner"
            align="end"
            sideOffset={5}
            collisionPadding={12}
          >
            <Menu.Popup className="preview-menu-popup" finalFocus={trigger}>
              <Menu.Item
                className="preview-menu-item"
                disabled={menuDisabled}
                onClick={() => {
                  if (!menuDisabled) onChoose("enabled");
                }}
              >
                启用预览
              </Menu.Item>
              <Menu.Item
                className="preview-menu-item"
                disabled={menuDisabled}
                onClick={() => {
                  if (!menuDisabled) onChoose("disabled");
                }}
              >
                禁用预览
              </Menu.Item>
            </Menu.Popup>
          </Menu.Positioner>
        </Menu.Portal>
      </Menu.Root>
      <span id={description} className="sr-only">
        {intent === "unset"
          ? "打开菜单后明确选择启用或禁用。"
          : intent === "enabled"
            ? "点击禁用本计划的自动预览取回。"
            : "点击启用本计划的自动预览取回。"}
        手动取回独立保留。
      </span>
      <span
        data-testid="preview-status"
        className="preview-status"
        role="status"
      >
        <span className="sr-only">{label}。</span>
        {intent === "unset"
          ? "选择是否自动取回本计划的预览文件。"
          : issues.length
            ? "自动预览关联待核实。"
            : intent === "disabled"
              ? "手动取回独立保留。"
              : hasAutomatic
                ? "本计划已包含自动取回预览文件的任务。"
                : "添加支持预览的拍摄任务后，将自动添加预览取回任务。"}
      </span>
      {issues.length > 0 && (
        <div data-testid="preview-diagnostics" className="preview-diagnostics">
          {issues.map((message, i) => (
            <p key={i} className="warning">
              {message}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}
