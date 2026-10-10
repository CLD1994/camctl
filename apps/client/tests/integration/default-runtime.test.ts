import { expect, it } from "vitest";
import { execFile, spawn, type ChildProcess } from "node:child_process";
import { promisify } from "node:util";
import { createServer } from "node:net";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import type { Draft, ExportedRequest } from "../../src/server/models";
import type { Application } from "../../src/server/application";

const clientDirectory = resolve(import.meta.dirname, "../..");
const temporaryParent = resolve(
  process.env.CAMCTL_RUNTIME_TEST_ROOT ?? tmpdir(),
);
const execute = promisify(execFile);
type State = ReturnType<Application["state"]>;

it("真实 Node ESM 模块默认随机源可生成合法身份", async () => {
  const { stdout } = await execute(
    process.execPath,
    [
      "--import",
      "tsx",
      "--input-type=module",
      "-e",
      `
    import { CryptoRandomSource } from './src/domain/request-id.ts';
    const value = new CryptoRandomSource().next();
    if (value < 1n || value > 9223372036854775807n) throw new Error('request id out of range');
    process.stdout.write(value.toString());
  `,
    ],
    { cwd: clientDirectory, windowsHide: true, timeout: 10_000 },
  );
  expect(stdout).toMatch(/^[1-9][0-9]*$/);
  expect(BigInt(stdout)).toBeLessThanOrEqual(9223372036854775807n);
});

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((done, fail) => {
    server.once("error", fail);
    server.listen(0, "127.0.0.1", done);
  });
  const address = server.address();
  if (!address || typeof address === "string")
    throw new Error("测试未取得 TCP 端口");
  await new Promise<void>((done, fail) =>
    server.close((error) => (error ? fail(error) : done())),
  );
  return address.port;
}

