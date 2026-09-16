import type { Capabilities, ParameterType } from "../shared/types";
import { isObject } from "../shared/validation";
import { resolveField } from "./editing";
import { optionLabel, parameterOptions } from "./parameter-options";

export function DeviceGuide({
  capabilities,
}: {
  capabilities: Capabilities | null;
}) {
  return (
    <div className="device-guide" data-testid="device-guide">
      <header className="guide-hero">
        <p className="eyebrow">拍摄前，先了解设备</p>
        <h2>选择适合这次拍摄的任务</h2>
        <p>
          查看设备提供的录像任务、可调整的画质与搭配限制，然后到计划中安排拍摄。
        </p>
        <ol className="guide-steps">
          <li>
            <span>1</span>
            <div>
              <strong>选设备</strong>
              <small>确认本次使用哪台设备</small>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>选任务与参数</strong>
              <small>了解固定设置和允许组合</small>
            </div>
          </li>
          <li>
            <span>3</span>
            <div>
              <strong>在计划中安排执行</strong>
              <small>填写时间，检查并导出</small>
            </div>
          </li>
        </ol>
        <p className="guide-note">
          这里介绍设备允许使用的功能。设备是否在线、任务是否完成，请以计划记录中收到的报告为准。
        </p>
      </header>
      {!capabilities ? (
        <p className="empty">
          尚无可用的设备说明，暂不能提供拍摄指南。你仍可准备状态报告、导入文件和查看已有结果。
        </p>
      ) : !capabilities.devices.length ? (
        <p className="empty">
          尚未配置设备。配置并加载设备说明后，可在这里查看拍摄任务。
        </p>
      ) : (
        capabilities.devices.map((device) => (
          <section key={device.device_id} className="guide-device">
            <div className="guide-device-heading">
              <div>
                <p className="eyebrow">可用设备</p>
                <h2>{device.device_id}</h2>
              </div>
              <span className="guide-tag">
                {device.actions.reduce(
                  (count, action) => count + action.parameter_types.length,
                  0,
                )}{" "}
                种拍摄任务
              </span>
            </div>
            {!device.actions.length && (
              <p className="empty">该设备尚未提供可用的拍摄任务。</p>
            )}
            <div className="guide-task-grid">
              {device.actions.flatMap((action) =>
                action.parameter_types.map((parameter) => (
                  <TaskGuide
                    key={`${action.type}/${parameter.type}`}
                    parameter={parameter}
                  />
                )),
              )}
            </div>
            <details className="guide-technical">
              <summary>设备技术详情</summary>
              <p>
                设备标识：<code>{device.device_id}</code> · 驱动：
                <code>{device.driver_id}</code>
              </p>
            </details>
          </section>
        ))
      )}
    </div>
  );
}

function TaskGuide({ parameter }: { parameter: ParameterType }) {
  const catalog = parameterOptions(parameter);
  const fields = Object.entries(
    isObject(parameter.schema.properties) ? parameter.schema.properties : {},
  )
    .filter(([name]) => name !== "type")
    .map(([name, schema]) => ({
      name,
      schema: resolveField(schema, parameter.schema),
    }));
  const fixed =
    catalog.kind === "finite" && catalog.rows.length > 0 && fields.length === 0;
  return (
    <article className="guide-task">
      <div className="guide-task-title">
        <span className="guide-tag">{fixed ? "固定设置" : "参数设置"}</span>
        <h3>{parameter.name}</h3>
      </div>
      <p className="guide-description">{parameter.description}</p>
      {fixed ? (
        <div className="guide-fixed">
          <strong>选好任务，即可安排拍摄</strong>
          <p>此任务使用固定设置，无需调整参数。</p>
        </div>
      ) : (
        <>
          <h4>拍摄前需要了解</h4>
          <dl className="guide-fields">
            {fields.map(({ name, schema }) => (
              <div key={name}>
                <dt>
                  {typeof schema.title === "string" ? schema.title : name}
                  <small>
                    {Array.isArray(parameter.schema.required) &&
                    parameter.schema.required.includes(name)
                      ? "必填"
                      : "可选"}
                  </small>
                </dt>
                <dd>
                  {typeof schema.description === "string"
                    ? schema.description
                    : "按任务规则填写。"}
                  {schema.minimum !== undefined && (
                    <span> 最小值：{optionLabel(schema.minimum)}。</span>
                  )}
                  {schema.maximum !== undefined && (
                    <span> 最大值：{optionLabel(schema.maximum)}。</span>
                  )}
                  {schema.default !== undefined && (
                    <span>
                      {" "}
                      未填写时的默认值说明：{optionLabel(schema.default)}。
                    </span>
                  )}
                </dd>
              </div>
            ))}
          </dl>
          {catalog.kind === "finite" ? (
            catalog.rows.length === 0 ? (
              <p className="notice warning">
                当前规则没有允许的参数组合，请联系提供设备说明的人员。
              </p>
            ) : catalog.rows.length > 24 ? (
              <p className="notice">
                共有 {catalog.rows.length}{" "}
                种允许组合。请在计划表单中选择，选项会随已选参数自动缩小范围。
              </p>
            ) : (
              <div className="guide-combinations">
                <h4>允许的参数组合</h4>
                <div className="table-scroll">
                  <table>
                    <caption className="sr-only">
                      {parameter.name}允许的参数组合
                    </caption>
                    <thead>
                      <tr>
                        {fields.map(({ name, schema }) => (
                          <th key={name} scope="col">
                            {typeof schema.title === "string"
                              ? schema.title
                              : name}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {catalog.rows.map((row, i) => (
                        <tr key={i}>
                          {fields.map(({ name }) => (
                            <td key={name}>
                              {Object.hasOwn(row, name)
                                ? optionLabel(row[name])
                                : "不填写"}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="muted">
                  在计划表单中选择时，只会出现与已选参数兼容的选项。
                </p>
              </div>
            )
          ) : (
            <p className="notice">
              此任务包含复杂参数，请在计划的参数 JSON
              中设置并检查。具体要求见上方任务说明。
            </p>
          )}
        </>
      )}
      <details className="guide-technical">
        <summary>任务技术详情</summary>
        <p>
          参数类型：<code>{parameter.type}</code>
        </p>
        <pre>{JSON.stringify(parameter.schema, null, 2)}</pre>
      </details>
    </article>
  );
}
