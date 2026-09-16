import { it, expect } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { mappedReport, reportInput } from "./fixtures";

export const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1cAAAAASUVORK5CYII=",
  "base64",
);
it.each([
  ["before", "image/png"],
  ["after", "image/png"],
  ["before", "image/svg+xml"],
  ["before", undefined],
] as const)(
  "媒体在报告 %s 到达，类型 %s 的读取资格正确",
  async (order, mediaType) => {
    const dir = mkdtempSync(join(tmpdir(), "camctl-image-"));
    const app = new Application(dir);
    app.store.initialize();
    const files = new Files(app);
    const r = mappedReport(png);
    const capture = r.plans![0].actions![0];
    capture.type = "camera_take_photo";
    capture.result = { capture: { status: "completed", captured_count: 1 } };
    capture.outputs![0].media_type = mediaType;
    r.plans![0].actions![1].deliveries![0].file_name = "d-001.png";
    const server = createHttpApp(app, files).listen(0, "127.0.0.1");
    await new Promise<void>((resolve) => server.once("listening", resolve));
    try {
      if (order === "before") app.applyReports([reportInput(r)]);
      const f = files.createBatch([
        { fileName: "d-001.png", size: png.length, kind: "media" },
      ]).files[0];
      await files.upload(f.id, Readable.from([png]));
      await files.idle();
      if (order === "after") {
        expect(files.videos()[0].status).toBe("waiting_report");
        const data = reportInput(r);
        const rf = files.createBatch([
          {
            fileName: data.file.fileName,
            size: data.bytes.length,
            kind: "report",
          },
        ]).files[0];
        await files.upload(rf.id, Readable.from([data.bytes]));
        await files.idle();
      }
      expect(files.videos()[0].status).toBe("verified");
      const recovered = new Files(app);
      await recovered.recover();
      await recovered.idle();
      const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
      const response = await fetch(`${base}/api/videos/${f.id}/content`);
      expect(response.headers.get("content-type")).toContain(
        mediaType === "image/png" ? "image/png" : "application/octet-stream",
      );
      expect(response.headers.get("content-disposition")).toMatch(
        mediaType === "image/png" ? /^inline/ : /^attachment/,
      );
      expect(response.headers.get("x-content-type-options")).toBe("nosniff");
      expect(Buffer.from(await response.arrayBuffer())).toEqual(png);
    } finally {
      await files.idle();
      await new Promise<void>((resolve) => server.close(() => resolve()));
      app.store.close();
      rmSync(dir, { recursive: true, force: true });
    }
  },
);
