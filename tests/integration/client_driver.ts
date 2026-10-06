// 客户端真实服务驱动：导出计划与导入报告两个子命令。
//
//   client_driver.ts export <客户端存储目录> <计划正文JSON路径> <输出计划路径> <输出凭据路径>
//   client_driver.ts import <报告目录> <客户端存储目录> <输出凭据路径>
//
// 导出走客户端的 createDraft/exportDraft/downloadRequest 真实路径：
// 按能力校验计划正文、分配随机整数请求身份；下载正文在客户端已保
// 存报告时自动携带 last_report_id（ACK）。导入经 parseReport/
// applyReports 真实路径核验并可靠保存，输出保存凭据供跨组件测试
// 独立核对。

import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Application } from "../../apps/client/src/server/application";
import { parseReport } from "../../apps/client/src/domain/reports";
import type { ImportFile } from "../../apps/client/src/server/models";

const [command, ...rest] = process.argv.slice(2);

function openApp(storeDir: string) {
  mkdirSync(storeDir, { recursive: true });
  const app = new Application(storeDir);
  app.store.initialize();
  return app;
}

function exportPlan(
  storeDir: string,
  bodyPath: string,
  planPath: string,
  receiptPath: string,
) {
  const app = openApp(storeDir);
  const draft = app.createDraft({ text: readFileSync(bodyPath, "utf-8") });
  const exported = app.exportDraft(draft.id, draft.revision, draft.content);
  const body = app.downloadRequest(exported.id);
  writeFileSync(planPath, JSON.stringify(body), "utf-8");
  writeFileSync(
    receiptPath,
    JSON.stringify({
      request_id: exported.id,
      created_at: exported.body.created_at,
      has_ack: Object.hasOwn(body, "last_report_id"),
      last_report_id: body.last_report_id ?? null,
    }),
    "utf-8",
  );
  app.store.close();
}

function importReports(
  reportsDir: string,
  storeDir: string,
  receiptPath: string,
) {
  const app = openApp(storeDir);
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
    .map(({ reportId }) => ({
      reportId,
      saved: app.store.report(reportId) !== undefined,
    }))
    .filter((entry) => entry.saved)
    .map((entry) => entry.reportId);

  writeFileSync(
    receiptPath,
    JSON.stringify({
      seen: names,
      saved_report_ids: savedReportIds,
      ack_id: app.ackId(),
      coverage: app.snapshot().to_wm ?? null,
    }),
    "utf-8",
  );
  app.store.close();
}

if (command === "export") {
  const [storeDir, bodyPath, planPath, receiptPath] = rest;
  if (!storeDir || !bodyPath || !planPath || !receiptPath) badUsage();
  exportPlan(storeDir, bodyPath, planPath, receiptPath);
} else if (command === "import") {
  const [reportsDir, storeDir, receiptPath] = rest;
  if (!reportsDir || !storeDir || !receiptPath) badUsage();
  importReports(reportsDir, storeDir, receiptPath);
} else {
  badUsage();
}

function badUsage(): never {
  console.error(
    "用法: client_driver.ts export <存储目录> <正文路径> <计划路径> <凭据路径> | import <报告目录> <存储目录> <凭据路径>",
  );
  process.exit(2);
}
