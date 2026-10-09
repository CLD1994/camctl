/**
 * 客户端请求身份分配。
 *
 * 身份是 1～2^63-1 的正整数，以 BigInt 参与运算、以规范十进制字
 * 符串参与公共协议；随机源提供均匀合法值，与本地已保存请求冲突
 * 时有限重选，重选耗尽返回导出错误——不返回默认 ID。
 */

import { randomBytes } from "node:crypto";
import { isCanonicalId } from "../shared/validation";

export const MAX_REQUEST_ID = 9_223_372_036_854_775_807n;

/** 规范十进制字符串：首位 1-9，无前导零、符号或空白。 */
export function isValidCanonicalId(raw: string): boolean {
  return isCanonicalId(raw);
}

/** 随机源端口：返回 1～MAX_REQUEST_ID 的均匀合法正整数。 */
export interface RandomSource {
  next(): bigint;
}

/** 单个身份的使用查询；查询失败不得当作未使用。 */
export interface RequestIdLookup {
  isUsed(id: bigint): Promise<boolean>;
}

export const MAX_ID_RESELECTIONS = 8;

export class RequestIdExhaustedError extends Error {
  constructor(attempts: number) {
    super(`请求身份重选耗尽（${attempts} 次冲突）`);
    this.name = "RequestIdExhaustedError";
  }
}

export async function newRequestId(
  random: RandomSource,
  usedIds: RequestIdLookup,
): Promise<string> {
  for (let attempt = 0; attempt <= MAX_ID_RESELECTIONS; attempt += 1) {
    const candidate = random.next();
    if (candidate < 1n || candidate > MAX_REQUEST_ID) {
      throw new Error(`随机源产生越界身份: ${candidate}`);
    }
    let used: boolean;
    try {
      used = await usedIds.isUsed(candidate);
    } catch (error) {
      throw new Error(`请求身份查询失败，不能当作未使用: ${String(error)}`);
    }
    if (!used) {
      return candidate.toString(10);
    }
  }
  throw new RequestIdExhaustedError(MAX_ID_RESELECTIONS + 1);
}

/** Node crypto 随机源：63 位均匀随机后取正。 */
export class CryptoRandomSource implements RandomSource {
  next(): bigint {
    const buffer = randomBytes(8);
    let value = 0n;
    for (const byte of buffer) {
      value = (value << 8n) | BigInt(byte);
    }
    value &= (1n << 63n) - 1n; // 63 位内均匀
    if (value === 0n) {
      return this.next();
    }
    return value;
  }
}
