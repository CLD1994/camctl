import { expect, type Page } from "@playwright/test";

const text = (value: string) => value.replace(/\s+/g, " ").trim();
type Field = { label: string; description?: string; object?: string };
type ObjectDescription = { name: string; description: string };
const readControls = (nodes: Element[]) =>
  nodes.map((node) => ({
    label: node.getAttribute("aria-label"),
    field: (node.closest(".field") as HTMLElement | null)?.innerText ?? "",
    object: node.closest(".object-branch-field")?.querySelector("h5 .field-key")
      ?.textContent,
  }));
const readObjects = (nodes: Element[]) =>
  nodes.map((node) => ({
    name: node.closest(".object-branch-field")?.querySelector("h5 .field-key")
      ?.textContent,
    description: (node as HTMLElement).innerText,
  }));
export async function checkVisibleFields(
  page: Page,
  fields: Field[],
  objects: ObjectDescription[],
) {
  await expect
    .poll(
      async () => {
        const [controls, visibleControls, descriptions, visibleDescriptions] =
          await Promise.all([
            page.locator("[aria-label]").evaluateAll(readControls),
            page.locator("[aria-label]:visible").evaluateAll(readControls),
            page
              .locator(".object-branch-field > .muted")
              .evaluateAll(readObjects),
            page
              .locator(".object-branch-field > .muted:visible")
              .evaluateAll(readObjects),
          ]);
        return {
          fields: fields.map((field) => ({
            label: field.label,
            count: controls.filter((control) => control.label === field.label)
              .length,
            visibleCount: visibleControls.filter(
              (control) =>
                control.label === field.label &&
                (field.object === undefined ||
                  control.object === field.object) &&
                (field.description === undefined ||
                  text(control.field).includes(text(field.description))),
            ).length,
          })),
          objects: objects.map((object) => ({
            name: object.name,
            count: descriptions.filter(
              (value) =>
                value.name === object.name &&
                text(value.description) === text(object.description),
            ).length,
            visibleCount: visibleDescriptions.filter(
              (value) =>
                value.name === object.name &&
                text(value.description) === text(object.description),
            ).length,
          })),
        };
      },
      { timeout: 5000 },
    )
    .toEqual({
      fields: fields.map(({ label }) => ({ label, count: 1, visibleCount: 1 })),
      objects: objects.map(({ name }) => ({ name, count: 1, visibleCount: 1 })),
    });
}

type GuideTask = {
  device: string;
  name: string;
  description: string;
  fields: { title: string; description: string }[];
};
const readGuide = (nodes: Element[]) =>
  nodes.map((node) => ({
    device: node
      .closest(".guide-device")
      ?.querySelector(".guide-device-heading h2")?.textContent,
    name: node.closest(".guide-task")?.querySelector("h3")?.textContent,
    title: node.querySelector("dt")?.textContent ?? "",
    text: (node as HTMLElement).innerText,
  }));
export async function checkVisibleGuide(page: Page, tasks: GuideTask[]) {
  await expect
    .poll(
      async () => {
        const [descriptions, visibleDescriptions, fields, visibleFields] =
          await Promise.all([
            page.locator(".guide-description").evaluateAll(readGuide),
            page.locator(".guide-description:visible").evaluateAll(readGuide),
            page.locator(".guide-fields > div").evaluateAll(readGuide),
            page.locator(".guide-fields > div:visible").evaluateAll(readGuide),
          ]);
        return tasks.map((task) => ({
          device: task.device,
          name: task.name,
          descriptionCount: descriptions.filter(
            (value) => value.device === task.device && value.name === task.name,
          ).length,
          matchingVisibleDescriptionCount: visibleDescriptions.filter(
            (value) =>
              value.device === task.device &&
              value.name === task.name &&
              text(value.text) === text(task.description),
          ).length,
          fields: task.fields.map((field) => ({
            title: field.title,
            count: fields.filter(
              (value) =>
                value.device === task.device &&
                value.name === task.name &&
                text(value.title).includes(text(field.title)),
            ).length,
            matchingVisibleCount: visibleFields.filter(
              (value) =>
                value.device === task.device &&
                value.name === task.name &&
                text(value.title).includes(text(field.title)) &&
                text(value.text).includes(text(field.description)),
            ).length,
          })),
        }));
      },
      { timeout: 5000 },
    )
    .toEqual(
      tasks.map((task) => ({
        device: task.device,
        name: task.name,
        descriptionCount: 1,
        matchingVisibleDescriptionCount: 1,
        fields: task.fields.map(({ title }) => ({
          title,
          count: 1,
          matchingVisibleCount: 1,
        })),
      })),
    );
}

export async function checkVisibleCleanupSupport(
  page: Page,
  expected: { device: string; text: string }[],
) {
  await expect
    .poll(
      async () => {
        const [deviceCount, all, visible] = await Promise.all([
          page.locator(".guide-device").count(),
          page.locator(".guide-cleanup-support strong").evaluateAll(readGuide),
          page
            .locator(".guide-cleanup-support strong:visible")
            .evaluateAll(readGuide),
        ]);
        return {
          deviceCount,
          counts: expected.map(({ device }) => ({
            device,
            count: all.filter((value) => value.device === device).length,
          })),
          support: visible
            .map(({ device, text }) => ({ device, text }))
            .sort((a, b) => String(a.device).localeCompare(String(b.device))),
        };
      },
      { timeout: 5000 },
    )
    .toEqual({
      deviceCount: expected.length,
      counts: expected.map(({ device }) => ({ device, count: 1 })),
      support: [...expected].sort((a, b) => a.device.localeCompare(b.device)),
    });
}
