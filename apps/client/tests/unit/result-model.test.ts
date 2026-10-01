import { it, expect } from "vitest";
import { resultProducts, resultNotes } from "../../src/web/result-model";
import type {
  ReportAction,
  ReportPlan,
  Output,
  Delivery,
} from "../../src/shared/types";
import type { Video } from "../../src/server/models";
const output: Output = {
  output_id: "o1",
  source_action_instance_id: "a1",
  kind: "original",
  media_type: "image/png",
  availability: "available",
  cleanup: { status: "not_requested" },
  checksum: { status: "not_obtained" },
  media: { check_status: "not_performed", duration: { status: "unknown" } },
};
const capture = {
  action_instance_id: "a1",
  type: "camera_timelapse",
  name: "拍摄",
  status: "canceled",
  outputs: [output],
} as ReportAction;
const d = (id: string) =>
  ({
    delivery_id: id,
    output_id: "o1",
    source_action_instance_id: "a1",
    file_name: id + ".png",
    display_name: "照片",
    status: "published",
  }) as Delivery;
const obtain = (id: string) =>
  ({
    action_instance_id: id,
    type: "obtain_action_outputs",
    name: "取回",
    status: "succeeded",
    deliveries: [d(id)],
  }) as ReportAction;
const plans = [
  { request_id: "r1", actions: [capture, obtain("d1"), obtain("d2")] },
] as ReportPlan[];
const videos = [
  { id: "v2", fileName: "d2.png", status: "verified" },
] as Video[];
it("拍摄按产物聚合多次交付，取消不隐藏图片", () => {
  const rows = resultProducts(capture, plans, videos);
  expect(rows).toHaveLength(1);
  expect(rows[0].deliveries).toHaveLength(2);
  expect(rows[0].state).toBe("ready");
  expect(rows[0].group).toBe("image");
});
it("本次取回不借用其他取回的本地副本", () => {
  const rows = resultProducts(obtain("d1"), plans, videos);
  expect(rows[0].state).toBe("waiting");
  expect(rows[0].deliveries).toHaveLength(1);
});
it("报告类型未知不从文件名猜测图片", () => {
  const row = resultProducts(
    { ...capture, outputs: [{ ...output, media_type: undefined }] },
    [],
    [],
  )[0];
  expect(row.group).toBe("other");
});
it("没有来源快照的交付仍可查看状态", () => {
  const rows = resultProducts(obtain("d2"), [], videos);
  expect(rows[0].output).toBeUndefined();
  expect(rows[0].state).toBe("ready");
  expect(rows[0].group).toBe("other");
});
it("好副本不会掩盖另一次失败交付", () => {
  const bad = {
    ...obtain("d1"),
    deliveries: [{ ...d("d1"), status: "failed" }],
  } as ReportAction;
  const rows = resultProducts(
    capture,
    [{ ...plans[0], actions: [capture, bad, obtain("d2")] }],
    videos,
  );
  expect(rows[0].state).toBe("ready");
  expect(rows[0].problems).toBe(1);
});
it("主机已发布但本地尚未收到文件时保持等待", () => {
  const rows = resultProducts(obtain("d1"), plans, []);
  expect(rows[0].deliveries).toHaveLength(1);
  expect(rows[0].state).toBe("waiting");
  expect(rows[0].problems).toBe(0);
});
it("本地文件核验失败标记注意并计数", () => {
  const mismatched = [
    { id: "v1", fileName: "d1.png", status: "mismatch" },
  ] as Video[];
  const rows = resultProducts(obtain("d1"), plans, mismatched);
  expect(rows[0].state).toBe("attention");
  expect(rows[0].problems).toBe(1);
});
it("部分产物被保留清理时其余产物仍全部列出", () => {
  const kept: Output = { ...output, output_id: "o2", cleanup: { status: "canceled" } };
  const action = { ...capture, outputs: [output, kept] } as ReportAction;
  const rows = resultProducts(action, [], []);
  expect(rows.map((r) => r.id)).toEqual(["o1", "o2"]);
});
it("没有交付的来源失败仍出现在动作业务摘要", () => {
  const action = {
    ...obtain("d1"),
    result: {
      failures: [
        {
          source_action_instance_id: "missing",
          error: { code: "source_not_found", stage: "selection", details: {} },
        },
      ],
    },
  } as ReportAction;
  expect(resultNotes(action).some((n) => n.text.includes("1") && n.error)).toBe(
    true,
  );
});
it("录像成功但修复失败仍提示后处理异常", () => {
  const action = {
    ...capture,
    type: "camera_record",
    status: "succeeded",
    result: { repair: { status: "failed" } },
  } as ReportAction;
  expect(resultNotes(action).some((n) => n.error)).toBe(true);
});
it("设备结束未确认或仍在运行都以自然语言提示", () => {
  const unconfirmed = {
    ...capture,
    status: "failed",
    error: { code: "future_code", stage: "execution", details: {} },
    device_execution: { status: "end_unconfirmed" },
  } as ReportAction;
  const running = {
    ...unconfirmed,
    device_execution: { status: "still_running" },
  } as ReportAction;
  expect(
    resultNotes(unconfirmed).some((n) => n.error && n.text.includes("尚未确认")),
  ).toBe(true);
  expect(
    resultNotes(running).some((n) => n.error && n.text.includes("仍在执行")),
  ).toBe(true);
  // 提示解除后摘要不再保留设备执行说法。
  expect(
    resultNotes({ ...unconfirmed, device_execution: undefined }).some((n) =>
      n.text.includes("设备"),
    ),
  ).toBe(false);
});
