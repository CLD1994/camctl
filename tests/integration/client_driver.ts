// 客户端真实服务驱动：网页导出、服务导出、报告/媒体导入及视频估算交接。
//
//   client_driver.ts initialize <客户端存储目录> <输出凭据路径>
//   client_driver.ts export <客户端存储目录> <计划正文JSON路径> <输出计划路径> <输出凭据路径>
//   client_driver.ts import <报告目录> <客户端存储目录> <输出凭据路径>
//   client_driver.ts import-media <客户端存储目录> <已领取文件路径> <输出凭据路径>
//   client_driver.ts estimate <客户端存储目录> <动作JSON路径> <输出凭据路径>
//   client_driver.ts validate-parameters <客户端存储目录> <参数用例路径> <输出凭据路径>
//   client_driver.ts reload-estimate <客户端存储目录> <动作JSON路径> <待加载能力路径> <输出凭据路径>
//   client_driver.ts capability-state <客户端存储目录> <输出凭据路径>
//   client_driver.ts browser-photo-export <存储目录> <输入路径> <计划路径> <凭据路径>
//   client_driver.ts browser-plan-export <存储目录> <正文路径> <计划路径> <凭据路径>
//   client_driver.ts upload-files <存储目录> <文件清单路径> <凭据路径>
//   client_driver.ts session <存储目录> <127.0.0.1> <端口> <会话身份> <计时路径>
//
// 导出走客户端的 createDraft/exportDraft/downloadRequest 真实路径：
// 按能力校验计划正文、分配随机整数请求身份；下载正文在客户端已保
// 存报告时自动携带 last_report_id（ACK）。导入经 parseReport/
// applyReports 真实路径核验并可靠保存，输出保存凭据供跨组件测试
// 独立核对。upload-files 则让报告与图片都经过真实 Files.upload，
// 支持媒体尚未有报告的等待状态；browser-* 通过当前构建首页下载。
// 估算先建立测试输入草稿，再读取真实 HTTP 状态；凭据记录计算及
// 前后草稿事实，不新增产品 API 或持久化字段。

import {
  createReadStream,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { stat } from "node:fs/promises";
import { join, dirname } from "node:path";
import { basename } from "node:path";
import { fileURLToPath } from "node:url";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import { createRequire } from "node:module";
import { randomUUID, createHash } from "node:crypto";
import { createConnection } from "node:net";
import { createInterface } from "node:readline";
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
  exactJsonValueIdentity,
} from "../../apps/client/src/shared/json";
import {
  loadCapabilities,
  validateParams,
} from "../../apps/client/src/shared/capabilities";
import { estimateVideoAction } from "../../apps/client/src/shared/video-size-estimate";
import { initializePreviewMetadata } from "../../apps/client/src/shared/automatic-previews";
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
  try {
    app.store.initialize();
    return app;
  } catch (error) {
    try { app.store.close(); } catch (closing) {
      throw new AggregateError([error, closing], "客户端初始化与收场均失败");
    }
    throw error;
  }
}

