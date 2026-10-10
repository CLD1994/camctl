import {
  cloneElement,
  createContext,
  useContext,
  useId,
  type ReactElement,
  type ReactNode,
} from "react";
import { pointer, type Path } from "./editing";
import type { PresentedIssue } from "./validation-presentation";
export type { PresentedIssue } from "./validation-presentation";

export const ValidationContext = createContext<PresentedIssue[]>([]);

/** 在真实控件上关联当前错误；JSON 控件同时负责其内部路径。 */
export function ValidationControl({
  path,
  json = false,
  pending = false,
  children,
}: {
  path: Path | string;
  json?: boolean;
  pending?: boolean;
  children: ReactElement;
}) {
  const id = useId(),
    target = typeof path === "string" ? path : pointer(path),
    all = useContext(ValidationContext);
  const matching = all.filter(
    ({ target: issueTarget, issue, json: requiresJson }) =>
      (json && target === "") ||
      (pending
        ? issue.code === "unfinished_input" && issue.path === target
        : (!requiresJson || json) &&
          issueTarget !== null &&
          (issueTarget === target ||
            (json && issueTarget.startsWith(target + "/")))),
  );
  const child = children as ReactElement<Record<string, unknown>>;
  const describedBy = [
    child.props["aria-describedby"],
    matching.length ? id : undefined,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <>
      {cloneElement(child, {
        "data-validation-path": target,
        "data-validation-json": json ? "true" : undefined,
        "data-validation-pending": pending ? "true" : undefined,
        "aria-invalid": matching.length ? true : undefined,
        "aria-describedby": describedBy || undefined,
      })}
      {matching.length > 0 && (
        <small id={id} className="field-error">
          {matching.map((item) => item.message).join("；")}
        </small>
      )}
    </>
  );
}

export function ValidationProvider({
  issues,
  children,
}: {
  issues: PresentedIssue[];
  children: ReactNode;
}) {
  return (
    <ValidationContext.Provider value={issues}>
      {children}
    </ValidationContext.Provider>
  );
}
