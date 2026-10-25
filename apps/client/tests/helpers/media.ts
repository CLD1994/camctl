import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir, readdir } from "node:fs/promises";
import { join } from "node:path";

/** 将固定可播放视频与匹配的真实报告装配到独立测试目录。 */
export async function makeMediaFixture(directory: string) {
  await mkdir(directory, { recursive: true });
  const video = await readFile(
    new URL("../fixtures/media/local-verification.webm", import.meta.url),
  );
  const checksum = createHash("sha256").update(video).digest("hex");
  const reports = "../../protocol/examples/client-protocol/01-success";
  const reportFile = (await readdir(reports)).find((name) =>
    /^status-report-1-[0-9a-f]{64}\.json$/.test(name),
  )!;
  const report = JSON.parse(await readFile(join(reports, reportFile), "utf8"));
  const output = report.plans[0].actions[0].outputs[0];
  output.size = video.length;
  output.checksum.sha256 = checksum;
  output.original_name = "本地验证.webm";
  output.media_type = "video/webm";
  const delivery = report.plans[0].actions[1].deliveries[0];
  delivery.size = video.length;
  delivery.sha256 = checksum;
  delivery.file_name = delivery.delivery_id + ".webm";
  delivery.display_name = "本地验证-正常采集-主录像.webm";
  const bytes = Buffer.from(JSON.stringify(report, null, 2) + "\n");
  const reportName = `status-report-1-${createHash("sha256").update(bytes).digest("hex")}.json`;
  const videoPath = join(directory, delivery.file_name);
  const reportPath = join(directory, reportName);
  await writeFile(videoPath, video);
  await writeFile(reportPath, bytes);
  return {
    videoPath,
    reportPath,
    videoName: delivery.file_name as string,
    reportName,
    video,
    reportBytes: bytes,
  };
}
