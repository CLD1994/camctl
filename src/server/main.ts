import { resolve } from "node:path";
import { Application } from "./application";
import { Files } from "./files";
import { createHttpApp } from "./http";
import { createStop, RequestLifecycle } from "./lifecycle";

const directory = resolve(process.env.CAMCTL_DATA_DIR ?? "data");
const port = Number(process.env.PORT ?? 4310);
if (!Number.isInteger(port) || port < 1 || port > 65535)
  throw new Error("PORT 必须是 1～65535 的整数");
const application = new Application(directory);
const files = new Files(application);
await files.recover();
const host = process.env.HOST ?? "127.0.0.1";
const requests = new RequestLifecycle();
const server = createHttpApp(application, files, requests).listen(
  port,
  host,
  () => {
    console.log(`camctl 客户端：http://localhost:${port}`);
    console.log(`持久化目录：${directory}`);
  },
);
const stop = createStop(
  () =>
    new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    ),
  requests,
  () => files.idle(),
  () => application.store.close(),
);
function onStop() {
  void stop().then(
    () => console.log("客户端服务已停止，可备份完整数据目录。"),
    (error) => {
      console.error("无法确认停机完成：", error);
      process.exitCode = 1;
    },
  );
}
process.on("SIGINT", onStop);
process.on("SIGTERM", onStop);
