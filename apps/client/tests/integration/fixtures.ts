import { createHash, randomUUID } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import type { ImportFile } from "../../src/server/models";
import type { StatusReport } from "../../src/shared/types";

export function reportInput(report: StatusReport) {
  const bytes = Buffer.from(JSON.stringify(report));
  const fileName = `status-report-${report.report_id}-${createHash("sha256").update(bytes).digest("hex")}.json`;
  const file: ImportFile = {
    id: randomUUID(),
    batchId: "test",
    fileName,
    kind: "report",
    expectedSize: bytes.length,
    bytesReceived: bytes.length,
    status: "received",
    createdAt: "test",
  };
  return { file, bytes };
}
export function mappedReport(bytes: Buffer): StatusReport {
  const dir = join(
    "../../protocol/examples/client-protocol",
    "01-success",
  );
  const name = readdirSync(dir).find((f) =>
    /^status-report-1-[0-9a-f]{64}\.json$/.test(f),
  )!;
  const r = JSON.parse(readFileSync(join(dir, name), "utf8")) as StatusReport;
  const digest = createHash("sha256").update(bytes).digest("hex");
  for (const action of r.plans![0].actions!) {
    for (const output of action.outputs ?? []) {
      output.size = bytes.length;
      output.checksum = { status: "available", sha256: digest };
    }
    for (const delivery of action.deliveries ?? []) {
      delivery.size = bytes.length;
      delivery.sha256 = digest;
    }
  }
  return r;
}
/** 权威示例中 obtain 交付的文件名；上传与报告映射都从这里取得。 */
export function deliveryFileName(): string {
  return mappedReport(Buffer.from("probe")).plans![0].actions!.find(
    (a) => a.type === "obtain_action_outputs",
  )!.deliveries![0].file_name;
}
