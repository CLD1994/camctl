import planSchema from "../../../../protocol/schemas/plan.schema.json";
import reportSchema from "../../../../protocol/schemas/status-report.schema.json";
import notificationSchema from "../../../../protocol/schemas/host-notification.schema.json";
import capabilitySchema from "../../../../protocol/schemas/capabilities.schema.json";
import { createValidator } from "./validation";

/** 公共 Schema 可相互引用；先登记全部资源，再由消费入口编译。 */
export function createProtocolValidator() {
  const ajv = createValidator();
  ajv.addSchema(planSchema, "plan.schema.json");
  ajv.addSchema(reportSchema, "status-report.schema.json");
  ajv.addSchema(notificationSchema, "host-notification.schema.json");
  ajv.addSchema(capabilitySchema, "capabilities.schema.json");
  return ajv;
}
