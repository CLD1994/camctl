import { Fragment } from "react";
import type { Output } from "../shared/types";
import { FactRow, Facts, label } from "./common";

export function OutputFacts({ output }: { output: Output }) {
  return (
    <dl className="facts output-facts">
      {Object.entries(output).map(([field, value]) => {
        if (field === "checksum")
          return (
            <FactRow key={field} field={field} title="源文件 SHA-256">
              {output.checksum.status === "available" ? (
                <code>{output.checksum.sha256}</code>
              ) : (
                label(output.checksum.status)
              )}
            </FactRow>
          );
        if (field === "cleanup")
          return (
            <Fragment key={field}>
              <FactRow field={field} title="清理状态">
                {output.cleanup.status === "completed"
                  ? "已完成"
                  : label(output.cleanup.status)}
              </FactRow>
              {output.cleanup.error && (
                <FactRow field="error" title="清理错误">
                  <Facts value={output.cleanup.error} />
                </FactRow>
              )}
            </Fragment>
          );
        if (field === "media") {
          const media = output.media;
          return (
            <Fragment key={field}>
              <FactRow field={field} title="主机媒体检查">
                {media.check_status === "completed"
                  ? "已完成"
                  : label(media.check_status)}
              </FactRow>
              <FactRow field="duration" title="媒体时长">
                {media.duration.status === "available" ? (
                  <>
                    <Facts
                      value={media.duration.seconds}
                      parent={media.duration}
                      field="seconds"
                    />{" "}
                    秒
                  </>
                ) : (
                  label(media.duration.status)
                )}
              </FactRow>
              {media.issues && (
                <FactRow field="issues" title="发现的媒体问题">
                  <Facts value={media.issues} />
                </FactRow>
              )}
              {media.error && (
                <FactRow field="error" title="媒体检查错误">
                  <Facts value={media.error} />
                </FactRow>
              )}
            </Fragment>
          );
        }
        return (
          <FactRow key={field} field={field}>
            {field === "availability" || field === "kind" ? (
              label(value)
            ) : (
              <Facts value={value} parent={output} field={field} />
            )}
          </FactRow>
        );
      })}
    </dl>
  );
}
