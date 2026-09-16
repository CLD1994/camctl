import { fileMediaType, previewKind } from "../shared/media";
import express from "express";
import { createReadStream } from "node:fs";
import { resolve } from "node:path";
import type { Application } from "./application";
import type { Files } from "./files";
import { AppError, errorMessage } from "./models";
import { DataError } from "./database";
import { byteRange } from "./range";
import { RequestLifecycle } from "./lifecycle";

export function createHttpApp(
  application: Application,
  files: Files,
  requests = new RequestLifecycle(),
) {
  const app = express();
  app.disable("x-powered-by");
  const tracked =
    (
      handler: (
        req: express.Request,
        res: express.Response,
      ) => Promise<unknown>,
    ): express.RequestHandler =>
    async (req, res) => {
      await requests.run(() => handler(req, res));
    };
  app.use((req, res, next) => {
    if (requests.stopping)
      return res
        .status(503)
        .json({ code: "server_stopping", message: "服务正在停止" });
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer");
    const host = req.headers.host ?? "";
    if (!/^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/.test(host))
      return res
        .status(403)
        .json({ code: "invalid_host", message: "请通过 localhost 访问客户端" });
    if (!["GET", "HEAD", "OPTIONS"].includes(req.method)) {
      const origin = req.headers.origin;
      if (
        (origin &&
          origin !== `http://${host}` &&
          origin !== `https://${host}`) ||
        req.headers["sec-fetch-site"] === "cross-site"
      )
        return res.status(403).json({
          code: "origin_rejected",
          message: "不允许其他网站修改客户端数据",
        });
    }
    next();
  });
  app.put(
    "/api/imports/:id/content",
    tracked(async (req, res) =>
      res.json(await files.upload(String(req.params.id), req)),
    ),
  );
  app.use(express.json({ limit: "32mb" }));
  app.use((_req, res, next) => {
    if (requests.stopping)
      return res
        .status(503)
        .json({ code: "server_stopping", message: "服务正在停止" });
    next();
  });
  app.get("/api/state", (_req, res) => {
    const state = application.state();
    res.setHeader("Cache-Control", "no-store");
    res.json({
      ...state,
      ...(state.startup.state === "ready"
        ? { videos: files.videos(), workerError: files.workerError }
        : {}),
    });
  });
  app.post(
    "/api/initialize",
    tracked(async (_req, res) => {
      const state = application.store.initialize();
      await files.recover();
      res.json(state);
    }),
  );
  app.post("/api/capabilities/reload", (_req, res) =>
    res.json(application.reloadCapabilities()),
  );
  app.post("/api/drafts", (req, res) =>
    res.status(201).json(application.createDraft(req.body?.content)),
  );
  app.put("/api/drafts/:id", (req, res) =>
    res.json(
      application.saveDraft(
        String(req.params.id),
        req.body.revision,
        req.body.content,
      ),
    ),
  );
  app.delete("/api/drafts/:id", (req, res) => {
    application.deleteDraft(String(req.params.id), req.body?.revision);
    res.json({ deleted: true });
  });
  app.post("/api/drafts/:id/validate", (req, res) => {
    application.validateContent(req.body.content);
    res.json({ valid: true });
  });
  app.post("/api/drafts/:id/export", (req, res) =>
    res.json(
      application.exportDraft(
        String(req.params.id),
        req.body.revision,
        req.body.content,
      ),
    ),
  );
  app.post("/api/drafts/:id/actions", (req, res) =>
    res.json(
      application.appendAction(
        String(req.params.id),
        req.body.revision,
        req.body.action,
      ),
    ),
  );
  app.get("/api/requests/:id/download", (req, res) => {
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="plan-${req.params.id}.json"`,
    );
    res
      .type("application/json")
      .send(
        JSON.stringify(
          application.downloadRequest(String(req.params.id)),
          null,
          2,
        ) + "\n",
      );
  });
  app.post("/api/requests/:id/copy", (req, res) =>
    res.status(201).json(application.copyRequest(String(req.params.id))),
  );
  app.put("/api/requests/:id/handoff", (req, res) => {
    if (typeof req.body.marked !== "boolean")
      throw new AppError("invalid_marker", "递交标记必须是布尔值");
    res.json(application.markHandoff(String(req.params.id), req.body.marked));
  });
  app.post("/api/presets", (req, res) =>
    res.status(201).json(application.savePreset(req.body)),
  );
  app.put("/api/presets/:id", (req, res) =>
    res.json(
      application.savePreset({ ...req.body, id: String(req.params.id) }),
    ),
  );
  app.get("/api/sync", (req, res) =>
    res.json(application.syncParams(req.query.full === "true")),
  );
  app.post("/api/batches", (req, res) =>
    res.status(201).json(files.createBatch(req.body.files)),
  );
  app.post(
    "/api/imports/:id/fail",
    tracked(async (req, res) =>
      res.json(
        await files.failUpload(
          String(req.params.id),
          String(req.body?.message ?? "浏览器上传中断"),
        ),
      ),
    ),
  );
  app.get("/api/reports/:id/download", (req, res) => {
    const id = Number(req.params.id);
    if (!Number.isSafeInteger(id) || id <= 0)
      throw new AppError("invalid_report_id", "报告编号无效");
    const report = application.store.report(id);
    if (!report) throw new AppError("not_found", "报告原文不存在", 404);
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="${report.file_name}"`,
    );
    res.type("application/json").send(Buffer.from(report.bytes));
  });
  app.get(
    ["/api/videos/:id/content", "/api/media/:id/content"],
    tracked(async (req, res) => {
      const video = await files.openVideo(String(req.params.id));
      res.setHeader("Accept-Ranges", "bytes");
      res.setHeader("Cache-Control", "private, no-store");
      const mediaType = fileMediaType(application.snapshot(), video.fileName);
      res.setHeader("X-Content-Type-Options", "nosniff");
      if (req.query.download === "true" || !previewKind(mediaType))
        res.setHeader(
          "Content-Disposition",
          `attachment; filename*=UTF-8''${encodeURIComponent(video.fileName)}`,
        );
      else res.setHeader("Content-Disposition", "inline");
      res.type(
        previewKind(mediaType) ? mediaType! : "application/octet-stream",
      );
      let range: { start: number; end: number } | undefined;
      if (req.headers.range) {
        try {
          range = byteRange(req.headers.range, video.size);
        } catch (error) {
          res.setHeader("Content-Range", `bytes */${video.size}`);
          throw error;
        }
      }
      if (range) {
        res.status(206);
        res.setHeader(
          "Content-Range",
          `bytes ${range.start}-${range.end}/${video.size}`,
        );
        res.setHeader("Content-Length", range.end - range.start + 1);
      } else res.setHeader("Content-Length", video.size);
      const stream = createReadStream(video.path, range);
      stream.on("error", (error) => res.destroy(error));
      res.on("close", () => stream.destroy());
      stream.pipe(res);
    }),
  );
  app.use("/api", (_req, res) =>
    res.status(404).json({ code: "not_found", message: "接口不存在" }),
  );
  app.use(express.static(resolve("dist/web")));
  app.use(
    (
      error: unknown,
      _req: express.Request,
      res: express.Response,
      _next: express.NextFunction,
    ) => {
      if (res.headersSent) {
        res.end();
        return;
      }
      if (error instanceof AppError)
        res.status(error.status).json({
          code: error.code,
          message: error.message,
          issues: error.issues,
        });
      else if (error instanceof DataError)
        res.status(503).json({ code: error.code, message: error.message });
      else
        res
          .status(500)
          .json({ code: "operation_failed", message: errorMessage(error) });
    },
  );
  return app;
}
