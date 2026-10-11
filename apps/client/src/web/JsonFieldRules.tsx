import { displayJsonValue, stringifyJson } from "../shared/json";
import { isObject } from "../shared/validation";

const otherStructures = [
  "$ref",
  "$dynamicRef",
  "allOf",
  "anyOf",
  "not",
  "if",
  "then",
  "else",
];
const hasOtherStructure = (schema: Record<string, unknown>) =>
  otherStructures.some((key) => Object.hasOwn(schema, key));

function directObject(schema: unknown): schema is Record<string, unknown> {
  return (
    isObject(schema) &&
    schema.type === "object" &&
    !hasOtherStructure(schema) &&
    !Object.hasOwn(schema, "oneOf") &&
    (schema.properties === undefined || isObject(schema.properties))
  );
}

function ObjectRules({ schema }: { schema: Record<string, unknown> }) {
  const properties = isObject(schema.properties) ? schema.properties : {};
  const required = Array.isArray(schema.required) ? schema.required : [];
  return Object.keys(properties).length ? (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">字段标识</th>
            <th scope="col">是否必填</th>
            <th scope="col">直接声明的约束</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(properties).map(([name, definition]) => {
            const field = isObject(definition) ? definition : {};
            return (
              <tr key={name}>
                <th scope="row">
                  <code>{name}</code>
                </th>
                <td>
                  {required.includes(name) ? (
                    <span className="required">必填</span>
                  ) : (
                    "可选"
                  )}
                </td>
                <td>
                  {Object.hasOwn(field, "type") && (
                    <p>
                      类型：
                      <code>{displayJsonValue(field, "type", field.type)}</code>
                    </p>
                  )}
                  {Object.hasOwn(field, "const") && (
                    <p>
                      固定值（const）：
                      <code>
                        {displayJsonValue(field, "const", field.const)}
                      </code>
                    </p>
                  )}
                  {Object.hasOwn(field, "enum") && (
                    <p>
                      允许值（enum）：
                      <code>{displayJsonValue(field, "enum", field.enum)}</code>
                    </p>
                  )}
                  {Object.hasOwn(field, "minimum") && (
                    <p>
                      最小值：
                      <code>
                        {displayJsonValue(field, "minimum", field.minimum)}
                      </code>
                    </p>
                  )}
                  {Object.hasOwn(field, "maximum") && (
                    <p>
                      最大值：
                      <code>
                        {displayJsonValue(field, "maximum", field.maximum)}
                      </code>
                    </p>
                  )}
                  {Object.hasOwn(field, "exclusiveMinimum") && (
                    <p>
                      必须大于：
                      <code>
                        {displayJsonValue(
                          field,
                          "exclusiveMinimum",
                          field.exclusiveMinimum,
                        )}
                      </code>
                    </p>
                  )}
                  {Object.hasOwn(field, "exclusiveMaximum") && (
                    <p>
                      必须小于：
                      <code>
                        {displayJsonValue(
                          field,
                          "exclusiveMaximum",
                          field.exclusiveMaximum,
                        )}
                      </code>
                    </p>
                  )}
                  <small>其他约束见完整字段规则。</small>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  ) : (
    <p>该对象没有直接列出子字段。</p>
  );
}

export function JsonFieldRules({
  schema,
  showEditingHint = true,
}: {
  schema: unknown;
  showEditingHint?: boolean;
}) {
  const branches =
    isObject(schema) &&
    !hasOtherStructure(schema) &&
    Array.isArray(schema.oneOf) &&
    schema.oneOf.length > 0 &&
    schema.oneOf.every(directObject)
      ? schema.oneOf
      : undefined;
  const object = directObject(schema);
  return (
    <div className="json-field-rules">
      {showEditingHint && <p>请填写一个完整的 JSON 值。</p>}
      {(showEditingHint || branches || object) && (
        <p>
          下方仅说明规则直接声明的字段事实；所有输入仍须满足完整字段规则中的其他约束。
        </p>
      )}
      {branches ? (
        <>
          <p>
            <code>oneOf</code> 要求输入恰好满足下列一个分支。
          </p>
          {branches.map((branch, index) => (
            <section key={index} className="json-rule-branch">
              <h5>分支 {index + 1}</h5>
              <ObjectRules schema={branch} />
            </section>
          ))}
        </>
      ) : object ? (
        <ObjectRules schema={schema} />
      ) : showEditingHint ? (
        <p>该字段未直接声明可逐项说明的对象结构，请展开完整字段规则查看。</p>
      ) : null}
      <details>
        <summary>完整字段规则</summary>
        <pre>{stringifyJson(schema, 2)}</pre>
      </details>
    </div>
  );
}
