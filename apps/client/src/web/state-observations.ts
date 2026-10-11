import type { EstimateReloadPhase } from "./video-estimate-state";
export type StateRead<Value> =
  { kind: "accepted"; value: Value } | { kind: "superseded" };
/** 读取已失去资格，不能用旧响应降级较新的来源事实。 */
export class SupersededStateReadError extends Error {
  constructor(cause?: unknown) {
    super(`${cause instanceof Error ? cause.message + "；" : ""}本次状态读取已被较新观察或能力重载取代，请重新核实。`, { cause });
  }
}
export class StateObservations<Value, Context> {
  private stage: "idle" | "preparing" | "posting" | "awaiting" = "idle";
  private unconfirmed = false;
  private requestEnded = false;
  private epoch = 0;
  private sequence = 0;
  private acceptedSequence = 0;
  private inflight = new Set<Promise<StateRead<Value>>>();
  constructor(
    private request: () => Promise<Value>,
    private capture: () => Context,
    private accept: (value: Value, context: Context) => void,
    private changed: (phase: EstimateReloadPhase) => void,
    private failed?: (error: unknown, context: Context) => void,
  ) {}
  get phase(): EstimateReloadPhase {
    return this.stage !== "idle"
      ? "updating"
      : this.unconfirmed
        ? "unconfirmed"
        : "idle";
  }
  beginReload() {
    if (this.stage !== "idle") throw Error("能力重载尝试尚未结束");
    this.epoch++;
    this.stage = "preparing";
    this.changed(this.phase);
  }
  postStarted() {
    if (this.stage !== "preparing") throw Error("能力重载尚未完成准备");
    this.epoch++;
    this.stage = "posting";
    this.unconfirmed = true;
    this.requestEnded = false;
    this.changed(this.phase);
  }
  postEnded() {
    if (this.stage !== "posting") throw Error("没有正在执行的能力重载请求");
    this.epoch++;
    this.requestEnded = true;
    this.stage = "awaiting";
    this.changed(this.phase);
  }
  preparationFailed() {
    if (this.stage !== "preparing") throw Error("能力重载不在准备阶段");
    this.stage = "idle";
    this.changed(this.phase);
  }
  reloadFailed() {
    this.stage = "idle";
    this.changed(this.phase);
  }
  read(context: Context = this.capture()): Promise<StateRead<Value>> {
    const epoch = this.epoch,
      sequence = ++this.sequence;
    const eligible =
      this.requestEnded &&
      this.unconfirmed &&
      (this.stage === "idle" || this.stage === "awaiting");
    const request = this.request().then(
      (value): StateRead<Value> => {
        if (epoch !== this.epoch || sequence <= this.acceptedSequence)
          return { kind: "superseded" };
        this.accept(value, context);
        this.acceptedSequence = sequence;
        if (
          epoch === this.epoch &&
          eligible &&
          (this.stage === "idle" || this.stage === "awaiting")
        ) {
          this.unconfirmed = false;
          this.stage = "idle";
          this.changed(this.phase);
        }
        return { kind: "accepted", value };
      },
      (error: unknown) => {
        if (epoch !== this.epoch || sequence <= this.acceptedSequence)
          throw new SupersededStateReadError(error);
        this.failed?.(error, context);
        throw error;
      },
    );
    this.inflight.add(request);
    void request.then(
      () => this.inflight.delete(request),
      () => this.inflight.delete(request),
    );
    return request;
  }
  async settle(): Promise<void> {
    let failure: PromiseRejectedResult | undefined;
    while (this.inflight.size) {
      const results = await Promise.allSettled([...this.inflight]);
      failure ??= results.find(
        (result): result is PromiseRejectedResult =>
          result.status === "rejected",
      );
    }
    if (failure) throw failure.reason;
  }
}
