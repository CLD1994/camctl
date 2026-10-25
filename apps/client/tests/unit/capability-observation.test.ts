import { expect, it } from "vitest";
import { stabilizeCapabilities } from "../../src/web/capability-observation";
import type { CapabilityState } from "../../src/shared/automatic-previews";

const state = (version?: string): CapabilityState => ({
  active: { devices: [] },
  error: null,
  generation: 1,
  version,
});

it("同版本规则复用对象，观察仍保留本次错误和代次", () => {
  const previous = state("v1"),
    incoming = state("v1");
  incoming.error = "本次重载失败";
  incoming.generation = 2;
  const result = stabilizeCapabilities(previous, incoming);
  expect(result.active).toBe(previous.active);
  expect(result.error).toBe("本次重载失败");
  expect(result.generation).toBe(2);
  expect(incoming.active).not.toBe(previous.active);
});

it.each([undefined, "", "v2"])("新观察版本 %s 不能复用旧规则", (version) => {
  const previous = state("v1"),
    incoming = state(version);
  expect(stabilizeCapabilities(previous, incoming)).toBe(incoming);
});

it("首次观察和能力可用性变化都消费新事实", () => {
  const available = state("v1"),
    unavailable = { ...state("v1"), active: null };
  expect(stabilizeCapabilities(undefined, available)).toBe(available);
  expect(stabilizeCapabilities(available, unavailable)).toBe(unavailable);
  expect(stabilizeCapabilities(unavailable, available)).toBe(available);
});
