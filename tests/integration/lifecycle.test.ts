import { afterEach, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { request, type Server } from "node:http";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";

const clean: Array<() => Promise<void>> = [];
afterEach(async () => {
  for (const item of clean.splice(0)) await item();
});
function barrier() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { release, promise };
}
async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-stop-"));
  const app = new Application(directory);
  app.store.initialize();
  const files = new Files(app);
  const requests = new RequestLifecycle();
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(app, files, requests).listen(0, "127.0.0.1", () =>
      resolve(s),
    );
  });
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      ),
    requests,
    () => files.idle(),
    () => app.store.close(),
  );
  clean.push(async () => {
    if (server.listening) await stop();
    app.store.close();
    rmSync(directory, { recursive: true, force: true });
  });
  return {
    app,
    files,
    stop,
    base: `http://127.0.0.1:${(server.address() as { port: number }).port}`,
  };
}
it("真实分段上传结束并完成后台核验后才停机", async () => {
  const { app, files, stop, base } = await setup();
  const file = files.createBatch([
    { fileName: "clip.webm", size: 6, kind: "video" },
  ]).files[0];
  const entered = barrier();
  const original = files.upload.bind(files);
  vi.spyOn(files, "upload").mockImplementation((...args) => {
    entered.release();
    return original(...args);
  });
  const closed = vi.spyOn(app.store, "close");
  const req = request(`${base}/api/imports/${file.id}/content`, {
    method: "PUT",
    headers: { "Content-Length": "6" },
  });
  const response = new Promise<number>((resolve) =>
    req.on("response", (res) => {
      res.resume();
      res.on("end", () => resolve(res.statusCode!));
    }),
  );
  req.write("abc");
  await entered.promise;
  const stopped = stop();
  expect(closed).not.toHaveBeenCalled();
  req.end("def");
  expect(await response).toBe(200);
  await stopped;
  expect(closed).toHaveBeenCalledOnce();
  const restored = new Application(app.store.directory);
  expect(restored.store.all<{ status: string }>("videos")[0].status).toBe(
    "waiting_report",
  );
  restored.store.close();
});
it("HTTP断连后仍等待上传处理的收尾，不以连接关闭代替处理完成", async () => {
  const { app, files, stop, base } = await setup();
  const file = files.createBatch([
    { fileName: "clip.webm", size: 6, kind: "video" },
  ]).files[0];
  const entered = barrier();
  const release = barrier();
  const cleanup = barrier();
  const original = files.upload.bind(files);
  vi.spyOn(files, "upload").mockImplementation(async (...args) => {
    entered.release();
    try {
      return await original(...args);
    } finally {
      cleanup.release();
      await release.promise;
    }
  });
  const closed = vi.spyOn(app.store, "close");
  const req = request(`${base}/api/imports/${file.id}/content`, {
    method: "PUT",
    headers: { "Content-Length": "6" },
  });
  req.on("error", () => {});
  req.write("abc");
  await entered.promise;
  const stopped = stop();
  req.destroy();
  await cleanup.promise;
  expect(closed).not.toHaveBeenCalled();
  release.release();
  await stopped;
  expect(closed).toHaveBeenCalledOnce();
});
it("视频可用性检查在连接断开后仍受停机等待保护", async () => {
  const { app, files, stop, base } = await setup();
  const entered = barrier();
  const release = barrier();
  vi.spyOn(files, "openVideo").mockImplementation(async () => {
    entered.release();
    await release.promise;
    throw new Error("文件不可读");
  });
  const closed = vi.spyOn(app.store, "close");
  const req = request(`${base}/api/videos/missing/content`);
  req.on("error", () => {});
  req.end();
  await entered.promise;
  const stopped = stop();
  req.destroy();
  expect(closed).not.toHaveBeenCalled();
  release.release();
  await stopped;
  expect(closed).toHaveBeenCalledOnce();
});
