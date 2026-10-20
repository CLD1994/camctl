import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

const directory = fileURLToPath(new URL("../..", import.meta.url));
it.each([
  [
    "shared/scalar-schema",
    'import {isLocalScalarSchema} from "./src/shared/scalar-schema.ts"; console.log(isLocalScalarSchema({type:"string"}));',
  ],
  [
    "shared/schema-nodes",
    'import {visitSchemaNodes} from "./src/shared/schema-nodes.ts"; const nodes=[]; visitSchemaNodes({type:"string"},node=>nodes.push(node.type)); console.log(nodes.length===1 && nodes[0]==="string");',
  ],
  [
    "web/scalar-schema",
    'import {isLocalScalarSchema} from "./src/web/scalar-schema.ts"; console.log(isLocalScalarSchema({type:"string"}));',
  ],
  [
    "shared/validation",
    'import {createValidator} from "./src/shared/validation.ts"; console.log(createValidator().compile({type:"string"})("value"));',
  ],
  [
    "shared/capabilities",
    'import {loadCapabilities} from "./src/shared/capabilities.ts"; const value={devices:[{device_id:"camera",driver_id:"test",actions:[{type:"camera_take_photo",parameter_types:[{type:"entry",name:"入口",description:"独立导入验证",preview_supported:false,schema:{$schema:"https://json-schema.org/draft/2020-12/schema",type:"object",additionalProperties:false,properties:{type:{const:"entry"}},required:["type"]}}]}]}]}; console.log(loadCapabilities(value).devices.length===1);',
  ],
])("真实 Node 首次导入 %s 不依赖其他模块预热", (_entry, program) => {
  const result = spawnSync(
    process.execPath,
    ["--import", "tsx", "--input-type=module", "-e", program],
    { cwd: directory, encoding: "utf8", timeout: 10000 },
  );
  expect(result.error).toBeUndefined();
  expect(result.status, result.stderr).toBe(0);
  expect(result.stdout.trim()).toBe("true");
});
