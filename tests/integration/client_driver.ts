// 客户端真实服务驱动：导出计划、导入报告及视频估算交接。
//
//   client_driver.ts export <客户端存储目录> <计划正文JSON路径> <输出计划路径> <输出凭据路径>
//   client_driver.ts import <报告目录> <客户端存储目录> <输出凭据路径>
//   client_driver.ts import-media <客户端存储目录> <已领取文件路径> <输出凭据路径>
//   client_driver.ts estimate <客户端存储目录> <动作JSON路径> <输出凭据路径>
//   client_driver.ts validate-parameters <客户端存储目录> <参数用例路径> <输出凭据路径>
//   client_driver.ts reload-estimate <客户端存储目录> <动作JSON路径> <待加载能力路径> <输出凭据路径>
//
// 导出走客户端的 createDraft/exportDraft/downloadRequest 真实路径：
// 按能力校验计划正文、分配随机整数请求身份；下载正文在客户端已保
// 存报告时自动携带 last_report_id（ACK）。导入经 parseReport/
// applyReports 真实路径核验并可靠保存，输出保存凭据供跨组件测试
// 独立核对。
// 估算先建立测试输入草稿，再读取真实 HTTP 状态；凭据记录计算及
// 前后草稿事实，不新增产品 API 或持久化字段。

