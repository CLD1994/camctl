import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";

const root = new URL("../../", import.meta.url);
const require = createRequire(
  new URL("../../apps/client/package.json", import.meta.url),
);
const Ajv = require("ajv/dist/2020").default;
const json = (path) => JSON.parse(readFileSync(new URL(path, root), "utf8"));
const ajv = new Ajv({ strict: false, allErrors: true });
for (const file of readdirSync(new URL("protocol/schemas/", root)))
  if (file.endsWith(".schema.json"))
    ajv.addSchema(json(`protocol/schemas/${file}`), file);
ajv.addSchema(
  json("protocol/errors/workflow-codes.json"),
  "workflow-codes.json",
);
const validate = ajv.getSchema("status-report.schema.json");

const scope = (state = "nonempty", outputIds = ["11"]) => ({
  state,
  member_count: state === "unknown" ? null : outputIds.length,
  output_ids: outputIds,
});
const coverage = (metric = "duration_s", values = {}) => ({
  metric,
  target: metric === "frames" ? 151 : 60.5,
  confirmed: null,
  status: "not_performed",
  checked_output_ids: [],
  default_scope: scope(),
  ...values,
});
const report = (type, result) => ({
  report_id: "1",
  from_wm: 0,
  to_wm: 1,
  plans: [
    {
      plan_instance_id: "10",
      request_id: "1",
      created_at: "2026-10-23 00:00:00",
      name: "录像计划",
      status: "completed",
      actions: [
        {
          action_instance_id: "20",
          name: "拍摄",
          type,
          device_id: "cam0",
          scheduled_at: "2026-10-23 00:00:00",
          input_params: { type: "capture" },
          effective_params: { type: "capture" },
          policy: { max_delay_ms: 0 },
          status: "succeeded",
          result,
        },
      ],
    },
  ],
});
const accepted = (value) =>
  assert.equal(validate(value), true, JSON.stringify(validate.errors));
const rejected = (value) => assert.equal(validate(value), false);

test("普通录像与原生延时都可报告适用量纲的覆盖", () => {
  accepted(report("camera_record", { coverage: coverage() }));
  accepted(
    report("camera_timelapse", {
      coverage: coverage("frames", {
        confirmed: 165,
        status: "reached",
        checked_output_ids: ["11"],
      }),
    }),
  );
});

test("覆盖量纲、整数帧及未检查状态组合受 Schema 约束", () => {
  rejected(
    report("camera_record", {
      coverage: coverage("frames"),
    }),
  );
  rejected(
    report("camera_timelapse", {
      coverage: coverage("duration_s"),
    }),
  );
  rejected(
    report("camera_timelapse", {
      coverage: coverage("frames", { target: 151.5 }),
    }),
  );
  rejected(
    report("camera_record", {
      coverage: coverage("duration_s", { confirmed: 0 }),
    }),
  );
  rejected(
    report("camera_record", {
      coverage: coverage("duration_s", {
        reason: { code: "unknown", debug: true },
      }),
    }),
  );
});

test("覆盖使用的公共默认范围保留 empty、unknown 与 nonempty 结构", () => {
  accepted(
    report("camera_record", {
      coverage: coverage("duration_s", {
        status: "unconfirmed",
        default_scope: scope("unknown", []),
        reason: { code: "missing_position", message: "顺序起点未知" },
      }),
    }),
  );
  accepted(
    report("camera_record", {
      coverage: coverage("duration_s", {
        confirmed: 0,
        status: "confirmed_short",
        default_scope: scope("empty", []),
      }),
    }),
  );
});
