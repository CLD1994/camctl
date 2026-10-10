import { expect, it } from "vitest";
import {
  readJsonPointer,
  decodeJsonPointer,
} from "../../src/shared/json-pointer";

it("读取空成员名", () =>
  expect(readJsonPointer({ "": 7 }, "/")).toMatchObject({
    found: true,
    key: "",
    value: 7,
  }));
it("依次解码斜杠、波浪号与数组位置", () => {
  const array = [9];
  expect(readJsonPointer({ "a/b": { "~x": array } }, "/a~1b/~0x/0")).toEqual({
    found: true,
    parent: array,
    key: "0",
    value: 9,
  });
});
it("对象的非规范数字成员保持原名", () =>
  expect(readJsonPointer({ "01": 8 }, "/01")).toMatchObject({
    found: true,
    value: 8,
  }));
it("数组的零索引可以读取", () =>
  expect(readJsonPointer([7], "/0")).toMatchObject({ found: true, value: 7 }));
it.each(["01", "-", "length", "1", "9007199254740992", "-1", "1.0"])(
  "数组不读取非法或越界位置 /%s",
  (key) => expect(readJsonPointer([7], "/" + key)).toEqual({ found: false }),
);
it("数组空洞属于缺失", () =>
  expect(readJsonPointer(new Array(1), "/0")).toEqual({ found: false }));
it("自身 __proto__ 成员可以读取", () =>
  expect(
    readJsonPointer(JSON.parse('{"__proto__":7}'), "/__proto__"),
  ).toMatchObject({ found: true, value: 7 }));
it.each(["inherited", "toString", "__proto__"])("不读取原型成员 %s", (key) =>
  expect(readJsonPointer(Object.create({ inherited: 7 }), "/" + key)).toEqual({
    found: false,
  }),
);
it("显式 null 与缺失区分", () =>
  expect(readJsonPointer({ a: null }, "/a")).toMatchObject({
    found: true,
    value: null,
  }));
it.each([{}, { a: null }, { a: 7 }, null])(
  "中间值无法读取时属于缺失 %#",
  (root) => expect(readJsonPointer(root, "/a/b")).toEqual({ found: false }),
);
it.each(["", "#/a", "a", "/a~", "/a~2", "/a~01~x"])(
  "拒绝非法估算指针 %s",
  (path) => expect(() => readJsonPointer({}, path)).toThrow(),
);
it("公共解码保留编辑资料的空根路径", () =>
  expect(decodeJsonPointer("")).toEqual([]));
it("~01 只按标准顺序解码一次", () =>
  expect(decodeJsonPointer("/~01")).toEqual(["~1"]));
