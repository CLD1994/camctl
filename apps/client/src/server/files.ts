import { randomUUID, createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, open, readFile, stat, unlink } from "node:fs/promises";
import { join, relative } from "node:path";
import { Readable, Transform } from "node:stream";
import { isDeepStrictEqual } from "node:util";
import { pipeline } from "node:stream/promises";
import { DataError } from "./database";
import type { Application } from "./application";
import {
  AppError,
  errorMessage,
  type ImportBatch,
  type ImportFile,
  type Video,
} from "./models";

interface CompleteFile {
  id: string;
  path: string;
  size: number;
  fileName: string;
  importId: string;
  processed: boolean;
}
interface Mapping {
  size: number;
  sha256: string;
}
interface Change {
  namespace: string;
  id: string;
  value: unknown;
}
interface Identity {
  fileName: string;
  id: string | undefined;
}
export interface FileEffects {
  fingerprint(path: string): Promise<Mapping>;
  readable(path: string, size: number): Promise<void>;
  remove(path: string): Promise<void>;
}
async function fingerprint(path: string): Promise<Mapping> {
  const hash = createHash("sha256");
  let size = 0;
  for await (const chunk of createReadStream(path)) {
    size += chunk.length;
    hash.update(chunk);
  }
  return { size, sha256: hash.digest("hex") };
}
function same(a: Mapping, b: Mapping) {
  return a.size === b.size && a.sha256 === b.sha256;
}
const effects: FileEffects = {
  fingerprint,
  remove: unlink,
  async readable(path, size) {
    const info = await stat(path);
    if (!info.isFile() || info.size !== size)
      throw new AppError("file_changed", "当前文件与完整保存记录不符");
    const fd = await open(path, "r");
    await fd.close();
  },
};

