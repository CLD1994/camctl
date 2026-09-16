import { Select as BaseSelect } from "@base-ui/react/select";
import {
  Children,
  isValidElement,
  createContext,
  useContext,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";

const DisabledContext = createContext(false);

// 弹出选项通过 Portal 渲染，需要显式继承表单的禁用状态。
export function SelectFieldset({
  disabled,
  children,
  ...props
}: ComponentProps<"fieldset">) {
  const inherited = useContext(DisabledContext);
  const blocked = inherited || !!disabled;
  return (
    <DisabledContext.Provider value={blocked}>
      <fieldset {...props} disabled={blocked}>
        {children}
      </fieldset>
    </DisabledContext.Provider>
  );
}

export function Select({
  value,
  onValueChange,
  disabled = false,
  children,
  ...props
}: {
  value: string;
  onValueChange: (value: string) => void;
  disabled?: boolean;
  children: ReactNode;
  "aria-label": string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
}) {
  const inherited = useContext(DisabledContext);
  const blocked = disabled || inherited;
  const [open, setOpen] = useState(false);
  // 标签从同一组选项派生，关闭列表或恢复草稿时也能显示业务文案。
  const items = Children.toArray(children).map((child) => {
    if (!isValidElement<SelectItemProps>(child) || child.type !== SelectItem)
      throw new Error("Select 的子元素必须是 SelectItem");
    return { value: String(child.props.value), label: child.props.children };
  });
  // 禁用后清除展开状态，恢复编辑时不自动重新弹出。
  if (blocked && open) setOpen(false);
  return (
    <BaseSelect.Root
      value={value}
      items={items}
      disabled={blocked}
      open={open && !blocked}
      onOpenChange={setOpen}
      onValueChange={(next, details) => {
        // 动态选项重排时组件会尝试自动回退；草稿值只能由用户确认选项修改。
        // 空字符串是显式清空项，null 是组件内部的未匹配状态，二者不可合并。
        if (blocked || details.reason !== "item-press" || next === null) {
          details.cancel();
          return;
        }
        if (next !== value) onValueChange(next);
      }}
    >
      <BaseSelect.Trigger
        {...props}
        className="select-trigger"
        data-value={value}
        data-empty={value === "" ? "" : undefined}
      >
        <BaseSelect.Value className="select-value" />
        <BaseSelect.Icon className="select-icon">
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
            aria-hidden="true"
          >
            <path
              d="m4 6 4 4 4-4"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </BaseSelect.Icon>
      </BaseSelect.Trigger>
      <BaseSelect.Portal>
        <BaseSelect.Positioner
          className="select-positioner"
          alignItemWithTrigger={false}
          sideOffset={5}
          collisionPadding={12}
        >
          <BaseSelect.Popup className="select-content">
            <BaseSelect.ScrollUpArrow
              className="select-scroll"
              aria-hidden="true"
            >
              ▴
            </BaseSelect.ScrollUpArrow>
            <BaseSelect.List className="select-list">
              {children}
            </BaseSelect.List>
            <BaseSelect.ScrollDownArrow
              className="select-scroll"
              aria-hidden="true"
            >
              ▾
            </BaseSelect.ScrollDownArrow>
          </BaseSelect.Popup>
        </BaseSelect.Positioner>
      </BaseSelect.Portal>
    </BaseSelect.Root>
  );
}

type SelectItemProps = {
  value: string | number;
  disabled?: boolean;
  children: ReactNode;
};

export function SelectItem({ value, disabled, children }: SelectItemProps) {
  return (
    <BaseSelect.Item
      className="select-item"
      value={String(value)}
      disabled={disabled}
      data-value={String(value)}
    >
      <BaseSelect.ItemText className="select-item-text">
        {children}
      </BaseSelect.ItemText>
      <BaseSelect.ItemIndicator className="select-indicator" aria-hidden="true">
        ✓
      </BaseSelect.ItemIndicator>
    </BaseSelect.Item>
  );
}
