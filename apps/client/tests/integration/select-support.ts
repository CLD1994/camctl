import { expect, type Locator } from "@playwright/test";

/** 通过真实鼠标交互选值，覆盖弹出列表的可达性。 */
export async function choose(select: Locator, value: string) {
  await select.click();
  const list = select.page().getByRole("listbox");
  await expect(list).toBeVisible();
  const option = list.locator(
    `[role="option"][data-value=${JSON.stringify(value)}]`,
  );
  const label = await option.locator(".select-item-text").innerText();
  await option.click();
  await expect(select).toHaveAttribute("data-value", value);
  await expect(select).toContainText(label);
}

/** 展开后读取用户可见的选项，用于验证动态能力筛选和非法旧值。 */
export async function readOptions(select: Locator) {
  await select.click();
  const list = select.page().getByRole("listbox");
  await expect(list).toBeVisible();
  const options = await list.getByRole("option").evaluateAll((items) =>
    items.map((item) => ({
      value: item.getAttribute("data-value"),
      text: item.querySelector(".select-item-text")?.textContent,
      disabled: item.getAttribute("aria-disabled") === "true",
      selected: item.getAttribute("aria-selected") === "true",
    })),
  );
  await select.page().keyboard.press("Escape");
  await expect(list).toBeHidden();
  return options;
}
