import { afterEach, expect, it } from "vitest";
import {
  mkdtempSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Server } from "node:http";
import { Readable } from "node:stream";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { mappedReport, reportInput, deliveryFileName } from "./fixtures";
const clean: Array<() => Promise<void>> = [];
afterEach(async () => {
  for (const c of clean.splice(0)) await c();
});

it.each(["play", "download", "range"] as const)(
  "资格在等待中失效时 HTTP %s 不发送视频",
  async (mode) => {
    const dir = mkdtempSync(join(tmpdir(), "camctl-http-gate-"));
    const app = new Application(dir);
    app.store.initialize();
    const initial = new Files(app);
    const bytes = Buffer.from("video");
    app.applyReports([reportInput(mappedReport(bytes))]);
    const file = initial.createBatch([
      { fileName: deliveryFileName(), size: bytes.length, kind: "video" },
    ]).files[0];
    await initial.upload(file.id, Readable.from([bytes]));
    await initial.idle();
    let entered!: () => void;
    let release!: () => void;
    let calls = 0;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const worker = new Files(app, {
      async fingerprint() {
        throw Error("此检查不应重新核验摘要");
      },
      async remove() {
        throw Error("此检查不应清理文件");
      },
      async readable() {
        if (++calls === 1) {
          entered();
          await gate;
        } else throw Error("无法读取");
      },
    });
    const server = await new Promise<Server>((resolve) => {
      const s = createHttpApp(app, worker).listen(0, "127.0.0.1", () =>
        resolve(s),
      );
    });
    const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
    clean.push(async () => {
      release();
      await worker.idle();
      await new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      );
      app.store.close();
      rmSync(dir, { recursive: true, force: true });
    });
    const pending = fetch(
      `${base}/api/videos/${file.id}/content${mode === "download" ? "?download=true" : ""}`,
      { headers: mode === "range" ? { Range: "bytes=0-1" } : {} },
    );
    await started;
    await expect(worker.openVideo(file.id)).rejects.toThrow();
    release();
    const response = await pending;
    expect(response.status).toBe(409);
    expect(response.headers.get("content-type")).toContain("application/json");
    expect((await response.json()).code).toBe("video_not_verified");
    expect(worker.videos()[0].status).toBe("unavailable");
  },
);
async function setup() {
  const dir = mkdtempSync(join(tmpdir(), "camctl-http-"));
  const app = new Application(dir);
  const files = new Files(app);
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(app, files).listen(0, "127.0.0.1", () =>
      resolve(s),
    );
  });
  const address = server.address() as { port: number };
  clean.push(async () => {
    await files.idle();
    await new Promise<void>((r, j) => server.close((e) => (e ? j(e) : r())));
    app.store.close();
    rmSync(dir, { recursive: true, force: true });
  });
  return `http://127.0.0.1:${address.port}`;
}
it("HTTP 显式初始化后保存并导出计划", async () => {
  const base = await setup();
  expect((await (await fetch(base + "/api/state")).json()).startup.state).toBe(
    "uninitialized",
  );
  await fetch(base + "/api/initialize", { method: "POST" });
  const content = {
    text: JSON.stringify({
      name: "状态",
      actions: [
        { name: "同步", type: "report_status", params: { scope: "full" } },
      ],
    }),
  };
  const created = await fetch(base + "/api/drafts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  const d = await created.json();
  expect(created.status).toBe(201);
  const missingVersion = await fetch(`${base}/api/drafts/${d.id}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, revision: d.revision }),
  });
  expect(missingVersion.status).toBe(409);
  const observation = await (await fetch(base + "/api/state")).json();
  expect(observation.requests).toEqual([]);
  const exported = await fetch(`${base}/api/drafts/${d.id}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      content,
      revision: d.revision,
      capabilityVersion: observation.capabilities.version,
    }),
  });
  expect(exported.status).toBe(200);
  const r = await exported.json();
  const downloaded = await (
    await fetch(`${base}/api/requests/${r.id}/download`)
  ).json();
  expect(downloaded.request_id).toBe(r.id);
});
it("跨来源写入被拒绝且数据库未初始化", async () => {
  const base = await setup();
  const result = await fetch(base + "/api/initialize", {
    method: "POST",
    headers: { Origin: "https://other.example" },
  });
  expect(result.status).toBe(403);
  expect((await (await fetch(base + "/api/state")).json()).startup.state).toBe(
    "uninitialized",
  );
});
it.each(["application/json", "application/octet-stream", undefined])(
  "报告上传保持原字节，MIME=%s",
  async (contentType) => {
    const base = await setup();
    await fetch(base + "/api/initialize", { method: "POST" });
    const input = reportInput(mappedReport(Buffer.from("video")));
    const batch = await (
      await fetch(base + "/api/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: [
            {
              fileName: input.file.fileName,
              size: input.bytes.length,
              kind: "report",
            },
          ],
        }),
      })
    ).json();
    const result = await fetch(
      `${base}/api/imports/${batch.files[0].id}/content`,
      {
        method: "PUT",
        headers: contentType ? { "Content-Type": contentType } : {},
        body: input.bytes,
      },
    );
    expect(result.status).toBe(200);
    let state;
    for (let i = 0; i < 100; i++) {
      state = await (await fetch(base + "/api/state")).json();
      if (state.coverage === 20) break;
      await new Promise((r) => setTimeout(r, 5));
    }
    expect(state.coverage).toBe(20);
    const original = await fetch(`${base}/api/reports/1/download`);
    expect(Buffer.from(await original.arrayBuffer())).toEqual(input.bytes);
  },
);
it("畸形 JSON 原文件到达逐文件诊断", async () => {
  const base = await setup();
  await fetch(base + "/api/initialize", { method: "POST" });
  const input = reportInput({ report_id: "1", from_wm: 0, to_wm: 0 });
  const bytes = Buffer.from("{bad");
  const batch = await (
    await fetch(base + "/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        files: [
          { fileName: input.file.fileName, size: bytes.length, kind: "report" },
        ],
      }),
    })
  ).json();
  expect(
    (
      await fetch(`${base}/api/imports/${batch.files[0].id}/content`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: new Uint8Array(bytes),
      })
    ).status,
  ).toBe(200);
  let state;
  for (let i = 0; i < 100; i++) {
    state = await (await fetch(base + "/api/state")).json();
    if (state.imports[0].status === "failed") break;
    await new Promise((r) => setTimeout(r, 5));
  }
  expect(state.imports[0].status).toBe("failed");
  expect(state.coverage).toBe(0);
});

