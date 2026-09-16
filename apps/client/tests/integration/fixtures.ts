import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
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
  const r = JSON.parse(
    readFileSync(
      "../../protocol/examples/client-protocol/01-success/status-report-1-6a2d8346b79be33790f613d183707116fb16ec40908850b7e9963195bf4f9b62.json",
      "utf8",
    ),
  ) as StatusReport;
  const digest = createHash("sha256").update(bytes).digest("hex");
  for (const action of r.plans![0].actions!) {
    for (const output of action.outputs ?? []) {
      output.size = bytes.length;
      output.checksum = { status: "available", sha256: digest };
    }
    for (const delivery of action.deliveries ?? []) {
      delivery.size = bytes.length;
      delivery.sha256 = digest;
      delivery.copy.source_size = bytes.length;
      delivery.copy.committed_bytes = bytes.length;
    }
  }
  return r;
}