function initializeClient(storeDir: string, receiptPath: string) {
  const app = openApp(storeDir);
  try {
    const state = app.state();
    writeFileSync(
      receiptPath,
      stringifyJson({ initialized: state.startup.state === "ready", state }),
      "utf-8",
    );
  } finally {
    app.store.close();
  }
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

    const actions =
      app.snapshot().plans?.flatMap((plan) => plan.actions ?? []) ?? [];
    const deliveries = actions
      .flatMap((action) => action.deliveries ?? [])
      .filter((delivery) => delivery.file_name === fileName);
    if (deliveries.length !== 1)
      throw new Error(
        `报告中匹配的领取文件必须恰好有一条，实际为 ${deliveries.length}`,
      );
    const delivery = deliveries[0];
    const outputs = actions
      .flatMap((action) => action.outputs ?? [])
      .filter((output) => output.output_id === delivery.output_id);
    if (outputs.length !== 1)
      throw new Error(
        `报告中匹配的来源输出必须恰好有一条，实际为 ${outputs.length}`,
      );
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
    action_type: unknown;
    device_id: unknown;
    action_name: unknown;
    scheduled_at: string;
    cases: ParameterCase[];
  };
  const requiredInputString = (field: string, value: unknown): string => {
    if (typeof value !== "string" || value.trim().length === 0)
      throw new Error(`validate-parameters 输入字段 ${field} 必须是非空字符串`);
    return value;
  };
  const actionType = requiredInputString("action_type", input.action_type);
  const deviceId = requiredInputString("device_id", input.device_id);
  const actionName = requiredInputString("action_name", input.action_name);
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
        actionType,
        deviceId,
      }: {
        origin: string;
        capabilitiesText: string;
        cases: ParameterCase[];
        actionType: string;
        deviceId: string;
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
              validateParams(deviceId, actionType, params, capabilities)
                .length === 0,
            params: stringifyJson(params),
          };
        });
      },
      {
        origin,
        capabilitiesText,
        cases: input.cases,
        actionType,
        deviceId,
      },
    );
    const cases = input.cases.map((item, index) => {
      const params = parseJson(item.params_text);
      const sharedValid =
        validateParams(deviceId, actionType, params, capabilities).length === 0;
      const body = {
        name: "时间参数接缝",
        actions: [
          {
            name: actionName,
            type: actionType,
            device_id: deviceId,
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

async function capabilityState(storeDir: string, receiptPath: string) {
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
    const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
    const path = join(storeDir, "device-capabilities.json");
    const source = readFileSync(path, "utf8");
    const loaded = loadCapabilities(parseJson(source));
    const original = app.capabilities.active;
    const state = async () => {
      const response = await fetch(`${origin}/api/state`);
      if (!response.ok) throw new Error(`状态读取 HTTP ${response.status}`);
      return (
        parseJson(await response.text()) as {
          capabilities: { active: unknown };
        }
      ).capabilities.active;
    };
    const http = await state();
    const invalidReloads: boolean[] = [];
    for (const invalid of [null, 0, "false", {}, []]) {
      const replacement = loadCapabilities(parseJson(source));
      Object.assign(replacement.devices[0]!, { delete_supported: invalid });
      writeFileSync(path, stringifyJson(replacement));
      const reloaded = await fetch(`${origin}/api/capabilities/reload`, {
        method: "POST",
      });
      if (!reloaded.ok) throw new Error(`能力重载 HTTP ${reloaded.status}`);
      await reloaded.text();
      invalidReloads.push(
        app.capabilities.error !== null &&
          app.capabilities.active === original &&
          stringifyJson(await state()) === stringifyJson(http),
      );
    }
    const restarted = new Application(storeDir);
    const failedStartup =
      restarted.capabilities.active === null &&
      restarted.capabilities.error !== null;
    restarted.store.close();
    const legacy = loadCapabilities(parseJson(source));
    for (const device of legacy.devices) delete device.delete_supported;
    writeFileSync(path, stringifyJson(legacy));
    app.reloadCapabilities();
    if (app.capabilities.error !== null)
      throw new Error(app.capabilities.error);
    const legacyHttp = await state();
    writeFileSync(path, '{"devices":[]}');
    app.reloadCapabilities();
    if (app.capabilities.error !== null)
      throw new Error(app.capabilities.error);
    writeFileSync(
      receiptPath,
      stringifyJson({
        loaded,
        http,
        invalid_reloads: invalidReloads,
        failed_startup: failedStartup,
        legacy: legacyHttp,
        empty: await state(),
      }),
    );
  } finally {
    await stop();
  }
}

async function uploadFiles(
  storeDir: string,
  manifestPath: string,
  receiptPath: string,
  existing?: BrowserStore,
) {
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as Array<{
    path: string;
    kind: "report" | "media";
  }>;
  if (
    !Array.isArray(manifest) ||
    !manifest.length ||
    manifest.some(
      (item) =>
        typeof item.path !== "string" ||
        !["report", "media"].includes(item.kind),
    )
  )
    throw new Error("上传清单必须明确列出 report/media 文件");
  const app = existing?.app ?? openApp(storeDir),
    files = existing?.files ?? new Files(app);
  try {
    if (!existing) await files.recover();
    await files.idle();
    for (const item of manifest) {
      const info = await stat(item.path);
      if (!info.isFile()) throw new Error("上传路径不是普通文件");
      const batch = files.createBatch([
        { fileName: basename(item.path), size: info.size, kind: item.kind },
      ]);
      await files.upload(batch.files[0].id, createReadStream(item.path));
      await files.idle();
    }
    writeFileSync(
      receiptPath,
      stringifyJson({
        imports: app.store.all<ImportFile>("imports"),
        media: files.videos(),
        snapshot: app.snapshot(),
        saved_report_ids: app.store.reports().map((row) => row.report_id),
        ack_id: app.ackId(),
        coverage: app.coverage(),
        worker_error: files.workerError,
      }),
    );
  } finally {
    await files.idle();
    if (!existing) app.store.close();
  }
}

function errorRecord(error: unknown): Record<string, any> {
  if (!(error instanceof Error)) return { message: String(error) };
  return { message: error.message, stack: error.stack,
    ...(error instanceof AggregateError ? { causes: error.errors.map(errorRecord) } : {}) };
}

class SessionRecorder {
  readonly data: Record<string, any>;

  constructor(readonly id: string, readonly path: string) {
    this.data = {
      schema_version: 1,
      session_id: id,
      node_pid: process.pid,
      counts: { node_process_starts: 1, application_starts: 0, files_starts: 0,
        http_server_starts: 0, browser_starts: 0 },
      browser_processes: [],
      commands: [],
      node_cpu_scope: "process.cpuUsage只包含当前Node进程，不含Chromium或其他子进程。",
    };
    this.save();
  }

  save() {
    writeFileSync(`${this.path}.next`, JSON.stringify(this.data, null, 2) + "\n");
    renameSync(`${this.path}.next`, this.path);
  }

  opened(kind: "application_starts" | "files_starts" | "http_server_starts" | "browser_starts") {
    this.data.counts[kind]++;
    this.save();
  }

  browserProcess(pid: number) {
    const fields = readFileSync(`/proc/${pid}/stat`, "utf8").split(") ").at(-1)!.trim().split(/\s+/);
    const identity = { pid, pgid: Number(fields[2]), starttime: fields[19], active: true };
    this.data.browser_processes.push(identity);
    this.save();
    return identity;
  }
}

/** 服务与浏览器均真实重建；持久目录由调用方独占。 */
async function openBrowserStore(storeDir: string, recorder?: SessionRecorder) {
  const requireClient = createRequire(
    new URL("../../apps/client/package.json", import.meta.url),
  );
  const { chromium, expect: check } = requireClient("@playwright/test");
  const app = openApp(storeDir),
    files = new Files(app),
    requests = new RequestLifecycle();
  recorder?.opened("application_starts");
  recorder?.opened("files_starts");
  let server: Server | undefined;
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server ? server.close((error) => (error ? reject(error) : resolve())) : resolve(),
      ),
    requests,
    () => files.idle(),
    () => app.store.close(),
  );
  let browser;
  let browserServer;
  let browserIdentity: ReturnType<SessionRecorder["browserProcess"]> | undefined;
  let completion: Promise<void> | undefined;
  const close = () => {
    if (!completion) completion = (async () => {
      const errors: unknown[] = [];
      for (const operation of [
        () => files.idle(),
        () => browser?.close(),
        () => browserServer?.close(),
        () => stop(),
      ]) {
        try { await operation(); } catch (error) { errors.push(error); }
      }
      try { recorder?.save(); } catch (error) { errors.push(error); }
      if (errors.length) throw new AggregateError(errors, "客户端上下文收场失败");
    })();
    return completion;
  };
  try {
    await files.recover();
    server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
    await new Promise<void>((resolve, reject) => {
      server!.once("listening", resolve);
      server!.once("error", reject);
    });
    recorder?.opened("http_server_starts");
    const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
    if (recorder) {
      browserServer = await chromium.launchServer({ headless: true });
      const child = browserServer.process();
      browserIdentity = recorder.browserProcess(child.pid!);
      child.once("exit", () => {
        browserIdentity!.active = false;
        try { recorder.save(); } catch (error) {
          recorder.data.recording_error = errorRecord(error);
          console.error(error);
        }
      });
      browser = await chromium.connect(browserServer.wsEndpoint());
    } else {
      browser = await chromium.launch({ headless: true });
    }
    recorder?.opened("browser_starts");
    const context = await browser.newContext({
      acceptDownloads: true,
      timezoneId: "UTC",
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const response = await page.goto(origin);
    if (response?.status() !== 200) throw new Error("当前构建的首页不可访问");
    return {
      app,
      files,
      page,
      check,
      origin,
      async state() {
        const response = await fetch(`${origin}/api/state`);
        if (!response.ok) throw new Error(`状态 HTTP ${response.status}`);
        return parseClientJson(await response.text()) as any;
      },
      close,
    };
  } catch (error) {
    try { await close(); } catch (closing) {
      throw new AggregateError([error, closing], "客户端启动与收场均失败");
    }
    throw error;
  }
}

type BrowserStore = Awaited<ReturnType<typeof openBrowserStore>>;

class WorldClient {
  current?: BrowserStore;

  constructor(readonly storeDir: string, readonly recorder: SessionRecorder) {}

  async browser() {
    if (!this.current) this.current = await openBrowserStore(this.storeDir, this.recorder);
    return this.current;
  }

  async reopen() {
    await this.close();
    return this.browser();
  }

  async close() {
    const previous = this.current;
    this.current = undefined;
    if (previous) {
      const errors: unknown[] = [];
      try { await previous.close(); } catch (error) { errors.push(error); }
      if (previous.files.workerError) errors.push(new Error(`客户端文件worker失败：${previous.files.workerError}`));
      if (this.recorder.data.recording_error) errors.push(new Error(JSON.stringify(this.recorder.data.recording_error)));
      if (errors.length) throw new AggregateError(errors, "客户端会话收场失败");
    }
  }
}

async function browserPlanExport(
  storeDir: string,
  inputPath: string,
  planPath: string,
  receiptPath: string,
  owner?: WorldClient,
) {
  const session = owner ? await owner.browser() : await openBrowserStore(storeDir);
  try {
    const { page, check } = session;
    if (owner) await page.goto(session.origin);
    await page.getByTestId("new-draft-button").click();
    await page.getByTestId("draft-json-toggle").click();
    page.once("dialog", (dialog: any) => dialog.accept());
    await page
      .getByTestId("draft-json-input")
      .fill(readFileSync(inputPath, "utf8"));
    await check(page.getByTestId("save-status")).toHaveText("已保存");
    const event = page.waitForEvent("download");
    await page.getByTestId("export-button").click();
    const downloaded = await event;
    await downloaded.saveAs(planPath);
    const requestText = readFileSync(planPath, "utf8");
    const request = parseJson(requestText) as any;
    writeFileSync(
      receiptPath,
      stringifyJson({
        request_text: requestText,
        request_id: request.request_id,
        last_report_id: request.last_report_id ?? null,
      }),
    );
  } finally {
    if (!owner) await session.close();
  }
}

type PhotoInput = {
  device_id: string;
  action_type: string;
  action_name: string;
  case_name: string;
  params_text: string;
  scheduled_at: string;
  refresh_scheduled_at?: boolean;
};

async function applicationPhotoExport(
  storeDir: string,
  inputPath: string,
  planPath: string,
  receiptPath: string,
  owner: WorldClient,
) {
  const { app } = await owner.browser();
  const input = JSON.parse(readFileSync(inputPath, "utf8")) as PhotoInput;
  const selected = input.refresh_scheduled_at ? new Date() : undefined;
  const scheduledAt = selected
    ? selected.toISOString().slice(0, 19).replace("T", " ")
    : input.scheduled_at;
  const draft = app.createDraft(initializePreviewMetadata({
    text: stringifyJson({
      name: `${input.case_name}-applied`,
      actions: [{
        name: input.action_name,
        type: input.action_type,
        device_id: input.device_id,
        scheduled_at: scheduledAt,
        policy: { max_delay_ms: 60000 },
        params: parseClientJson(input.params_text),
      }],
    }),
  }, "disabled", randomUUID()));
  const exported = app.exportDraft(draft.id, draft.revision, draft.content);
  const saved = app.draft(draft.id);
  const request = app.request(exported.id);
  const body = app.downloadRequest(request.id);
  const requestText = stringifyJson(body);
  writeFileSync(planPath, requestText, "utf8");
  writeFileSync(receiptPath, stringifyJson({
    export_mode: "application",
    capabilities_sha256: createHash("sha256")
      .update(readFileSync(join(storeDir, "device-capabilities.json"))).digest("hex"),
    draft_id: saved.id,
    draft_revision: saved.revision,
    draft_text: saved.content.text,
    preview_intent: saved.content.automaticPreviews?.intent,
    draft_request_id: saved.exportedRequestId,
    request_draft_id: request.draftId,
    request_body_text: stringifyJson(request.body),
    request_id: request.id,
    request_text: requestText,
    created_at: request.body.created_at,
    has_ack: Object.hasOwn(body, "last_report_id"),
    last_report_id: body.last_report_id ?? null,
    ...(selected ? { schedule_refresh: {
      previous_scheduled_at: input.scheduled_at,
      scheduled_at: scheduledAt,
      selected_utc: selected.toISOString(),
    } } : {}),
  }));
}

async function browserPhotoExport(
  storeDir: string,
  inputPath: string,
  planPath: string,
  receiptPath: string,
  owner?: WorldClient,
) {
  const input = JSON.parse(readFileSync(inputPath, "utf8")) as PhotoInput;
  const params = parseClientJson(input.params_text) as any;
  const capabilitiesText = readFileSync(
    join(storeDir, "device-capabilities.json"),
    "utf8",
  );
  const capabilities = loadCapabilities(parseJson(capabilitiesText));
  const device = capabilities.devices.find(
    (item) => item.device_id === input.device_id,
  );
  const parameter = device?.actions
    .find((item) => item.type === input.action_type)
    ?.parameter_types.find((item) => item.type === params.type);
  if (!parameter) throw new Error("安装能力没有对应的参数类型");
  const schema = parameter.schema as any;
  const { choose } =
    await import("../../apps/client/tests/integration/select-support");
  let session = owner ? await owner.browser() : await openBrowserStore(storeDir);
  const receipt: Record<string, any> = {
    export_mode: "browser",
    capabilities_sha256: createHash("sha256")
      .update(capabilitiesText)
      .digest("hex"),
    server_open_count: 1,
    selected_options: [],
  };
  const settled = () =>
    session.check(session.page.getByTestId("save-status")).toHaveText("已保存");
  const seed = async (name: string) => {
    const { page } = session;
    await page.getByTestId("new-draft-button").click();
    await page.getByTestId("draft-json-toggle").click();
    page.once("dialog", (dialog: any) => dialog.accept());
    await page.getByTestId("draft-json-input").fill(
      stringifyJson({
        name,
        actions: [
          {
            name: input.action_name,
            type: input.action_type,
            device_id: input.device_id,
            scheduled_at: input.scheduled_at,
            policy: { max_delay_ms: 60000 },
            params: { type: params.type },
          },
        ],
      }),
    );
    await settled();
    await page.getByTestId("draft-json-toggle").click();
    const state = await session.state();
    const draft = state.drafts.find(
      (item: Draft) =>
        (parseClientJson(item.content.text) as any).name === name,
    );
    if (!draft) throw new Error("网页未保存新草稿");
    return draft;
  };
  const selectScalar = async (
    control: any,
    field: any,
    parent: any,
    key: string,
  ) => {
    const value = parent[key];
    const identity = exactJsonValueIdentity(value, true, { parent, key });
    const candidates = Array.isArray(field.oneOf)
      ? field.oneOf.map((branch: any) => ({
          parent: branch,
          key: "const",
          value: branch.const,
        }))
      : field.enum.map((item: any, index: number) => ({
          parent: field.enum,
          key: index,
          value: item,
        }));
    const index = candidates.findIndex(
      (item: any) =>
        exactJsonValueIdentity(item.value, true, {
          parent: item.parent,
          key: item.key,
        }) === identity,
    );
    if (index < 0) throw new Error(`安装 Schema 无精确选项 ${key}`);
    await choose(control, String(index));
    await settled();
    receipt.selected_options.push({
      field: key,
      selected_index: index,
      label: await control.innerText(),
    });
  };
  try {
    const source = await seed(input.case_name);
    receipt.source_draft_id = source.id;
    await selectScalar(
      session.page.getByRole("combobox", {
        name: `${schema.properties.capture_format.title} (capture_format)`,
        exact: true,
      }),
      schema.properties.capture_format,
      params,
      "capture_format",
    );
    for (const name of ["exposure", "white_balance", "focus"]) {
      const object = schema.properties[name];
      const index = object.oneOf.findIndex(
        (branch: any) => branch.properties.mode.const === params[name].mode,
      );
      if (index < 0) throw new Error(`安装 Schema 无模式 ${name}`);
      const branch = object.oneOf[index];
      const section = session.page.locator(".object-branch-field").filter({
        has: session.page.getByRole("heading", {
          name: new RegExp(`^${object.title}`),
        }),
      });
      await choose(
        section.getByRole("combobox", {
          name: `${branch.properties.mode.title} (mode)`,
          exact: true,
        }),
        `branch:${index}`,
      );
      await settled();
      for (const key of Object.keys(params[name]).filter(
        (key) => key !== "mode",
      )) {
        const field = branch.properties[key];
        const label = `${field.title ?? key} (${key})`;
        if (field.oneOf || field.enum) {
          await selectScalar(
            section.getByRole("combobox", { name: label, exact: true }),
            field,
            params[name],
            key,
          );
        } else {
          await section
            .getByLabel(label, { exact: true })
            .fill(stringifyJson(params[name][key]));
          await settled();
        }
      }
    }
    receipt.source_draft_text = (await session.state()).drafts.find(
      (item: Draft) => item.id === source.id,
    ).content.text;
    await session.page.getByText("拍摄参数预设", { exact: true }).click();
    await session.page
      .getByLabel("预设名称", { exact: true })
      .fill(input.case_name);
    const presetResponse = session.page.waitForResponse(
      (response: any) =>
        response.url().endsWith("/api/presets") &&
        response.request().method() === "POST",
    );
    await session.page
      .getByRole("button", { name: "保存为新预设", exact: true })
      .click();
    const savedResponse = await presetResponse;
    receipt.preset_http_status = savedResponse.status();
    receipt.preset_http_text = await savedResponse.text();
    await session
      .check(session.page.getByRole("status").filter({ hasText: "预设已保存" }))
      .toBeVisible();
    const preset = (await session.state()).presets.find(
      (item: any) => item.name === input.case_name,
    );
    receipt.preset_id = preset.id;
    receipt.preset_params_text = stringifyJson(preset.params);
    await session.page.screenshot({
      path: `${receiptPath}.source.png`,
      fullPage: true,
    });
    if (owner) session = await owner.reopen();
    else {
      await session.close();
      session = await openBrowserStore(storeDir);
    }
    receipt.server_open_count++;
    await session.page
      .getByTestId("draft-open-button")
      .filter({ hasText: input.case_name })
      .click();
    await settled();
    const reopened = await session.state();
    receipt.reopened_draft_text = reopened.drafts.find(
      (item: Draft) => item.id === source.id,
    ).content.text;
    const reopenedPreset = reopened.presets.find(
      (item: any) => item.id === preset.id,
    );
    receipt.reopened_preset_id = reopenedPreset.id;
    receipt.reopened_preset_params_text = stringifyJson(reopenedPreset.params);
    const target = await seed(`${input.case_name}-applied`);
    receipt.applied_draft_id = target.id;
    await session.page.getByText("拍摄参数预设", { exact: true }).click();
    await choose(
      session.page.getByRole("combobox", { name: "已有预设", exact: true }),
      preset.id,
    );
    await session.page
      .getByRole("button", { name: "应用预设", exact: true })
      .click();
    await settled();
    const preview = session.page.getByTestId("preview-intent");
    if ((await preview.innerText()).includes("尚未设置")) {
      await preview.click();
      await session.page
        .getByRole("menuitem", { name: "禁用预览", exact: true })
        .click();
    } else if ((await preview.innerText()).includes("已启用"))
      await preview.click();
    await settled();
    let applied = await session.state();
    receipt.applied_draft_text = applied.drafts.find(
      (item: Draft) => item.id === target.id,
    ).content.text;
    await session.page.screenshot({
      path: `${receiptPath}.applied.png`,
      fullPage: true,
    });
    if (input.refresh_scheduled_at) {
      // 网页预设往返与截图均已完成，再为真实执行选择时间。
      const selected = new Date();
      const scheduledAt = selected.toISOString().slice(0, 19).replace("T", " ");
      // datetime-local会将整分钟规范化为不带秒的控件值。
      await session.page
        .getByLabel("执行时间", { exact: true })
        .fill(scheduledAt.replace(" ", "T").replace(/:00$/, ""));
      await settled();
      await session.check.poll(async () => {
        const state = await session.state();
        const draft = state.drafts.find((item: Draft) => item.id === target.id);
        return (parseClientJson(draft.content.text) as any).actions[0].scheduled_at;
      }).toBe(scheduledAt);
      applied = await session.state();
      const expected = parseClientJson(receipt.applied_draft_text) as any;
      expected.actions[0].scheduled_at = scheduledAt;
      const actual = applied.drafts.find((item: Draft) => item.id === target.id).content.text;
      if (stringifyJson(parseClientJson(actual)) !== stringifyJson(expected))
        throw new Error("刷新执行时间改写了目标草稿的其他字段");
      receipt.applied_draft_text = actual;
      receipt.schedule_refresh = {
        previous_scheduled_at: input.scheduled_at,
        scheduled_at: scheduledAt,
        selected_utc: selected.toISOString(),
      };
    }
    if (
      receipt.source_draft_text !==
        applied.drafts.find((item: Draft) => item.id === source.id).content
          .text ||
      receipt.preset_params_text !==
        stringifyJson(
          applied.presets.find((item: any) => item.id === preset.id).params,
        )
    ) {
      throw new Error("应用预设或刷新时间改写了原草稿或预设");
    }
    const event = session.page.waitForEvent("download");
    await session.page.getByTestId("export-button").click();
    await (await event).saveAs(planPath);
    receipt.request_text = readFileSync(planPath, "utf8");
    receipt.request_id = (parseJson(receipt.request_text) as any).request_id;
    writeFileSync(receiptPath, stringifyJson(receipt));
  } catch (error) {
    if (!session.page.isClosed())
      await session.page.screenshot({
        path: `${receiptPath}.failure.png`,
        fullPage: true,
      });
    throw error;
  } finally {
    if (!owner) await session.close();
  }
}

/** Python 独占连接按序递交文件路径；业务 JSON 仍由原入口读写。 */
async function worldClientSession(
  storeDir: string, host: string, portText: string, sessionId: string, metricsPath: string,
) {
  const port = Number(portText);
  if (host !== "127.0.0.1" || !Number.isInteger(port) || port < 1 || port > 65535)
    throw new Error("客户端会话必须连接独占的loopback端口");
  const recorder = new SessionRecorder(sessionId, metricsPath);
  const owner = new WorldClient(storeDir, recorder);
  const socket = createConnection({ host, port });
  let primary: unknown;
  try {
    await new Promise<void>((resolve, reject) => {
      socket.once("connect", resolve);
      socket.once("error", reject);
    });
    recorder.data.status = "ready";
    recorder.data.node_cpu_at_ready_microseconds = process.cpuUsage();
    recorder.save();
    socket.write(JSON.stringify({ kind: "ready", session_id: sessionId, pid: process.pid }) + "\n");
    const lines = createInterface({ input: socket, crlfDelay: Infinity });
    let nextId = 1;
    let closed = false;
    for await (const line of lines) {
      const message = JSON.parse(line) as {
        id: number; session_id: string; operation: string; arguments: string[];
      };
      if (message.session_id !== sessionId || message.id !== nextId++ ||
          !Array.isArray(message.arguments) || message.arguments.some((value) => typeof value !== "string"))
        throw new Error("客户端会话命令身份或路径无效");
      const started = process.hrtime.bigint();
      const cpu = process.cpuUsage();
      const record: Record<string, any> = {
        id: message.id, operation: message.operation, arguments: message.arguments,
        started_monotonic_ns: started.toString(), outcome: "running",
        contexts_before: { ...recorder.data.counts },
      };
      recorder.data.commands.push(record);
      recorder.save();
      const finish = (outcome: string) => {
        const ended = process.hrtime.bigint();
        record.finished_monotonic_ns = ended.toString();
        record.elapsed_seconds = Number(ended - started) / 1e9;
        record.node_cpu_microseconds = process.cpuUsage(cpu);
        record.contexts_after = { ...recorder.data.counts };
        record.outcome = outcome;
        recorder.save();
      };
      try {
        const args = message.arguments;
        if (message.operation === "close" && args.length === 0) {
          await owner.close();
          recorder.data.status = "closed";
          finish("passed");
          socket.write(JSON.stringify({ kind: "result", session_id: sessionId, id: message.id, ok: true }) + "\n");
          closed = true;
          break;
        }
        if (message.operation === "browser-photo-export" && args.length === 3)
          await browserPhotoExport(storeDir, args[0], args[1], args[2], owner);
        else if (message.operation === "application-photo-export" && args.length === 3)
          await applicationPhotoExport(storeDir, args[0], args[1], args[2], owner);
        else if (message.operation === "browser-plan-export" && args.length === 3)
          await browserPlanExport(storeDir, args[0], args[1], args[2], owner);
        else if (message.operation === "upload-files" && args.length === 2)
          await uploadFiles(storeDir, args[0], args[1], await owner.browser());
        else throw new Error(`未知客户端会话操作或路径数量：${message.operation}`);
        const receiptPath = args.at(-1)!;
        const receipt = parseJson(readFileSync(receiptPath, "utf8")) as any;
        if (receipt.request_id !== undefined) record.request_id = String(receipt.request_id);
        finish("passed");
        socket.write(JSON.stringify({
          kind: "result", session_id: sessionId, id: message.id, ok: true, receipt_path: receiptPath,
        }) + "\n");
      } catch (error) {
        let failure = error;
        try { await owner.close(); } catch (closing) {
          failure = new AggregateError([failure, closing], "客户端命令与收场均失败");
        }
        record.error = errorRecord(failure);
        recorder.data.status = "failed";
        try { finish("failed"); } catch (logging) {
          failure = new AggregateError([failure, logging], "客户端失败及计时保存失败");
          record.error = errorRecord(failure);
        }
        console.error(failure);
        socket.write(JSON.stringify({
          kind: "result", session_id: sessionId, id: message.id, ok: false, error: record.error,
        }) + "\n");
        throw failure;
      }
    }
    if (!closed) throw new Error("客户端会话在close命令前断开");
  } catch (error) {
    primary = error;
  } finally {
    try { await owner.close(); } catch (closing) {
      primary = primary ? new AggregateError([primary, closing], "客户端命令与收场均失败") : closing;
    }
    recorder.data.status = primary ? "failed" : "closed";
    recorder.data.node_cpu_total_microseconds = process.cpuUsage();
    try { recorder.save(); } catch (logging) {
      primary = primary ? new AggregateError([primary, logging], "客户端失败及最终计时保存失败") : logging;
    }
    socket.end();
  }
  if (primary) throw primary;
}

async function main() {
  if (command === "session") {
    const [storeDir, host, port, sessionId, metricsPath] = rest;
    if (!storeDir || !host || !port || !sessionId || !metricsPath) badUsage();
    await worldClientSession(storeDir, host, port, sessionId, metricsPath);
  } else if (command === "initialize") {
    const [storeDir, receiptPath] = rest;
    if (!storeDir || !receiptPath) badUsage();
    initializeClient(storeDir, receiptPath);
  } else if (command === "browser-photo-export" || command === "browser-plan-export") {
    const [storeDir, inputPath, planPath, receiptPath] = rest;
    if (!storeDir || !inputPath || !planPath || !receiptPath) badUsage();
    if (command === "browser-photo-export")
      await browserPhotoExport(storeDir, inputPath, planPath, receiptPath);
    else await browserPlanExport(storeDir, inputPath, planPath, receiptPath);
  } else if (command === "upload-files") {
    const [storeDir, manifestPath, receiptPath] = rest;
    if (!storeDir || !manifestPath || !receiptPath) badUsage();
    await uploadFiles(storeDir, manifestPath, receiptPath);
  } else if (command === "capability-state") {
    const [storeDir, receiptPath] = rest;
    if (!storeDir || !receiptPath) badUsage();
    await capabilityState(storeDir, receiptPath);
  } else if (command === "validate-parameters") {
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
    "用法: client_driver.ts session <存储目录> <127.0.0.1> <端口> <会话身份> <计时路径> | initialize <存储目录> <凭据路径> | export <存储目录> <正文路径> <计划路径> <凭据路径> | import <报告目录> <存储目录> <凭据路径> | import-media <存储目录> <已领取文件路径> <凭据路径> | estimate <存储目录> <动作路径> <凭据路径> | validate-parameters <存储目录> <参数用例路径> <凭据路径> | reload-estimate <存储目录> <动作路径> <替换说明路径> <凭据路径> | capability-state <存储目录> <凭据路径> | browser-photo-export <存储目录> <输入路径> <计划路径> <凭据路径> | browser-plan-export <存储目录> <正文路径> <计划路径> <凭据路径> | upload-files <存储目录> <清单路径> <凭据路径>",
  );
  process.exit(2);
}
