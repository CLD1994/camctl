// 客户端真实服务驱动：导出计划、导入报告及视频估算交接。
//
//   client_driver.ts export <客户端存储目录> <计划正文JSON路径> <输出计划路径> <输出凭据路径>
//   client_driver.ts import <报告目录> <客户端存储目录> <输出凭据路径>
//   client_driver.ts estimate <客户端存储目录> <动作JSON路径> <输出凭据路径>
//   client_driver.ts reload-estimate <客户端存储目录> <动作JSON路径> <待加载能力路径> <输出凭据路径>
//
// 导出走客户端的 createDraft/exportDraft/downloadRequest 真实路径：
// 按能力校验计划正文、分配随机整数请求身份；下载正文在客户端已保
// 存报告时自动携带 last_report_id（ACK）。导入经 parseReport/
// applyReports 真实路径核验并可靠保存，输出保存凭据供跨组件测试
// 独立核对。
// 估算先建立测试输入草稿，再读取真实 HTTP 状态；凭据记录计算及
// 前后草稿事实，不新增产品 API 或持久化字段。

import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { Application } from "../../apps/client/src/server/application";
import { parseReport } from "../../apps/client/src/domain/reports";
import type { Draft, ImportFile } from "../../apps/client/src/server/models";
import {
  parseJson,
  parseClientJson,
  stringifyJson,
} from "../../apps/client/src/shared/json";
import { loadCapabilities } from "../../apps/client/src/shared/capabilities";
import { estimateVideoAction } from "../../apps/client/src/shared/video-size-estimate";
import { Files } from "../../apps/client/src/server/files";
import { createHttpApp } from "../../apps/client/src/server/http";
import {
  createStop,
  RequestLifecycle,
} from "../../apps/client/src/server/lifecycle";

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
  writeFileSync(planPath, stringifyJson(body), "utf-8");
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
  const names = readdirSync(reportsDir).filter((name) =>
    name.startsWith("status-report-"),
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

async function estimateAction(
  storeDir: string,
  actionPath: string,
  receiptPath: string,
  replacementPath?: string,
) {
  const app = openApp(storeDir);
  const files = new Files(app),
    requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      ),
    requests,
    () => files.idle(),
    () => app.store.close(),
  );
  try {
    await new Promise<void>((resolve, reject) => {
      server.once("listening", resolve);
      server.once("error", reject);
    });
    const action = parseJson(readFileSync(actionPath, "utf8"));
    // 先建立测试输入；随后的估算及状态交接应保留这份草稿的原事实。
    const draft = app.createDraft({
      text: stringifyJson({ name: "估算输入", actions: [action] }),
    });
    const original = app.capabilities.active;
    const initial = estimateVideoAction(action, original);
    const draftsBefore = app.store.all("drafts");
    const address = server.address() as { port: number };
    const origin = `http://127.0.0.1:${address.port}`;
    if (replacementPath) {
      writeFileSync(
        join(storeDir, "device-capabilities.json"),
        readFileSync(replacementPath),
      );
      const reloaded = await fetch(`${origin}/api/capabilities/reload`, {
        method: "POST",
      });
      if (!reloaded.ok) throw new Error(`能力重载 HTTP ${reloaded.status}`);
      await reloaded.text();
    }
    const response = await fetch(`${origin}/api/state`);
    if (!response.ok) throw new Error(`状态读取 HTTP ${response.status}`);
    const state = parseClientJson(await response.text()) as {
      capabilities: { active: unknown; error: string | null };
      drafts: Draft[];
    };
    const observedDraft = state.drafts.find((item) => item.id === draft.id);
    if (!observedDraft) throw new Error("HTTP 状态缺少本次输入草稿");
    const observedPlan = parseJson(observedDraft.content.text) as {
      actions: unknown[];
    };
    const observedAction = observedPlan.actions[0];
    const browserCapabilities =
      state.capabilities.active === null
        ? null
        : loadCapabilities(state.capabilities.active);
    writeFileSync(
      receiptPath,
      stringifyJson({
        initial,
        estimate: estimateVideoAction(action, app.capabilities.active),
        http_estimate: estimateVideoAction(observedAction, browserCapabilities),
        http_action: observedAction,
        load_error: state.capabilities.error,
        active_unchanged: app.capabilities.active === original,
        drafts_before: draftsBefore,
        drafts_after: app.store.all("drafts"),
      }),
    );
  } finally {
    await stop();
  }
}

async function main() {
  if (command === "export") {
    const [storeDir, bodyPath, planPath, receiptPath] = rest;
    if (!storeDir || !bodyPath || !planPath || !receiptPath) badUsage();
    exportPlan(storeDir, bodyPath, planPath, receiptPath);
  } else if (command === "import") {
    const [reportsDir, storeDir, receiptPath] = rest;
    if (!reportsDir || !storeDir || !receiptPath) badUsage();
    importReports(reportsDir, storeDir, receiptPath);
  } else if (command === "estimate") {
    const [storeDir, actionPath, receiptPath] = rest;
    if (!storeDir || !actionPath || !receiptPath) badUsage();
    await estimateAction(storeDir, actionPath, receiptPath);
  } else if (command === "reload-estimate") {
    const [storeDir, actionPath, replacementPath, receiptPath] = rest;
    if (!storeDir || !actionPath || !replacementPath || !receiptPath)
      badUsage();
    await estimateAction(storeDir, actionPath, receiptPath, replacementPath);
  } else {
    badUsage();
  }
}

void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

function badUsage(): never {
  console.error(
    "用法: client_driver.ts export <存储目录> <正文路径> <计划路径> <凭据路径> | import <报告目录> <存储目录> <凭据路径> | estimate <存储目录> <动作路径> <凭据路径> | reload-estimate <存储目录> <动作路径> <替换说明路径> <凭据路径>",
  );
  process.exit(2);
}
