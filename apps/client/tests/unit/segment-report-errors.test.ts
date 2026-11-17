import { expect, it, vi } from "vitest";
import { segmentReport, scopeReport } from "../helpers/segment-reports";

it("分段错误按类型准备校验并逐条检查详情", async () => {
  vi.resetModules();
  const protocol = await import("../../src/shared/protocol-validation");
  const createValidator = protocol.createProtocolValidator;
  const compileCalls: unknown[] = [];
  const create = vi
    .spyOn(protocol, "createProtocolValidator")
    .mockImplementation(() => {
      const validator = createValidator();
      const compile = validator.compile.bind(validator);
      vi.spyOn(validator, "compile").mockImplementation((schema) => {
        compileCalls.push(schema);
        return compile(schema);
      });
      return validator;
    });
  try {
    const { segmentIssue } =
      await import("../../src/shared/segment-report-errors");
    expect(segmentIssue(null)).toBeUndefined();
    expect(
      segmentIssue({ code: "future", stage: "execution", details: {} }),
    ).toBeUndefined();
    expect(create).not.toHaveBeenCalled();

    const capture = segmentReport().plans![0].actions![0].error!;
    expect(segmentIssue(capture)?.code).toBe("capture_segment_gap");
    expect(create).toHaveBeenCalledTimes(1);
    expect(compileCalls).toHaveLength(1);
    expect(() => segmentIssue({ ...capture, details: {} })).toThrow();
    expect(compileCalls).toHaveLength(1);

    for (const state of ["empty", "unknown"] as const) {
      const result = scopeReport(state).plans![0].actions![1].result as {
        failures: Array<{ error: unknown }>;
      };
      expect(segmentIssue(result.failures[0].error)).toBeDefined();
      expect(segmentIssue(result.failures[0].error)).toBeDefined();
    }
    expect(create).toHaveBeenCalledTimes(1);
    expect(compileCalls).toHaveLength(3);
  } finally {
    create.mockRestore();
  }
});
