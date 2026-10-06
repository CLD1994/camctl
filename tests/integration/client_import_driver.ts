// 客户端真实导入驱动：消费目录中的 status-report 文件并保存。
//
// 用法：node --import tsx client_import_driver.ts <报告目录> <客户端存储目录> <凭据输出路径>
// 驱动只做递交与读取：文件经客户端服务的 parseReport/applyReports
// 真实路径核验（文件名身份与字节摘要一致才接受）并可靠保存，输出
// 已保存报告身份与累计确认位置，供跨组件测试独立核对。

import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Application } from "../../apps/client/src/server/application";
import { parseReport } from "../../apps/client/src/domain/reports";
import type { ImportFile } from "../../apps/client/src/server/models";

const [reportsDir, storeDir, outputPath] = process.argv.slice(2);
if (!reportsDir || !storeDir || !outputPath) {
  console.error("用法: client_import_driver.ts <报告目录> <存储目录> <输出路径>");
  process.exit(2);
}

mkdirSync(storeDir, { recursive: true });
const app = new Application(storeDir);
app.store.initialize();

const names = readdirSync(reportsDir).filter(
  (name) => name.startsWith("status-report-"),
);
const inputs = names.map((name) => {
  const bytes = new Uint8Array(readFileSync(join(reportsDir, name)));
  // 先解析一次取得报告身份；applyReports 内部再做同一核验。
  const report = parseReport(name, bytes);
  const file: ImportFile = {
    id: randomUUID(),
    batchId: "cross-component",
    fileName: name,
    kind: "report",
    expectedSize: bytes.length,
    bytesReceived: bytes.length,
    status: "received",
    createdAt: new Date().toISOString(),
  };
  return { file, bytes, reportId: report.report_id };
});

app.applyReports(inputs.map(({ file, bytes }) => ({ file, bytes })));

const savedReportIds = inputs
  .map(({ reportId }) => ({ reportId, saved: app.store.report(reportId) !== undefined }))
  .filter((entry) => entry.saved)
  .map((entry) => entry.reportId);

writeFileSync(
  outputPath,
  JSON.stringify({
    seen: names,
    saved_report_ids: savedReportIds,
    ack_id: app.ackId(),
    coverage: app.snapshot().to_wm ?? null,
  }),
  "utf-8",
);
app.store.close();
