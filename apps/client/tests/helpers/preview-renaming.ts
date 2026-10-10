import type { DraftContent } from "../../src/server/models";
const time = "2026-10-10 01:00:00";
export function linked(): DraftContent {
  return {
    text: JSON.stringify({
      name: "计划",
      actions: [
        {
          name: "A",
          type: "camera_record",
          device_id: "cam",
          scheduled_at: time,
          params: { type: "photo" },
          policy: { max_delay_ms: 0 },
        },
        {
          name: "B",
          type: "camera_record",
          device_id: "cam",
          scheduled_at: time,
          params: { type: "photo" },
          policy: { max_delay_ms: 0 },
        },
        {
          name: "自动A",
          type: "obtain_action_outputs",
          scheduled_at: time,
          params: {
            source: { action_name: "A" },
            filter: "preview",
            purpose: "auto_preview",
          },
        },
        {
          name: "自动B",
          type: "obtain_action_outputs",
          scheduled_at: time,
          params: {
            source: { action_name: "B" },
            filter: "preview",
            purpose: "auto_preview",
          },
        },
        {
          name: "手动",
          type: "obtain_action_outputs",
          scheduled_at: time,
          params: {
            source: { action_name: "A" },
            filter: "preview",
            purpose: "manual",
          },
        },
      ],
    }),
    automaticPreviews: {
      intent: "enabled",
      namespace: "n",
      next: 5,
      actions: [
        { id: "n:0" },
        { id: "n:1" },
        { id: "n:2", sourceId: "n:0" },
        { id: "n:3", sourceId: "n:1" },
        { id: "n:4" },
      ],
    },
  };
}
