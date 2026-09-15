import { afterEach, expect, it } from "vitest";
import { mkdtempSync, rmSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Server } from "node:http";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { mappedReport, reportInput } from "./fixtures";
const clean: Array<() => Promise<void>> = [];
afterEach(async () => {
  for (const c of clean.splice(0)) await c();
});
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
  const exported = await fetch(`${base}/api/drafts/${d.id}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, revision: d.revision }),
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
  const input = reportInput({ report_id: 1, from_wm: 0, to_wm: 0 });
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

it("HTTP 混合反序报告、视频补发与 Range 使用同一权威结果", async () => {
  const base = await setup();
  await fetch(base + "/api/initialize", { method: "POST" });
  const good = Buffer.from("correct-video");
  const bad = Buffer.from("wrong");
  const first = mappedReport(good);
  const second = structuredClone(first);
  second.report_id = 2;
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