it("HTTP 状态与能力重载保留每台设备的删除声明", async () => {
  const dir = mkdtempSync(join(tmpdir(), "camctl-http-capabilities-"));
  const file = join(dir, "device-capabilities.json");
  const document = {
    devices: [
      {
        device_id: "camera-alpha",
        driver_id: "vendor.future_camera_v9",
        delete_supported: true,
        actions: [],
      },
      {
        device_id: "camera-beta",
        driver_id: "vendor.future_camera_v9",
        delete_supported: false,
        actions: [],
      },
    ],
  };
  writeFileSync(file, JSON.stringify(document));
  const app = new Application(dir);
  const files = new Files(app);
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(app, files).listen(0, "127.0.0.1", () =>
      resolve(s),
    );
  });
  const address = server.address() as { port: number };
  const base = "http://127.0.0.1:" + address.port;
  clean.push(async () => {
    await files.idle();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
    app.store.close();
    rmSync(dir, { recursive: true, force: true });
  });

  const initial = await (await fetch(base + "/api/state")).json();
  expect(
    initial.capabilities.active.devices.map((device: any) => [
      device.device_id,
      device.delete_supported,
    ]),
  ).toEqual([
    ["camera-alpha", true],
    ["camera-beta", false],
  ]);

  document.devices[0].delete_supported = false;
  writeFileSync(file, JSON.stringify(document));
  const reloaded = await (
    await fetch(base + "/api/capabilities/reload", { method: "POST" })
  ).json();
  expect(reloaded.error).toBeNull();
  expect(reloaded.active.devices[0].delete_supported).toBe(false);
  const current = await (await fetch(base + "/api/state")).json();
  expect(current.capabilities.active.devices[0].delete_supported).toBe(false);
});

