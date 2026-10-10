import type { Page } from "@playwright/test";

/** 未完成输入按原机器路径定位，不把界面语言作为持久身份。 */
export function pendingInput(page: Page, path: string) {
  return page.locator(
    `textarea[data-validation-pending="true"][data-validation-path=${JSON.stringify(path)}]`,
  );
}

/** 使用对应字段容器，允许多个未完成输入拥有相同按钮文案。 */
export function pendingAction(
  page: Page,
  path: string,
  action: "apply" | "clear",
) {
  return page
    .locator(`[data-pending-path=${JSON.stringify(path)}]`)
    .getByRole("button", {
      name: action === "apply" ? /^确认修改：/ : /^清除这项输入：/,
    });
}