export class Files {
  private queue: Promise<void> = Promise.resolve();
  private reportQueue: Promise<void> = Promise.resolve();
  private activeUploads = new Set<string>();
  private processingBatches = new Set<string>();
  workerError: string | null = null;
  constructor(
    readonly app: Application,
    private readonly io: FileEffects = effects,
  ) {}
  private get store() {
    return this.app.store;
  }
  private complete(id: string): CompleteFile | undefined {
    const row = this.store.get<CompleteFile>("complete_files", id);
    return row
      ? { ...row, path: join(this.store.directory, row.path) }
      : undefined;
  }
  private saveComplete(row: CompleteFile) {
    this.store.set("complete_files", row.id, {
      ...row,
      path: relative(this.store.directory, row.path),
    });
  }
  private file(id: string): ImportFile {
    const f = this.store.get<ImportFile>("imports", id);
    if (!f) throw new AppError("not_found", "导入文件不存在", 404);
    return f;
  }
  private update(id: string, patch: Partial<ImportFile>) {
    this.store.set("imports", id, { ...this.file(id), ...patch });
  }
  private current(fileName: string): Video | undefined {
    const row = this.store.get<Video>("videos", fileName);
    return row
      ? { ...row, path: join(this.store.directory, row.path) }
      : undefined;
  }
  private completeChange(row: CompleteFile): Change {
    return {
      namespace: "complete_files",
      id: row.id,
      value: { ...row, path: relative(this.store.directory, row.path) },
    };
  }
  private videoChange(row: Video): Change {
    return {
      namespace: "videos",
      id: row.fileName,
      value: { ...row, path: relative(this.store.directory, row.path) },
    };
  }
  private importChange(id: string, patch: Partial<ImportFile>): Change {
    return { namespace: "imports", id, value: { ...this.file(id), ...patch } };
  }
  /** 提交异常只按权威记录核实结果；不能在这里推断物理文件失败。 */
  private commit(changes: Change[], expected?: Identity): boolean {
    if (expected && this.current(expected.fileName)?.id !== expected.id)
      return false;
    const before = changes.map((c) => this.store.get(c.namespace, c.id));
    let attempted = false;
    try {
      return this.store.transaction(() => {
        if (expected && this.current(expected.fileName)?.id !== expected.id)
          return false;
        attempted = true;
        for (const c of changes) this.store.set(c.namespace, c.id, c.value);
        return true;
      });
    } catch (error) {
      let saved: unknown[];
      try {
        saved = changes.map((c) => this.store.get(c.namespace, c.id));
      } catch (readError) {
        this.workerError = `提交结果无法确认：${errorMessage(error)}；核实失败：${errorMessage(readError)}`;
        throw new DataError(this.workerError);
      }
      // Store 使用 JSON 持久化，比较的也是同一格式的完整结果。
      if (
        attempted &&
        saved.every((v, i) =>
          isDeepStrictEqual(v, JSON.parse(JSON.stringify(changes[i].value))),
        )
      )
        return true;
      const unchanged = saved.every((v, i) => isDeepStrictEqual(v, before[i]));
      this.workerError = unchanged
        ? `本次操作未提交：${errorMessage(error)}`
        : `提交结果无法确认，已保存记录不一致：${errorMessage(error)}`;
      throw new DataError(this.workerError);
    }
  }
  private writeCurrent(video: Video, patch: Partial<Video>): boolean {
    const next = { ...video, ...patch };
    return this.commit(
      [
        this.videoChange(next),
        this.importChange(video.importId, {
          status: next.status,
          message: next.message,
        }),
      ],
      video,
    );
  }
  private unavailable(video: Video, error: unknown) {
    return this.writeCurrent(video, {
      status: "unavailable",
      message: errorMessage(error),
    });
  }
  createBatch(
    input: Array<{ fileName: string; size: number; kind: ImportFile["kind"] }>,
  ) {
    if (!Array.isArray(input) || !input.length)
      throw new AppError("invalid_batch", "请选择文件");
    for (const f of input) {
      if (
        !f ||
        typeof f.fileName !== "string" ||
        !f.fileName ||
        /[\x00-\x1f/\\]/.test(f.fileName) ||
        !Number.isSafeInteger(f.size) ||
        f.size < 0 ||
        !["report", "video", "media"].includes(f.kind)
      )
        throw new AppError("invalid_file", "文件名、类型或大小无效");
    }
    return this.store.transaction(() => {
      const id = randomUUID();
      const now = new Date().toISOString();
      const files: ImportFile[] = input.map((f) => ({
        id: randomUUID(),
        batchId: id,
        fileName: f.fileName,
        expectedSize: f.size,
        kind: f.kind,
        status: "uploading",
        bytesReceived: 0,
        createdAt: now,
      }));
      const batch: ImportBatch = {
        id,
        fileIds: files.map((f) => f.id),
        createdAt: now,
        reportsDone: false,
      };
      this.store.set("batches", id, batch);
      for (const f of files) this.store.set("imports", f.id, f);
      return { ...batch, files };
    });
  }
  async upload(id: string, stream: Readable): Promise<ImportFile> {
    const file = this.file(id);
    if (file.status !== "uploading" || this.activeUploads.has(id))
      throw new AppError(
        "upload_state",
        "该上传已开始或结束，请查询实际结果",
        409,
      );
    this.activeUploads.add(id);
    const path = join(this.store.directory, "uploads", `${id}.part`);
    try {
      let size = 0;
      let last = Date.now();
      let storageFailure: unknown;
      try {
        await mkdir(join(this.store.directory, "uploads"), { recursive: true });
        const count = new Transform({
          transform: (chunk, encoding, callback) => {
            size += chunk.length;
            if (size > file.expectedSize) {
              callback(new AppError("upload_size", "上传字节超过所声明大小"));
              return;
            }
            if (Date.now() - last > 250) {
              try {
                this.update(id, { bytesReceived: size });
                last = Date.now();
              } catch (error) {
                storageFailure = error;
                callback(error as Error);
                return;
              }
            }
            callback(null, chunk);
          },
        });
        await pipeline(stream, count, createWriteStream(path, { flags: "wx" }));
        if (size !== file.expectedSize)
          throw new AppError(
            "upload_incomplete",
            `上传未完成：${size}/${file.expectedSize} 字节`,
          );
        const fd = await open(path, "r+");
        try {
          await fd.sync();
        } finally {
          await fd.close();
        }
      } catch (error) {
        if (storageFailure) throw storageFailure;
        if (!this.complete(id))
          this.commit([
            this.importChange(id, {
              status: "interrupted",
              message: errorMessage(error),
            }),
          ]);
        this.enqueueReport(() => this.processBatch(file.batchId));
        throw error;
      }
      this.commit([
        this.completeChange({
          id,
          path,
          size,
          fileName: file.fileName,
          importId: id,
          processed: false,
        }),
        this.importChange(id, { status: "received", bytesReceived: size }),
      ]);
      if (file.kind !== "report") this.enqueue(() => this.processVideo(id));
      else this.enqueueReport(() => this.processBatch(file.batchId));
    } finally {
      this.activeUploads.delete(id);
    }
    return this.file(id);
  }
  async failUpload(id: string, message: string) {
    if (this.activeUploads.has(id))
      throw new AppError("upload_active", "文件仍在上传，请等待实际结果", 409);
    const file = this.file(id);
    if (!this.complete(id) && file.status === "uploading") {
      this.commit([this.importChange(id, { status: "interrupted", message })]);
      this.enqueueReport(() => this.processBatch(file.batchId));
    }
    return this.file(id);
  }
  private enqueue(job: () => Promise<void>) {
    this.queue = this.queue.then(job).catch((error) => {
      this.workerError = errorMessage(error);
    });
  }
  private enqueueReport(job: () => Promise<void>) {
    this.reportQueue = this.reportQueue.then(job).catch((error) => {
      this.workerError = errorMessage(error);
    });
  }
  async idle() {
    await this.reportQueue;
    await this.queue;
  }
  private mapping(name: string): Mapping | undefined {
    for (const plan of this.app.snapshot().plans ?? [])
      for (const action of plan.actions ?? [])
        if ("deliveries" in action)
          for (const delivery of action.deliveries ?? []) {
            if (
              delivery.file_name === name &&
              typeof delivery.size === "number" &&
              typeof delivery.sha256 === "string"
            )
              return { size: delivery.size, sha256: delivery.sha256 };
          }
    return undefined;
  }
  private async processBatch(id: string) {
    const batch = this.store.get<ImportBatch>("batches", id);
    if (!batch || batch.reportsDone || this.processingBatches.has(id)) return;
    const files = batch.fileIds
      .map((fid) => this.file(fid))
      .filter((f) => f.kind === "report");
    if (files.some((f) => f.status === "uploading")) return;
    this.processingBatches.add(id);
    try {
      const inputs: Array<{ file: ImportFile; bytes: Uint8Array }> = [];
      for (const f of files.filter((f) => f.status === "received")) {
        const complete = this.complete(f.id);
        if (!complete) throw new DataError("缺少完整文件保存记录");
        try {
          inputs.push({ file: f, bytes: await readFile(complete.path) });
        } catch (error) {
          this.update(f.id, { status: "failed", message: errorMessage(error) });
        }
      }
      this.app.applyReports(inputs);
      this.store.set("batches", id, { ...batch, reportsDone: true });
      for (const f of files) {
        const complete = this.complete(f.id);
        if (complete) {
          this.saveComplete({ ...complete, processed: true });
          await this.removeTemporary(complete.path, f.id);
        }
      }
      for (const video of this.videos())
        if (video.status === "waiting_report" || video.status === "unavailable")
          this.enqueue(() =>
            this.verifyCurrent({ fileName: video.fileName, id: video.id }),
          );
    } finally {
      this.processingBatches.delete(id);
    }
  }
  private async removeTemporary(path: string, id: string) {
    if (this.videos().some((v) => v.path === path)) return;
    const complete = this.complete(id);
    if (!complete || !complete.processed) return;
    try {
      await this.io.remove(path);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        const message = `临时文件整理失败：${errorMessage(error)}`;
        this.workerError = message;
        this.store.set("file_diagnostics", randomUUID(), {
          id,
          path: relative(this.store.directory, path),
          message,
        });
      }
    }
  }
  async processVideo(id: string) {
    const file = this.file(id);
    const complete = this.complete(id);
    if (!complete) throw new DataError("缺少完整媒体文件保存记录");
    if (complete.processed) return;
    let newHash: Mapping;
    try {
      newHash = await this.io.fingerprint(complete.path);
      if (newHash.size !== complete.size)
        throw new AppError("file_changed", "文件大小与完整保存记录不一致");
    } catch (error) {
      this.commit([
        this.importChange(id, {
          status: "unavailable",
          message: errorMessage(error),
        }),
      ]);
      return;
    }
    const mapping = this.mapping(file.fileName);
    const previous = this.current(file.fileName);
    const expected: Identity = { fileName: file.fileName, id: previous?.id };
    if (previous) {
      let oldHash: Mapping;
      try {
        oldHash = await this.io.fingerprint(previous.path);
        if (
          oldHash.size !== previous.size ||
          (previous.sha256 !== undefined && oldHash.sha256 !== previous.sha256)
        )
          throw new AppError(
            "file_changed",
            "已有副本的实际内容与保存记录矛盾，无法决定替换",
          );
        if (
          previous.verification &&
          same(oldHash, previous.verification.against) !==
            (previous.verification.status === "verified")
        )
          throw new AppError(
            "file_changed",
            "已有副本的核验结论与实际内容矛盾",
          );
        if (
          previous.verifiedAgainst &&
          !same(oldHash, previous.verifiedAgainst)
        )
          throw new AppError("file_changed", "已有成功核验依据与实际内容矛盾");
      } catch (error) {
        this.unavailable(previous, error);
        this.commit(
          [
            this.importChange(id, {
              status: "unavailable",
              message: errorMessage(error),
            }),
          ],
          expected,
        );
        return;
      }
      if (same(oldHash, newHash)) {
        if (
          this.commit(
            [
              this.importChange(id, {
                status: "duplicate",
                videoId: previous.id,
                message: "相同内容已导入，复用已有副本",
              }),
              this.completeChange({ ...complete, processed: true }),
            ],
            expected,
          )
        ) {
          await this.removeTemporary(complete.path, id);
          await this.verifyCurrent(expected);
        }
        return;
      }
      if (!mapping || !same(newHash, mapping) || same(oldHash, mapping)) {
        if (
          this.commit(
            [
              this.importChange(id, {
                status: "conflict",
                message: !mapping
                  ? "同名文件内容冲突，等待报告核对"
                  : !same(newHash, mapping)
                    ? "新副本不符合报告，已有副本保留"
                    : "已有副本符合报告，不可覆盖",
              }),
              this.completeChange({ ...complete, processed: true }),
            ],
            expected,
          )
        )
          await this.removeTemporary(complete.path, id);
        return;
      }
    }
    const matched = mapping ? same(newHash, mapping) : false;
    const video: Video = {
      id,
      fileName: file.fileName,
      path: complete.path,
      size: newHash.size,
      sha256: newHash.sha256,
      status: mapping ? (matched ? "verified" : "mismatch") : "waiting_report",
      importId: id,
      ...(mapping
        ? {
            verification: {
              status: matched ? ("verified" as const) : ("mismatch" as const),
              against: mapping,
            },
          }
        : {}),
      ...(matched ? { verifiedAgainst: mapping } : {}),
      ...(previous ? { previousId: previous.id } : {}),
    };
    if (
      this.commit(
        [
          this.videoChange(video),
          this.completeChange({ ...complete, processed: true }),
          this.importChange(id, {
            status: video.status,
            videoId: id,
            message: matched
              ? "大小与 SHA-256 均匹配"
              : mapping
                ? "文件内容与报告不符"
                : "文件已保存，等待对应报告",
          }),
        ],
        expected,
      ) &&
      previous
    )
      await this.removeTemporary(previous.path, previous.importId);
  }
  private async verifyCurrent(expected: Identity) {
    const video = this.current(expected.fileName);
    if (!video || video.id !== expected.id) return;
    try {
      await this.io.readable(video.path, video.size);
    } catch (error) {
      this.unavailable(video, error);
      return;
    }
    if (this.current(expected.fileName)?.id !== expected.id) return;
    const mapping = this.mapping(video.fileName);
    const verification = video.verification;
    if (verification && mapping && same(verification.against, mapping)) {
      if (video.status !== verification.status)
        this.writeCurrent(video, {
          status: verification.status,
          message: undefined,
        });
      return;
    }
    if (video.status === "verified" || video.status === "mismatch") return;
    if (!mapping) {
      this.writeCurrent(video, {
        status: "waiting_report",
        message: undefined,
      });
      return;
    }
    if (!this.writeCurrent(video, { status: "verifying" })) return;
    let hash: Mapping;
    try {
      hash = await this.io.fingerprint(video.path);
      if (
        hash.size !== video.size ||
        (video.sha256 !== undefined && hash.sha256 !== video.sha256)
      )
        throw new AppError(
          "file_changed",
          "当前文件实际内容与完整保存记录矛盾",
        );
    } catch (error) {
      this.unavailable(video, error);
      return;
    }
    const matched = same(hash, mapping);
    this.writeCurrent(video, {
      sha256: hash.sha256,
      status: matched ? "verified" : "mismatch",
      message: matched ? undefined : "大小或摘要与报告不符",
      verification: {
        status: matched ? "verified" : "mismatch",
        against: mapping,
      },
      ...(matched ? { verifiedAgainst: mapping } : {}),
    });
  }
  videos(): Video[] {
    return this.store
      .all<Video>("videos")
      .map((v) => ({ ...v, path: join(this.store.directory, v.path) }));
  }
  private requirePlayable(video: Video): void {
    if (video.status !== "verified" || !video.verifiedAgainst)
      throw new AppError(
        "video_not_verified",
        "媒体文件尚未满足播放和下载条件",
        409,
      );
    const mapping = this.mapping(video.fileName);
    if (!mapping || !same(mapping, video.verifiedAgainst))
      throw new AppError(
        "video_mapping",
        "当前报告映射不满足已保存核验结果",
        409,
      );
  }
  async openVideo(id: string): Promise<Video> {
    const video = this.videos().find((v) => v.id === id);
    if (!video) throw new AppError("not_found", "媒体文件不存在", 404);
    this.requirePlayable(video);
    try {
      await this.io.readable(video.path, video.size);
    } catch (error) {
      this.unavailable(video, error);
      throw new AppError(
        "video_unavailable",
        "已保存的媒体文件当前不可读取",
        409,
      );
    }
    const current = this.current(video.fileName);
    if (current?.id !== id)
      throw new AppError("not_found", "当前媒体文件副本已变更", 404);
    this.requirePlayable(current);
    return current;
  }
  async recover() {
    if (this.store.status().state !== "ready") return;
    for (const file of this.store.all<ImportFile>("imports")) {
      const complete = this.complete(file.id);
      if (
        file.kind === "report" &&
        ["uploading", "received", "processing"].includes(file.status)
      ) {
        this.update(file.id, {
          status: "interrupted",
          message: "后端处理中断，请重新选择原批报告",
        });
        continue;
      }
      if (file.kind !== "report" && complete && !complete.processed)
        this.enqueue(() => this.processVideo(file.id));
      else if (file.status === "uploading")
        this.update(file.id, {
          status: "interrupted",
          message: "上传未完成，请重新选择文件从头上传",
        });
    }
    for (const v of this.videos())
      this.enqueue(() =>
        this.verifyCurrent({ fileName: v.fileName, id: v.id }),
      );
  }
}