interface Runtime {
  child: ChildProcess;
  exited: Promise<void>;
  base: string;
  output(): string;
}
async function start(
  data: string,
  evidence: string,
  run: string,
): Promise<Runtime> {
  const port = await freePort();
  const child = spawn(
    process.execPath,
    ["--import", "tsx", "src/server/main.ts"],
    {
      cwd: clientDirectory,
      env: {
        ...process.env,
        CAMCTL_DATA_DIR: data,
        HOST: "127.0.0.1",
        PORT: String(port),
      },
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stdout = "",
    stderr = "",
    spawnError: Error | undefined,
    ended = false;
  child.stdout!.on("data", (data) => {
    stdout += String(data);
  });
  child.stderr!.on("data", (data) => {
    stderr += String(data);
  });
  child.once("error", (error) => {
    spawnError = error;
  });
  const exited = new Promise<void>((done) =>
    child.once("close", () => {
      ended = true;
      writeFileSync(
        join(evidence, `${run}.log`),
        `stdout:\n${stdout}\nstderr:\n${stderr}\nexitCode:${child.exitCode} signal:${child.signalCode}\n`,
      );
      done();
    }),
  );
  const runtime: Runtime = {
    child,
    exited,
    base: `http://127.0.0.1:${port}`,
    output: () => `${stdout}\n${stderr}`,
  };
  try {
    const deadline = Date.now() + 10_000;
    while (Date.now() < deadline) {
      if (ended || spawnError)
        throw new Error(
          `main 启动失败: ${spawnError ?? child.exitCode}\n${runtime.output()}`,
        );
      // 本次子进程的监听输出是就绪前提，避免将端口竞争后的其他服务视为本次服务。
      if (stdout.includes(`http://localhost:${port}`)) {
        let response: Response | undefined;
        try {
          response = await fetch(`${runtime.base}/api/state`, {
            signal: AbortSignal.timeout(500),
          });
        } catch {
          /* 本次监听已建立，但 HTTP 尚未可读时继续有期限等待。 */
        }
        if (response?.ok && !ended) {
          const state = (await response.json()) as State;
          if (state.startup.directory !== data)
            throw new Error("本次端口响应不属于启动的数据目录");
          return runtime;
        }
      }
      await new Promise((done) => setTimeout(done, 25));
    }
    throw new Error(`main 就绪超时\n${runtime.output()}`);
  } catch (error) {
    await stop(runtime);
    throw error;
  }
}
async function stop(runtime: Runtime): Promise<void> {
  if (runtime.child.exitCode === null && runtime.child.signalCode === null)
    runtime.child.kill("SIGTERM");
  async function awaitExit() {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        runtime.exited,
        new Promise<never>((_done, fail) => {
          timer = setTimeout(
            () => fail(new Error(`main 未退出\n${runtime.output()}`)),
            5000,
          );
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  try {
    await awaitExit();
  } catch (error) {
    // 关闭超时仍是测试失败；强制终止只用于释放本测试拥有的子进程。
    runtime.child.kill("SIGKILL");
    await awaitExit();
    throw error;
  }
}
async function json<T>(
  runtime: Runtime,
  path: string,
  status = 200,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`${runtime.base}${path}`, {
    method,
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    signal: AbortSignal.timeout(5000),
  });
  const text = await response.text();
  expect(
    response.status,
    `${method} ${path}: ${text}\n${runtime.output()}`,
  ).toBe(status);
  return JSON.parse(text) as T;
}
async function download(runtime: Runtime, id: string): Promise<Buffer> {
  const response = await fetch(`${runtime.base}/api/requests/${id}/download`, {
    signal: AbortSignal.timeout(5000),
  });
  expect(response.status).toBe(200);
  expect(response.headers.get("content-type")).toContain("application/json");
  expect(response.headers.get("content-disposition")).toContain(
    `plan-${id}.json`,
  );
  return Buffer.from(await response.arrayBuffer());
}

it("真实 main 默认导出经保存下载与进程重启后保持固定请求", async () => {
  const root = mkdtempSync(join(temporaryParent, "camctl-runtime-"));
  const data = join(root, "data"),
    evidence = join(root, "evidence");
  mkdirSync(data);
  mkdirSync(evidence);
  writeFileSync(join(data, "device-capabilities.json"), '{"devices":[]}');
  let runtime: Runtime | undefined;
  try {
    runtime = await start(data, evidence, "before");
    expect((await json<State>(runtime, "/api/state")).startup.state).toBe(
      "uninitialized",
    );
    expect(
      (
        await json<{ state: string }>(
          runtime,
          "/api/initialize",
          200,
          "POST",
          {},
        )
      ).state,
    ).toBe("ready");
    let state = await json<State>(runtime, "/api/state");
    expect(state.startup.state).toBe("ready");
    expect(state.capabilities.error).toBeNull();
    expect(state.capabilities.active).toEqual({ devices: [] });
    expect(state.capabilities.version).toEqual(expect.any(String));
    const content = {
      text: JSON.stringify({
        name: "默认入口验收",
        actions: [
          {
            name: "完整同步",
            type: "report_status",
            params: { scope: "full" },
          },
        ],
      }),
    };
    const draft = await json<Draft>(runtime, "/api/drafts", 201, "POST", {
      content,
    });
    const saved = await json<Draft>(
      runtime,
      `/api/drafts/${draft.id}`,
      200,
      "PUT",
      { revision: draft.revision, content: draft.content },
    );
    state = await json<State>(runtime, "/api/state");
    expect(state.drafts?.find((d) => d.id === saved.id)).toEqual(saved);
    const fixed = await json<ExportedRequest>(
      runtime,
      `/api/drafts/${saved.id}/export`,
      200,
      "POST",
      {
        revision: saved.revision,
        content: saved.content,
        capabilityVersion: state.capabilities.version,
      },
    );
    expect(fixed.id).toMatch(/^[1-9][0-9]*$/);
    expect(BigInt(fixed.id)).toBeGreaterThan(0n);
    expect(BigInt(fixed.id)).toBeLessThanOrEqual(9223372036854775807n);
    expect(fixed.body).toMatchObject({
      request_id: fixed.id,
      name: "默认入口验收",
      actions: [
        { name: "完整同步", type: "report_status", params: { scope: "full" } },
      ],
    });
    expect(fixed.body.created_at).toMatch(
      /^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$/,
    );
    const before = await download(runtime, fixed.id);
    expect(JSON.parse(before.toString())).toEqual(fixed.body);
    const originalCapabilityVersion = state.capabilities.version;
    const originalBase = runtime.base;
    await stop(runtime);
    runtime = undefined;
    runtime = await start(data, evidence, "after");
    state = await json<State>(runtime, "/api/state");
    expect(state.startup.state).toBe("ready");
    expect(state.requests).toEqual([fixed]);
    const recovered = state.drafts!.find((d) => d.id === saved.id)!;
    expect(recovered.exportedRequestId).toBe(fixed.id);
    expect(await download(runtime, fixed.id)).toEqual(before);
    expect(
      await json<ExportedRequest>(
        runtime,
        `/api/drafts/${saved.id}/export`,
        200,
        "POST",
        {
          revision: recovered.revision,
          content: recovered.content,
          capabilityVersion: state.capabilities.version,
        },
      ),
    ).toEqual(fixed);
    expect((await json<State>(runtime, "/api/state")).requests).toEqual([
      fixed,
    ]);
    const result = {
      at: new Date().toISOString(),
      node: process.version,
      platform: process.platform,
      requestId: fixed.id,
      savedRevision: saved.revision,
      originalCapabilityVersion,
      recoveredCapabilityVersion: state.capabilities.version,
      originalBase,
      recoveredBase: runtime.base,
      dataDirectory: data,
      bytes: before.length,
      restartState: state.startup.state,
      stopMode:
        process.platform === "win32" ? "process termination" : "SIGTERM",
      fixed,
    };
    if (process.env.CAMCTL_RUNTIME_EVIDENCE_FILE)
      writeFileSync(
        process.env.CAMCTL_RUNTIME_EVIDENCE_FILE,
        JSON.stringify(result, null, 2),
      );
  } finally {
    if (runtime) await stop(runtime);
    // root 是本测试在指定临时目录中创建的目录；进程退出后方可移除。
    if (
      !root.startsWith(`${temporaryParent}\\`) &&
      !root.startsWith(`${temporaryParent}/`)
    )
      throw new Error("临时目录超出测试范围");
    rmSync(root, { recursive: true, force: true });
  }
}, 30_000);
