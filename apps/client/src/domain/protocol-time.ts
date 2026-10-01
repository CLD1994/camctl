/**
 * 公共协议时间格式。
 *
 * 输出固定 UTC 秒级字面量 YYYY-MM-DD HH:mm:ss；输入非法（非字符
 * 串、格式不符、非真实日期）时抛错，不返回默认值。
 */

const TIMESTAMP = /^([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})$/;

export type UtcText = string;

export function isUtcText(raw: string): boolean {
  return TIMESTAMP.test(raw);
}

function daysInMonth(year: number, month: number): number {
  const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  const table = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return table[month - 1]!;
}

/** 校验并返回规范时间文本；不修改、不补默认。 */
export function formatProtocolTime(value: string): UtcText {
  const match = TIMESTAMP.exec(value);
  if (match === null) {
    throw new Error(`非法公共时间格式: ${JSON.stringify(value)}`);
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (month < 1 || month > 12) {
    throw new Error(`非法月份: ${value}`);
  }
  if (day < 1 || day > daysInMonth(year, month)) {
    throw new Error(`非法日期: ${value}`);
  }
  if (hour > 23 || minute > 59 || second > 59) {
    throw new Error(`非法时间分量: ${value}`);
  }
  return value;
}

/** Date → 公共时间文本（UTC，秒级）。 */
export function utcTextFromDate(date: Date): UtcText {
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return (
    `${pad(date.getUTCFullYear(), 4)}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    ` ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`
  );
}
