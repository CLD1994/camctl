import type { DraftContent } from "../../src/server/models";
import type { StatusReport } from "../../src/shared/types";

/** 组件与跨组件测试共享的边界输入；不从生产校验器计算预期。 */
export const motorPositions = [-2147483648, -1, 0, 2147483647] as const;
export const roundedMotorFractions = [
  "2147483647.00000001",
  "1.0000000000000000001",
  "1e-999",
] as const;
export function motorDraft(position = 0): DraftContent {
  return {
    text: JSON.stringify({
      name: "电机计划",
      actions: [
        {
          name: "位置控制",
          type: "motor_control",
          scheduled_at: "2026-10-08 12:00:00",
          params: { position },
          policy: { max_delay_ms: 1000 },
        },
      ],
    }),
  };
}
export function motorReport(requestId = "1", position = 0): StatusReport {
  return {
    report_id: "1",
    from_wm: 0,
    to_wm: 1,
    plans: [
      {
        plan_instance_id: "1",
        request_id: requestId,
        name: "电机计划",
        created_at: "2026-10-08 11:00:00",
        status: "completed",
        actions: [
          {
            action_instance_id: "1",
            name: "位置控制",
            type: "motor_control",
            scheduled_at: "2026-10-08 12:00:00",
            input_params: { position },
            policy: { max_delay_ms: 1000 },
            status: "succeeded",
          },
        ],
      },
    ],
  };
}