it("HTTP 状态与重载保留预览和估算声明的精确数字文本", async () => {
  const dir = mkdtempSync(join(tmpdir(), "camctl-http-capability-numbers-"));
  const file = join(dir, "device-capabilities.json");
  const text = [
    '{"devices":[',
    '{"device_id":"camera","driver_id":"vendor.future_camera_v9",',
    '"delete_supported":true,"actions":[',
    '{"type":"camera_record","parameter_types":[',
    '{"type":"exact","name":"exact","description":"exact","preview_supported":true,',
    '"schema":{"$schema":"https://json-schema.org/draft/2020-12/schema",',
    '"type":"object","properties":{"type":{"const":"exact"}},',
    '"required":["type"],"additionalProperties":false},',
    '"video_size_estimate":{"bitrate_mbps":130.125,',
    '"duration":{"method":"direct","seconds":{"source":"constant","value":2.4e2}}}',
    '}]}]}]}',
  ].join("");
  writeFileSync(file, text);
  const app = new Application(dir);
  const files = new Files(app);
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(app, files).listen(0, "127.0.0.1", () =>
      resolve(s),
    );
  });
  const address = server.address() as { port: number };
  const base = "http://127.0.0.1:" + address.port;
  clean.push(async () => {
    await files.idle();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
    app.store.close();
    rmSync(dir, { recursive: true, force: true });
  });

  const stateText = await (await fetch(base + "/api/state")).text();
  expect(stateText).toContain('"delete_supported":true');
  expect(stateText).toContain('"preview_supported":true');
  expect(stateText).toContain('"bitrate_mbps":130.125');
  expect(stateText).toContain('"value":2.4e2');

  const reloadText = await (
    await fetch(base + "/api/capabilities/reload", { method: "POST" })
  ).text();
  expect(reloadText).toContain('"delete_supported":true');
  expect(reloadText).toContain('"bitrate_mbps":130.125');
  expect(reloadText).toContain('"value":2.4e2');
});

it("HTTP 混合反序报告、视频补发与 Range 使用同一权威结果", async () => {
  const base = await setup();
  await fetch(base + "/api/initialize", { method: "POST" });
  const good = Buffer.from("correct-video");
  const bad = Buffer.from("wrong");
  const first = mappedReport(good);
  const second = structuredClone(first);
  second.report_id = "2";
  second.from_wm = 20;
  second.to_wm = 30;
  const reports = [reportInput(first), reportInput(second)];
  const name = first.plans![0].actions!.find(
    (a) => a.type === "obtain_action_outputs",
  )!.deliveries![0].file_name;
  const create = async (files: unknown[]) => {
    const response = await fetch(base + "/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files }),
    });
    expect(response.status).toBe(201);
    return response.json();
  };
  const put = async (id: string, bytes: Buffer) =>
    expect(
      (
        await fetch(`${base}/api/imports/${id}/content`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: new Uint8Array(bytes),
        })
      ).status,
    ).toBe(200);
  const state = async () => (await fetch(base + "/api/state")).json();
  const until = async (test: (s: any) => boolean) => {
    let current;
    for (let i = 0; i < 100; i++) {
      current = await state();
      if (test(current)) return current;
      await new Promise((r) => setTimeout(r, 5));
    }
    throw Error("状态未到达：" + JSON.stringify(current));
  };
  const batch = await create([
    { fileName: name, size: bad.length, kind: "video" },
    ...reports.map((r) => ({
      fileName: r.file.fileName,
      size: r.bytes.length,
      kind: "report",
    })),
  ]);
  await put(batch.files[0].id, bad);
  await until((s) => s.videos[0]?.status === "waiting_report");
  await put(batch.files[2].id, reports[1].bytes);
  await put(batch.files[1].id, reports[0].bytes);
  const mismatch = await until(
    (s) => s.coverage === 30 && s.videos[0]?.status === "mismatch",
  );
  expect(mismatch.reports).toHaveLength(2);
  const replacement = await create([
    { fileName: name, size: good.length, kind: "video" },
  ]);
  await put(replacement.files[0].id, good);
  const verified = await until((s) => s.videos[0]?.status === "verified");
  const video = verified.videos[0];
  expect(video.id).toBe(replacement.files[0].id);
  const range = await fetch(`${base}/api/videos/${video.id}/content`, {
    headers: { Range: "bytes=2-5" },
  });
  expect(range.status).toBe(206);
  expect(range.headers.get("Content-Range")).toBe(`bytes 2-5/${good.length}`);
  expect(Buffer.from(await range.arrayBuffer())).toEqual(good.subarray(2, 6));
  expect(
    Buffer.from(
      await (await fetch(base + "/api/reports/1/download")).arrayBuffer(),
    ),
  ).toEqual(reports[0].bytes);
  await fetch(`${base}/api/imports/${video.id}/fail`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "迟到的断线提示" }),
  });
  expect((await state()).videos[0].status).toBe("verified");
  unlinkSync(video.path);
  expect((await fetch(`${base}/api/videos/${video.id}/content`)).status).toBe(
    409,
  );
  const unavailable = await state();
  expect(unavailable.videos[0].status).toBe("unavailable");
  expect(unavailable.imports.find((f: any) => f.id === video.id).status).toBe(
    "unavailable",
  );
});