import {
  createReadStream,
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { stat } from "node:fs/promises";
import { join, dirname } from "node:path";
import { basename } from "node:path";
import { fileURLToPath } from "node:url";
import type { IncomingMessage, ServerResponse } from "node:http";
import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";
import { Application } from "../../apps/client/src/server/application";
import { parseReport } from "../../apps/client/src/domain/reports";
import {
  AppError,
  type Draft,
  type ImportFile,
} from "../../apps/client/src/server/models";
import {
  parseJson,
  parseClientJson,
  stringifyJson,
} from "../../apps/client/src/shared/json";
import {
  loadCapabilities,
  validateParams,
} from "../../apps/client/src/shared/capabilities";
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

async function importMedia(
  storeDir: string,
  claimedPath: string,
  receiptPath: string,
) {
  const app = openApp(storeDir);
  const files = new Files(app);
  try {
    const fileName = basename(claimedPath);
    const info = await stat(claimedPath);
    if (!info.isFile()) throw new Error("领取路径不是普通文件");

    const actions = app.snapshot().plans?.flatMap((plan) => plan.actions ?? []) ?? [];
    const deliveries = actions
      .flatMap((action) => action.deliveries ?? [])
      .filter((delivery) => delivery.file_name === fileName);
    if (deliveries.length !== 1)
      throw new Error(`报告中匹配的领取文件必须恰好有一条，实际为 ${deliveries.length}`);
    const delivery = deliveries[0];
    const outputs = actions
      .flatMap((action) => action.outputs ?? [])
      .filter((output) => output.output_id === delivery.output_id);
    if (outputs.length !== 1)
      throw new Error(`报告中匹配的来源输出必须恰好有一条，实际为 ${outputs.length}`);
    const output = outputs[0];
    if (output.source_action_instance_id !== delivery.source_action_instance_id)
      throw new Error("领取记录与输出的来源动作身份不一致");

    const importKind = output.kind === "preview" ? "media" : "video";
    const batch = files.createBatch([
      { fileName, size: info.size, kind: importKind },
    ]);
    const file = batch.files[0];
    await files.upload(file.id, createReadStream(claimedPath));
    await files.idle();

    const imported = app.store.get<ImportFile>("imports", file.id);
    const video = files.videos().find((item) => item.id === file.id);
    if (!imported || !video) throw new Error("Files 未保存实际媒体入库记录");
    writeFileSync(
      receiptPath,
      stringifyJson({
        file_name: fileName,
        delivery_id: delivery.delivery_id,
        output_id: output.output_id,
        source_action_instance_id: output.source_action_instance_id,
        output_kind: output.kind,
        import_kind: importKind,
        import: imported,
        video,
      }),
      "utf-8",
    );
  } finally {
    await files.idle();
    app.store.close();
  }
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

// 测试合同的同一 describe 文件分别进入真实浏览器模块与后端导出。
// 输入和输出正文以文本交接，避免 Playwright 的浮点传输改写原数字。
async function validateParameters(
  storeDir: string,
  inputPath: string,
  receiptPath: string,
) {
  type ParameterCase = { name: string; params_text: string };
  const input = JSON.parse(readFileSync(inputPath, "utf8")) as {
    scheduled_at: string;
    cases: ParameterCase[];
  };
  const capabilitiesText = readFileSync(
    join(storeDir, "device-capabilities.json"),
    "utf8",
  );
  const capabilities = loadCapabilities(parseClientJson(capabilitiesText));
  const requireClient = createRequire(
    new URL("../../apps/client/package.json", import.meta.url),
  );
  const { createServer } = await import(requireClient.resolve("vite"));
  const { chromium } = requireClient("@playwright/test");
  const clientRoot = fileURLToPath(
    new URL("../../apps/client/", import.meta.url),
  );
  const vite = await createServer({
    configFile: false,
    root: clientRoot,
    logLevel: "error",
    server: {
      host: "127.0.0.1",
      port: 0,
      fs: { allow: [dirname(dirname(clientRoot))] },
    },
  });
  vite.middlewares.use(
    "/__parameter-seam",
    (_req: IncomingMessage, res: ServerResponse) => {
      res.setHeader("Content-Type", "text/html");
      res.end("<!doctype html><html><body>参数接缝</body></html>");
    },
  );
  const app = openApp(storeDir);
  let browser;
  try {
    await vite.listen();
    const address = vite.httpServer!.address() as { port: number };
    const origin = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    await page.goto(`${origin}/__parameter-seam`);
    const browserCases = await page.evaluate(
      async ({
        origin,
        capabilitiesText,
        cases,
      }: {
        origin: string;
        capabilitiesText: string;
        cases: ParameterCase[];
      }) => {
        const { parseClientJson, stringifyJson } = await import(
          `${origin}/src/shared/json.ts`
        );
        const { loadCapabilities, validateParams } = await import(
          `${origin}/src/shared/capabilities.ts`
        );
        const capabilities = loadCapabilities(
          parseClientJson(capabilitiesText),
        );
        return cases.map((item: ParameterCase) => {
          const params = parseClientJson(item.params_text);
          return {
            valid:
              validateParams("camera", "camera_timelapse", params, capabilities)
                .length === 0,
            params: stringifyJson(params),
          };
        });
      },
      { origin, capabilitiesText, cases: input.cases },
    );
    const cases = input.cases.map((item, index) => {
      const params = parseJson(item.params_text);
      const sharedValid =
        validateParams("camera", "camera_timelapse", params, capabilities)
          .length === 0;
      const body = {
        name: "时间参数接缝",
        actions: [
          {
            name: "延时摄影",
            type: "camera_timelapse",
            device_id: "camera",
            scheduled_at: input.scheduled_at,
            policy: { max_delay_ms: 5000 },
            params,
          },
        ],
      };
      const draft = app.createDraft({ text: stringifyJson(body) });
      let exported: string | undefined, exportError: string | undefined;
      try {
        const request = app.exportDraft(
          draft.id,
          draft.revision,
          draft.content,
        );
        exported = stringifyJson(app.downloadRequest(request.id));
      } catch (error) {
        if (!(error instanceof AppError) || error.code !== "invalid_plan")
          throw error;
        exportError = error.code;
      }
      return {
        name: item.name,
        shared_valid: sharedValid,
        shared_params: stringifyJson(params),
        browser_valid: browserCases[index].valid,
        browser_params: browserCases[index].params,
        export_valid: exported !== undefined,
        exported,
        export_error: exportError,
      };
    });
    writeFileSync(receiptPath, stringifyJson({ cases }));
  } finally {
    await browser?.close();
    await vite.close();
    app.store.close();
  }
}

async function main() {
  if (command === "validate-parameters") {
    const [storeDir, inputPath, receiptPath] = rest;
    if (!storeDir || !inputPath || !receiptPath) badUsage();
    await validateParameters(storeDir, inputPath, receiptPath);
  } else if (command === "export") {
    const [storeDir, bodyPath, planPath, receiptPath] = rest;
    if (!storeDir || !bodyPath || !planPath || !receiptPath) badUsage();
    exportPlan(storeDir, bodyPath, planPath, receiptPath);
  } else if (command === "import") {
    const [reportsDir, storeDir, receiptPath] = rest;
    if (!reportsDir || !storeDir || !receiptPath) badUsage();
    importReports(reportsDir, storeDir, receiptPath);
  } else if (command === "import-media") {
    const [storeDir, claimedPath, receiptPath] = rest;
    if (!storeDir || !claimedPath || !receiptPath) badUsage();
    await importMedia(storeDir, claimedPath, receiptPath);
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
    "用法: client_driver.ts export <存储目录> <正文路径> <计划路径> <凭据路径> | import <报告目录> <存储目录> <凭据路径> | import-media <存储目录> <已领取文件路径> <凭据路径> | estimate <存储目录> <动作路径> <凭据路径> | validate-parameters <存储目录> <参数用例路径> <凭据路径> | reload-estimate <存储目录> <动作路径> <替换说明路径> <凭据路径>",
  );
  process.exit(2);
}
