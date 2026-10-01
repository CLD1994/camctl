import { describe, expect, it } from "vitest";

import {
  CryptoRandomSource,
  MAX_REQUEST_ID,
  MAX_ID_RESELECTIONS,
  RequestIdExhaustedError,
  isValidCanonicalId,
  newRequestId,
  type RandomSource,
  type RequestIdLookup,
} from "../../src/domain/request-id";
import { formatProtocolTime, isUtcText, utcTextFromDate } from "../../src/domain/protocol-time";

class SequenceRandom implements RandomSource {
  constructor(private readonly values: bigint[]) {}
  next(): bigint {
    const value = this.values.shift();
    if (value === undefined) {
      throw new Error("随机源耗尽");
    }
    return value;
  }
}

class SetLookup implements RequestIdLookup {
  constructor(private readonly used: Set<bigint>, private readonly fail = false) {}
  async isUsed(id: bigint): Promise<boolean> {
    if (this.fail) {
      throw new Error("查询失败");
    }
    return this.used.has(id);
  }
}

describe("请求身份分配", () => {
  it("均匀随机源产生合法身份并保持规范字符串", async () => {
    const random = new SequenceRandom([42n]);
    const id = await newRequestId(random, new SetLookup(new Set()));
    expect(id).toBe("42");
    expect(isValidCanonicalId(id)).toBe(true);
  });

  it("冲突时有限重选后成功", async () => {
    const random = new SequenceRandom([1n, 2n, 3n]);
    const id = await newRequestId(random, new SetLookup(new Set([1n, 2n])));
    expect(id).toBe("3");
  });

  it("重选耗尽返回导出错误而非默认 ID", async () => {
    const values: bigint[] = [];
    for (let index = 0; index <= MAX_ID_RESELECTIONS; index += 1) {
      values.push(BigInt(index + 1));
    }
    const allUsed = new Set(values);
    await expect(
      newRequestId(new SequenceRandom(values), new SetLookup(allUsed)),
    ).rejects.toBeInstanceOf(RequestIdExhaustedError);
  });

  it("查询失败不当作未使用", async () => {
    await expect(
      newRequestId(new SequenceRandom([5n]), new SetLookup(new Set(), true)),
    ).rejects.toThrow(/不能当作未使用/);
  });

  it("越界身份拒绝", async () => {
    await expect(
      newRequestId(new SequenceRandom([0n]), new SetLookup(new Set())),
    ).rejects.toThrow(/越界/);
    await expect(
      newRequestId(new SequenceRandom([MAX_REQUEST_ID + 1n]), new SetLookup(new Set())),
    ).rejects.toThrow(/越界/);
  });

  it("最大合法值可用且不经 Number", async () => {
    const id = await newRequestId(
      new SequenceRandom([MAX_REQUEST_ID]),
      new SetLookup(new Set()),
    );
    expect(id).toBe("9223372036854775807");
  });

  it("随机源分布内均为正 63 位", () => {
    const source = new CryptoRandomSource();
    for (let index = 0; index < 200; index += 1) {
      const value = source.next();
      expect(value).toBeGreaterThan(0n);
      expect(value).toBeLessThanOrEqual(MAX_REQUEST_ID);
    }
  });
});

describe("公共时间格式", () => {
  it("合法时间原样输出", () => {
    expect(formatProtocolTime("2026-01-15 08:00:00")).toBe("2026-01-15 08:00:00");
    expect(isUtcText("2026-01-15 08:00:00")).toBe(true);
  });

  it("非法格式与非法日期拒绝", () => {
    expect(() => formatProtocolTime("2026-1-15 08:00:00")).toThrow();
    expect(() => formatProtocolTime("2026-02-30 08:00:00")).toThrow();
    expect(() => formatProtocolTime("2026-01-15 08:00:00.5")).toThrow();
    expect(() => formatProtocolTime(123 as unknown as string)).toThrow();
  });

  it("Date 转 UTC 秒级文本", () => {
    expect(utcTextFromDate(new Date(Date.UTC(2026, 0, 15, 8, 0, 0)))).toBe(
      "2026-01-15 08:00:00",
    );
  });
});
