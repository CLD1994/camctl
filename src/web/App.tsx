import { useEffect, useRef, useState, useReducer } from "react";
import type {
  Draft,
  Preset,
  ExportedRequest,
  ImportFile,
} from "../server/models";
import {
  api,
  readDraft,
  download,
  upload,
  HttpError,
  type ClientState,
} from "./api";
import { DraftSession, sameContent } from "./session";
import { FollowOperation } from "./followup";
import { recordsFor, parseDraft, utcToLocal } from "./editing";
import { Editor } from "./Editor";
import { DeviceGuide } from "./DeviceGuide";
import { useFeedback } from "./feedback";
import { RecordDetail, type Followup } from "./Records";
import { Badge, Empty, ErrorBox, Facts } from "./common";

type Page = "plans" | "import" | "devices";
type PendingFollow = { operation: FollowOperation; follow: Followup };
export function App() {
  const [state, setState] = useState<ClientState>(),
    [connection, setConnection] = useState(""),
    [page, setPage] = useState<Page>("plans"),
    [tab, setTab] = useState<"drafts" | "records">("drafts"),
    [selected, setSelected] = useState(""),
    [recordId, setRecordId] = useState(""),
    [busy, setBusy] = useState(false),
    [follow, setFollow] = useState<Followup>(),
    [activeFollow, setActiveFollow] = useState<PendingFollow>(),
    [followOperations, setFollowOperations] = useState<PendingFollow[]>([]);
  const [error, setError] = useFeedback(page);
  const [notice, setNotice] = useFeedback(page, 3000);
  const startFollow = (input: Followup) => {
    setActiveFollow(undefined);
    setFollow(input);
  };
  const [, render] = useReducer((n) => n + 1, 0),
    sessions = useRef(new Map<string, DraftSession>()),
    deletedDrafts = useRef(new Set<string>()),
    mounted = useRef(true),
    refreshing = useRef<Promise<ClientState> | null>(null);
  const [progress, setProgress] = useState<Record<string, number>>({}),
    [uploadErrors, setUploadErrors] = useState<Record<string, string>>({});
  const refresh = async () => {
    if (refreshing.current) return refreshing.current;
    const exportTokens = new Map(
      [...sessions.current].map(([id, session]) => [
        id,
        session.exportState === "unknown" ? session.exportToken : undefined,
      ]),
    );
    const request = api<ClientState>("/state")
      .then((value) => {
        if (value.drafts)
          value = {
            ...value,
            drafts: value.drafts.filter(
              (d) => !deletedDrafts.current.has(d.id),
            ),
          };
        if (mounted.current) {
          for (const draft of value.drafts ?? [])
            sessions.current
              .get(draft.id)
              ?.observe(draft, exportTokens.get(draft.id));
          setState(value);
          setConnection("");
        }
        return value;
      })
      .catch((e) => {
        if (mounted.current) setConnection((e as Error).message);
        throw e;
      })
      .finally(() => {
        refreshing.current = null;
      });
    refreshing.current = request;
    return request;
  };
  useEffect(() => {
    mounted.current = true;
    void refresh().catch(() => {});
    const timer = setInterval(() => void refresh().catch(() => {}), 900);
    const leave = (e: BeforeUnloadEvent) => {
      if ([...sessions.current.values()].some((s) => !s.saved)) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", leave);
    return () => {
      mounted.current = false;
      clearInterval(timer);
      window.removeEventListener("beforeunload", leave);
    };
  }, []);
  const getSession = (draft: Draft) => {
    let s = sessions.current.get(draft.id);
    if (!s) {
      s = new DraftSession(
        draft,
        {
          save: (id, revision, content) =>
            api<Draft>(`/drafts/${id}`, "PUT", { revision, content }),
          read: readDraft,
        },
        () => {
          if (mounted.current) render();
        },
      );
      sessions.current.set(draft.id, s);
    }
    return s;
  };
  const openDraft = (draft: Draft) => {
    getSession(draft);
    setSelected(draft.id);
    setPage("plans");
    setTab("drafts");
    setError("");
  };
  const run = (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    void fn()
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };
  const create = async () => {
    const draft = await api<Draft>("/drafts", "POST", {});
    openDraft(draft);
    await refresh();
  };
  const openRecord = (id: string) => {
    setRecordId(id);
    setPage("plans");
    setTab("records");
  };
  const current = sessions.current.get(selected);
  const removeDraft = (session: DraftSession, verify = false) => {
    if (
      !verify &&
      !window.confirm(
        `删除草稿“${draftName(session.content.text)}”？删除后无法恢复。`,
      )
    )
      return;
    run(async () => {
      const id = session.draft.id;
      const read = async () => {
        const latest = await api<ClientState>("/state");
        if (latest.startup.state !== "ready" || !Array.isArray(latest.drafts))
          throw new Error("无法可靠读取草稿列表");
        return latest.drafts.find((d) => d.id === id);
      };
      if (verify) await session.checkDeletion(read);
      else
        await session.delete(async (revision) => {
          const result = await api<{ deleted: boolean }>(
            `/drafts/${id}`,
            "DELETE",
            { revision },
          );
          if (result.deleted !== true) throw new Error("删除回执无效");
        }, read);
      deletedDrafts.current.add(id);
      sessions.current.delete(id);
      setSelected((value) => (value === id ? "" : value));
      setState((old) =>
        old?.drafts
          ? { ...old, drafts: old.drafts.filter((d) => d.id !== id) }
          : old,
      );
      setNotice("草稿已删除");
    });
  };
  useEffect(() => {
    if (current?.exportedRequestId) {
      setRecordId(current.exportedRequestId);
      setPage("plans");
      setTab("records");
    }
  }, [current?.exportedRequestId]);
  const exportDraft = () =>
    run(async () => {
      if (!current) return;
      let record: ExportedRequest;
      await current.flush();
      current.beginExport();
      try {
        record = await api<ExportedRequest>(
          `/drafts/${current.draft.id}/export`,
          "POST",
          { revision: current.revision, content: current.content },
        );
      } catch (e) {
        if (e instanceof HttpError && e.status >= 400 && e.status < 500) {
          current.exportFailed();
          throw e;
        }
        current.exportUnknown();
        const actual = await current.checkExport();
        if (!actual.exportedRequestId) throw e;
        const latest = await api<ClientState>("/state");
        const existing = latest.requests?.find(
          (r) => r.id === actual.exportedRequestId,
        );
        if (!existing)
          throw new Error("已导出，但原请求暂时无法读取，请恢复连接后打开记录");
        record = existing;
        setState(latest);
      }
      current.confirmExport(record.id);
      setState((old) =>
        old?.requests
          ? {
              ...old,
              requests: [
                ...(old.requests ?? []).filter((r) => r.id !== record.id),
                record,
              ],
            }
          : old,
      );
      openRecord(record.id);
      await download(
        `/requests/${record.id}/download`,
        `plan-${record.id}.json`,
      );
      await refresh();
    });
  const handoff = (id: string, marked: boolean) =>
    run(async () => {
      try {
        await api(`/requests/${id}/handoff`, "PUT", { marked });
        await refresh();
        setNotice(marked ? "递交标记已保存" : "递交标记已清除");
      } catch (e) {
        const latest = await api<ClientState>("/state");
        setState(latest);
        const actual = latest.requests?.find((r) => r.id === id);
        if (!actual || Boolean(actual.handedAt) !== marked) throw e;
        setNotice("已按保存记录确认递交标记");
      }
    });
  const copy = (id: string) =>
    run(async () => {
      const draft = await api<Draft>(`/requests/${id}/copy`, "POST", {});
      openDraft(draft);
      await refresh();
    });
  const savePreset = async (input: {
    id?: string;
    name: string;
    deviceId: string;
    actionType: string;
    params: unknown;
  }) => {
    try {
      const result = await api<Preset>(
        input.id ? `/presets/${input.id}` : "/presets",
        input.id ? "PUT" : "POST",
        input,
      );
      setState((old) =>
        old?.presets
          ? {
              ...old,
              presets: [
                ...(old.presets ?? []).filter((p) => p.id !== result.id),
                result,
              ],
            }
          : old,
      );
      return result;
    } catch (e) {
      await refresh();
      throw e;
    }
  };
  const addFollow = (destination: string, action: Record<string, unknown>) =>
    run(async () => {
      const pending = activeFollow ?? {
        follow: follow!,
        operation: new FollowOperation(destination, action, {
          create: () => api<Draft>("/drafts", "POST", {}),
          prepare: async (id) => {
            const initial = await readDraft(id),
              session = getSession(initial);
            session.observe(initial);
            await session.flush();
            const actual = await readDraft(id);
            session.observe(actual);
            if (
              !session.editable ||
              actual.revision !== session.revision ||
              !sameContent(actual.content, session.content)
            )
              throw Error("目标草稿与当前保存基线不一致，请先核对目标内容");
            parseDraft(actual.content);
            session.lockAppend();
            return actual;
          },
          append: (id, revision, action) =>
            api<Draft>(`/drafts/${id}/actions`, "POST", { revision, action }),
          read: readDraft,
        }),
      };
      if (!activeFollow) {
        setActiveFollow(pending);
        setFollowOperations((old) => [...old, pending]);
      }
      await pending.operation.advance();
      if (pending.operation.result) {
        const updated = pending.operation.result,
          session = getSession(updated);
        session.unlockAppend();
        session.accept(updated);
        openDraft(updated);
        setFollow(undefined);
        setActiveFollow(undefined);
        setNotice("后续动作已加入草稿，请检查引用并填写执行时间");
      }
      await refresh();
    });
  const importFiles = async (files: File[]) => {
    if (!files.length) return;
    setError("");
    let batch: { id: string; files: ImportFile[] };
    try {
      batch = await api("/batches", "POST", {
        files: files.map((f) => ({
          fileName: f.name,
          size: f.size,
          kind:
            f.name.startsWith("status-report-") || /\.json$/i.test(f.name)
              ? "report"
              : "video",
        })),
      });
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    const jobs = batch.files.map((item, index) => ({
      item,
      file: files[index],
    }));
    const transfer = async ({
      item,
      file,
    }: {
      item: ImportFile;
      file: File;
    }) => {
      try {
        await upload(item.id, file, (bytes) =>
          setProgress((old) => ({ ...old, [item.id]: bytes })),
        );
      } catch (e) {
        setUploadErrors((old) => ({ ...old, [item.id]: (e as Error).message }));
        try {
          const latest = await api<ClientState>("/state");
          const actual = latest.imports?.find((f) => f.id === item.id);
          if (actual?.status === "uploading")
            await api(`/imports/${item.id}/fail`, "POST", {
              message: (e as Error).message,
            });
        } catch (readError) {
          setUploadErrors((old) => ({
            ...old,
            [item.id]: `${(e as Error).message}；保存结果尚未确认：${(readError as Error).message}`,
          }));
        }
      } finally {
        void refresh().catch(() => {});
      }
    };
    // 先启动全部报告上传；视频任务独立推进，批次不会等待大视频才取得报告。
    const ordered = [
      ...jobs.filter((j) => j.item.kind === "report"),
      ...jobs.filter((j) => j.item.kind === "video"),
    ];
    void Promise.allSettled(ordered.map(transfer));
    void refresh().catch(() => {});
  };
  if (!state)
    return (
      <main className="startup">
        <div className="brand">
          camctl<span>本地工作台</span>
        </div>
        <section className="panel">
          <h1>{connection ? "无法连接客户端" : "正在读取客户端数据…"}</h1>
          <ErrorBox error={connection} />
          {connection && (
            <button onClick={() => void refresh().catch(() => {})}>
              重新连接
            </button>
          )}
        </section>
      </main>
    );
  if (state.startup.state !== "ready")
    return (
      <main className="startup">
        <div className="brand">
          camctl<span>本地工作台</span>
        </div>
        <section className="panel">
          <p className="eyebrow">开始使用</p>
          <h1>
            {state.startup.state === "uninitialized"
              ? "创建客户端数据"
              : "客户端数据无法使用"}
          </h1>
          <p>使用当前个人电脑上的数据目录保存草稿、原请求、报告和视频。</p>
          <p className="directory">{state.startup.directory}</p>
          <ErrorBox error={state.startup.message ?? error ?? connection} />
          {state.startup.state === "uninitialized" ? (
            <button
              className="primary"
              data-testid="initialize-button"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await api("/initialize", "POST", {});
                  await refresh();
                })
              }
            >
              创建客户端数据
            </button>
          ) : (
            <button onClick={() => void refresh().catch(() => {})}>
              重新检查
            </button>
          )}
          <ErrorBox error={error || connection} />
        </section>
      </main>
    );
  const drafts = (state.drafts ?? []).filter(
      (d) =>
        !d.exportedRequestId && !sessions.current.get(d.id)?.exportedRequestId,
    ),
    records = recordsFor(state.requests ?? [], state.snapshot?.plans ?? []),
    record = records.find((r) => r.id === recordId);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          camctl<span>本地工作台</span>
        </div>
        <nav aria-label="主要入口">
          <button
            data-testid="nav-plans"
            aria-current={page === "plans" ? "page" : undefined}
            onClick={() => setPage("plans")}
          >
            <span>▤</span>计划
          </button>
          <button
            data-testid="nav-import"
            aria-current={page === "import" ? "page" : undefined}
            onClick={() => setPage("import")}
          >
            <span>↥</span>文件导入
          </button>
          <button
            data-testid="nav-devices"
            aria-current={page === "devices" ? "page" : undefined}
            onClick={() => setPage("devices")}
          >
            <span>◉</span>设备说明
          </button>
        </nav>
        <div className="sidebar-foot">
          <span className="live-dot" />
          个人电脑上的资料<span>计划通过文件手工交接</span>
        </div>
      </aside>
      <main className="workspace">
        {page === "plans" &&
          followOperations
            .filter((item) => item.operation.phase !== "done")
            .map((item, index) => (
              <section className="notice" key={index}>
                <strong>{item.follow.summary}</strong>
                <p>{item.operation.error || "后续操作尚未结束"}</p>
                {item.operation.targetId && (
                  <code>目标草稿：{item.operation.targetId}</code>
                )}
                <div className="button-row">
                  <button
                    disabled={busy}
                    onClick={() => {
                      setActiveFollow(item);
                      setFollow(item.follow);
                    }}
                  >
                    返回核实后续操作
                  </button>
                </div>
              </section>
            ))}
        <header className="page-header">
          <div>
            <p className="eyebrow">CAMCTL / 本地计划与文件</p>
            <h1>
              {page === "plans"
                ? "计划"
                : page === "import"
                  ? "文件导入"
                  : "设备说明"}
            </h1>
            <p>
              {page === "plans"
                ? "准备执行请求，查看主机结果与已接收文件。"
                : page === "import"
                  ? "导入收到的状态报告与视频，查看保存、关联和核验结果。"
                  : "查看当前启用的设备和参数规则，准备符合能力的拍摄计划。"}
            </p>
          </div>
          <div className="connection">
            <span className="live-dot" />
            {connection ? "连接待恢复" : "本地服务已连接"}
          </div>
        </header>
        <ErrorBox
          error={
            connection
              ? `无法取得最新状态：${connection}。下方保留此前读取的结果。`
              : error
          }
        />
        {notice && (
          <p className="notice success" role="status">
            {notice}
          </p>
        )}
        {page === "plans" &&
          [...sessions.current.values()]
            .filter((session) => session.recoveryContent)
            .map((session) => (
              <section className="panel" key={session.draft.id}>
                <h2>保留的额外编辑内容</h2>
                <p>
                  原草稿 {session.draft.id}{" "}
                  已导出，以下本地输入未写入固定原请求，可明确保存为新草稿。
                </p>
                <textarea
                  className="code-input"
                  aria-label="保留的额外编辑内容"
                  readOnly
                  value={session.recoveryContent!.text}
                />
                <Facts value={session.recoveryContent!.pending} />
                {Object.keys(session.recoveryContent!.actionVariants ?? {})
                  .length > 0 && (
                  <p>
                    同时保留了其他动作类型的编辑内容，保存为新草稿后可切换查看。
                  </p>
                )}
                <button
                  disabled={busy}
                  onClick={() =>
                    run(async () => {
                      const draft = await api<Draft>("/drafts", "POST", {
                        content: session.recoveryContent,
                      });
                      session.completeRecovery();
                      openDraft(draft);
                      await refresh();
                    })
                  }
                >
                  将保留内容保存为新草稿
                </button>
              </section>
            ))}
        {page === "import" && state.workerError && (
          <ErrorBox error={`文件后台处理：${state.workerError}`} />
        )}
        {state.historyMissing && (
          <div className="notice warning">
            <strong>主机历史尚未补齐</strong>
            <p>
              本地完整进度 {state.coverage}，已知至少需要补齐至{" "}
              {state.gapTarget}。下方展示最后一次可靠保存的状态。
            </p>
            <button
              onClick={() =>
                startFollow({
                  action: { name: "状态同步", type: "report_status" },
                  summary: "补齐主机状态",
                  sync: true,
                })
              }
            >
              准备补齐历史
            </button>
          </div>
        )}
        {page === "plans" && (
          <>
            <div className="plan-tools">
              <div role="tablist" aria-label="计划分类">
                <button
                  role="tab"
                  aria-selected={tab === "drafts"}
                  onClick={() => setTab("drafts")}
                >
                  草稿 <span>{drafts.length}</span>
                </button>
                <button
                  role="tab"
                  data-testid="tab-records"
                  aria-selected={tab === "records"}
                  onClick={() => setTab("records")}
                >
                  计划记录 <span>{records.length}</span>
                </button>
              </div>
              <button
                disabled={busy}
                onClick={() =>
                  startFollow({
                    action: { name: "状态同步", type: "report_status" },
                    summary: "准备主机状态同步",
                    sync: true,
                  })
                }
              >
                准备状态同步
              </button>
            </div>
            <div className="content-grid">
              <section className="panel list-panel">
                <div className="section-head">
                  <h2>{tab === "drafts" ? "草稿" : "计划记录"}</h2>
                  {tab === "drafts" && (
                    <button
                      className="primary"
                      data-testid="new-draft-button"
                      disabled={busy}
                      onClick={() => run(create)}
                    >
                      新建草稿
                    </button>
                  )}
                </div>
                {tab === "drafts" ? (
                  drafts.length ? (
                    drafts.map((d) => (
                      <button
                        className={`list-item ${selected === d.id ? "selected" : ""}`}
                        data-testid="draft-open-button"
                        key={d.id}
                        onClick={() => openDraft(d)}
                      >
                        <strong>
                          {draftName(
                            sessions.current.get(d.id)?.content.text ??
                              d.content.text,
                          )}
                        </strong>
                        <span>
                          更新于 {utcToLocal(d.updatedAt).replace("T", " ")}
                        </span>
                      </button>
                    ))
                  ) : (
                    <Empty>从一份新草稿开始。未完成的输入也会自动保存。</Empty>
                  )
                ) : records.length ? (
                  records.map((r) => (
                    <button
                      className={`list-item ${recordId === r.id ? "selected" : ""}`}
                      data-testid="record-open-button"
                      key={r.id}
                      onClick={() => openRecord(r.id)}
                    >
                      <strong>
                        {r.plan?.name ?? String(r.request?.body.name ?? r.id)}
                      </strong>
                      <span>
                        {r.plan ? "主机已提供结果" : "已导出，等待报告"}
                      </span>
                      <small>{r.id}</small>
                    </button>
                  ))
                ) : (
                  <Empty>导出计划或导入报告后，可在这里查看记录。</Empty>
                )}
              </section>
              {tab === "drafts" ? (
                current && !current.exportedRequestId ? (
                  <Editor
                    key={selected}
                    session={current}
                    capabilities={state.capabilities.active}
                    presets={state.presets ?? []}
                    reports={state.reports ?? []}
                    coverage={state.coverage ?? 0}
                    busy={busy}
                    onExport={exportDraft}
                    onDelete={() => removeDraft(current)}
                    checkDeletion={() => removeDraft(current, true)}
                    checkExport={() =>
                      run(async () => {
                        await current.checkExport();
                        await refresh();
                      })
                    }
                    savePreset={savePreset}
                  />
                ) : (
                  <section className="panel welcome">
                    <span className="welcome-mark">▤</span>
                    <h2>把下一次拍摄安排好</h2>
                    <p>
                      新建或打开草稿，填写动作与参数。导出后，固定原请求和后续执行结果都保存在计划记录中。
                    </p>
                  </section>
                )
              ) : record ? (
                <RecordDetail
                  key={record.id}
                  record={record}
                  state={state}
                  busy={busy}
                  run={run}
                  copy={copy}
                  handoff={handoff}
                  follow={startFollow}
                  open={openRecord}
                />
              ) : (
                <section className="panel welcome">
                  <h2>查看一份计划记录</h2>
                  <p>选择记录，查看交接、动作结果、产物和本地视频。</p>
                </section>
              )}
            </div>
          </>
        )}
        {page === "import" && (
          <ImportPage
            state={state}
            progress={progress}
            errors={uploadErrors}
            onFiles={(files) => void importFiles(files)}
            run={run}
            open={openRecord}
          />
        )}
        {page === "devices" && (
          <section className="panel device-page">
            <CapabilitiesError
              error={state.capabilities.error}
              directory={state.startup.directory}
            />
            {state.capabilities.error && state.capabilities.active && (
              <p className="notice">继续使用此前成功启用的说明。</p>
            )}
            <DeviceGuide capabilities={state.capabilities.active} />
            <div className="section-head guide-management">
              <div>
                <h2>设备说明管理</h2>
                <p>手工替换说明文件后，重新加载以检查并启用。</p>
              </div>
              <button
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const capabilities = await api<ClientState["capabilities"]>(
                      "/capabilities/reload",
                      "POST",
                      {},
                    );
                    setState((old) => (old ? { ...old, capabilities } : old));
                    setNotice(
                      capabilities.error
                        ? "本次加载未成功，请查看诊断"
                        : "能力说明已重新加载；草稿内容保持，按新规则检查",
                    );
                  })
                }
              >
                重新加载能力说明
              </button>
            </div>
          </section>
        )}
        <footer className="workspace-footer">
          本地完整覆盖：{state.coverage} · 已接受报告：
          {state.reports?.length ?? 0} · 累计确认依据：{state.ackId ?? "暂无"}
        </footer>
      </main>
      {follow && (
        <FollowupDialog
          follow={follow}
          drafts={drafts}
          busy={busy}
          close={() => setFollow(undefined)}
          submit={addFollow}
          operation={activeFollow?.operation}
          viewTarget={() =>
            run(async () => {
              if (activeFollow?.operation.targetId) {
                openDraft(await readDraft(activeFollow.operation.targetId));
              } else {
                setPage("plans");
                setTab("drafts");
                await refresh();
              }
              setFollow(undefined);
            })
          }
        />
      )}
    </div>
  );
}
function draftName(text: string) {
  try {
    const value = parseDraft({ text });
    return typeof value.name === "string" && value.name
      ? value.name
      : "未命名草稿";
  } catch {
    return "待完成的 JSON 草稿";
  }
}
function CapabilitiesError({
  error,
  directory,
}: {
  error: string | null;
  directory: string;
}) {
  if (!error) return null;
  return (
    <div className="notice warning">
      <strong>本次能力说明未能加载</strong>
      <p>
        请将有效的 <code>device-capabilities.json</code>{" "}
        放入以下数据目录，再点击“重新加载能力说明”。已有文件时，请检查文件内容和读取权限。
      </p>
      <p className="directory">{directory}</p>
      <details>
        <summary>查看原始诊断</summary>
        <p>{error}</p>
      </details>
    </div>
  );
}
function FollowupDialog({
  follow,
  drafts,
  busy,
  close,
  submit,
  operation,
  viewTarget,
}: {
  follow: Followup;
  drafts: Draft[];
  busy: boolean;
  close: () => void;
  submit: (id: string, action: Record<string, unknown>) => void;
  operation?: FollowOperation;
  viewTarget: () => void;
}) {
  const [target, setTarget] = useState("new"),
    [name, setName] = useState(String(follow.action.name)),
    [full, setFull] = useState(
      (follow.action.params as { scope?: string })?.scope === "full",
    ),
    [params, setParams] = useState<Record<string, unknown>>(),
    [error, setError] = useState("");
  useEffect(() => {
    if (!follow.sync) return;
    let active = true;
    setParams(undefined);
    setError("");
    void api<Record<string, unknown>>(`/sync?full=${full}`)
      .then((p) => {
        if (active) setParams(p);
      })
      .catch((e) => {
        if (active) setError((e as Error).message);
      });
    return () => {
      active = false;
    };
  }, [full, follow.sync]);
  return (
    <div className="modal-backdrop">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="follow-title"
        className="modal panel"
      >
        <h2 id="follow-title">将后续动作加入草稿</h2>
        <p>{follow.summary}</p>
        {operation ? (
          <>
            <ErrorBox error={operation.error} />
            {operation.targetId && (
              <p className="identifier">固定目标：{operation.targetId}</p>
            )}
            <details>
              <summary>查看本次固定动作与追加基线</summary>
              <pre>
                {JSON.stringify(
                  { action: operation.action, baseline: operation.baseline },
                  null,
                  2,
                )}
              </pre>
            </details>
            {operation.actual && operation.phase === "conflict" && (
              <details>
                <summary>查看实际草稿记录</summary>
                <pre>{JSON.stringify(operation.actual, null, 2)}</pre>
              </details>
            )}
            <div className="button-row end">
              <button disabled={busy} onClick={close}>
                返回
              </button>
              <button disabled={busy} onClick={viewTarget}>
                {operation.targetId ? "查看目标草稿" : "查看实际草稿列表"}
              </button>
              {operation.phase !== "creation_unknown" && (
                <button
                  className="primary"
                  disabled={busy || operation.inProgress}
                  onClick={() =>
                    submit(operation.targetId ?? "new", operation.action)
                  }
                >
                  {operation.phase === "unknown" ||
                  operation.phase === "conflict"
                    ? "重新核实追加结果"
                    : operation.phase === "not_appended"
                      ? "重试同一目标追加"
                      : operation.phase === "target"
                        ? "重试准备目标草稿"
                        : "重新创建并追加"}
                </button>
              )}
            </div>
          </>
        ) : (
          <fieldset disabled={busy}>
            <label className="field">
              动作名称
              <input value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            {follow.sync && (
              <>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={full}
                    onChange={(e) => setFull(e.target.checked)}
                  />
                  重新获取完整状态
                </label>
                {params ? (
                  <p className="notice">
                    {params.scope === "full"
                      ? "将获取完整状态；没有可用起点或已主动选择完整同步。"
                      : `将从已完整保存的报告 ${params.after_report_id} 之后补齐。`}
                  </p>
                ) : (
                  <p>正在取得可靠同步起点…</p>
                )}
                <ErrorBox error={error} />
              </>
            )}
            <label className="field">
              目标草稿
              <select
                aria-label="目标草稿"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
              >
                <option value="new">新建一份草稿</option>
                {drafts.map((d) => (
                  <option key={d.id} value={d.id}>
                    {draftName(d.content.text)} · {d.id.slice(0, 8)}
                  </option>
                ))}
              </select>
            </label>
            <p className="muted">
              保留已有动作；执行时间由你在草稿中明确填写。源对象的状态继续以主机报告为准。
            </p>
            <div className="button-row end">
              <button onClick={close}>返回</button>
              <button
                className="primary"
                disabled={!name || !!error || (follow.sync && !params)}
                onClick={() =>
                  submit(target, {
                    ...follow.action,
                    name,
                    ...(follow.sync ? { params } : {}),
                  })
                }
              >
                加入草稿
              </button>
            </div>
          </fieldset>
        )}
      </section>
    </div>
  );
}
function ImportPage({
  state,
  progress,
  errors,
  onFiles,
  run,
  open,
}: {
  state: ClientState;
  progress: Record<string, number>;
  errors: Record<string, string>;
  onFiles: (files: File[]) => void;
  run: (f: () => Promise<void>) => void;
  open: (id: string) => void;
}) {
  const [drag, setDrag] = useState(false);
  const [pageNumber, setPageNumber] = useState(1);
  const allFiles = [...(state.imports ?? [])].reverse();
  const pageCount = Math.max(1, Math.ceil(allFiles.length / 10));
  const currentPage = Math.min(pageNumber, pageCount);
  const visibleFiles = allFiles.slice((currentPage - 1) * 10, currentPage * 10);
  const selectFiles = (files: File[]) => {
    if (files.length) setPageNumber(1);
    onFiles(files);
  };
  const batches = new Map<string, ImportFile[]>();
  for (const file of visibleFiles)
    batches.set(file.batchId, [...(batches.get(file.batchId) ?? []), file]);
  return (
    <>
      <section
        className={`drop-zone ${drag ? "drag" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          selectFiles([...e.dataTransfer.files]);
        }}
      >
        <span className="upload-symbol">↥</span>
        <h2>将报告和视频拖到这里</h2>
        <p>支持一次混合选择。报告按覆盖关系处理，视频独立保存与核验。</p>
        <label className="file-button">
          选择文件
          <input
            type="file"
            multiple
            data-testid="import-files"
            aria-label="选择报告和视频文件"
            onChange={(e) => {
              selectFiles([...(e.target.files ?? [])]);
              e.target.value = "";
            }}
          />
        </label>
        <small>导入在客户端保存副本，你选择的原文件保留。</small>
      </section>
      {!batches.size && (
        <section className="panel">
          <Empty>
            尚未导入文件。收到传回资料后，在这里查看每个文件的结果。
          </Empty>
        </section>
      )}
      {allFiles.length > 0 && (
        <nav className="pagination button-row" aria-label="导入记录分页">
          <span>
            共 {allFiles.length} 个文件 · 第 {currentPage} / {pageCount} 页
          </span>
          <button
            disabled={currentPage === 1}
            onClick={() => setPageNumber(currentPage - 1)}
          >
            上一页
          </button>
          <button
            disabled={currentPage === pageCount}
            onClick={() => setPageNumber(currentPage + 1)}
          >
            下一页
          </button>
        </nav>
      )}
      {[...batches.entries()].map(([id, files]) => (
        <section className="panel import-batch" key={id}>
          <div className="section-head">
            <h2>
              导入批次 <small>{id.slice(0, 8)}</small>
            </h2>
            <span>本页 {files.length} 个文件</span>
          </div>
          {files.map((file) => {
            const bytes =
              file.status === "uploading"
                ? (progress[file.id] ?? file.bytesReceived)
                : file.bytesReceived;
            const requestIds = new Set<string>();
            const accepted = state.reports?.find(
              (r) => r.report_id === file.reportId,
            );
            if (file.videoId)
              for (const plan of state.snapshot?.plans ?? [])
                for (const action of plan.actions ?? [])
                  if (
                    action.deliveries?.some(
                      (d) => d.file_name === file.fileName,
                    )
                  )
                    requestIds.add(plan.request_id);
            return (
              <article className="import-row" key={file.id}>
                <div className="section-head">
                  <div>
                    <strong>{file.fileName}</strong>
                    <small>
                      {file.kind === "report" ? "状态报告" : "视频"} ·{" "}
                      {file.expectedSize.toLocaleString()} 字节
                    </small>
                  </div>
                  <Badge value={file.status} />
                </div>
                {file.status === "uploading" && (
                  <>
                    <progress
                      value={bytes}
                      max={Math.max(file.expectedSize, 1)}
                    />
                    <small>
                      已发送 {bytes.toLocaleString()} /{" "}
                      {file.expectedSize.toLocaleString()} 字节，等待完整保存
                    </small>
                  </>
                )}
                {file.message && <p>{file.message}</p>}
                {errors[file.id] && (
                  <p className="notice">
                    本次传输：{errors[file.id]}。当前结果以上方后端状态为准。
                  </p>
                )}
                {file.reportId !== undefined && (
                  <p className="muted">
                    报告 {file.reportId} · 覆盖 ({file.fromWm}, {file.toWm}]
                  </p>
                )}
                <div className="button-row">
                  {accepted && (
                    <>
                      {accepted.file_name !== file.fileName && (
                        <small>下载已接受版本：{accepted.file_name}</small>
                      )}
                      <button
                        onClick={() =>
                          run(() =>
                            download(
                              `/reports/${accepted.report_id}/download`,
                              accepted.file_name,
                            ),
                          )
                        }
                      >
                        下载已接受报告原文
                      </button>
                    </>
                  )}
                  {[...requestIds].map((id) => (
                    <button
                      className="inline"
                      key={id}
                      onClick={() => open(id)}
                    >
                      查看关联计划
                    </button>
                  ))}
                </div>
              </article>
            );
          })}
        </section>
      ))}
      {state.snapshot?.plan_file_diagnostics?.length ? (
        <section className="panel">
          <h2>主机输入诊断</h2>
          {state.snapshot.plan_file_diagnostics.map((d) => (
            <article className="import-row" key={d.diagnostic_id}>
              <h3>{d.file_name}</h3>
              <Facts value={d.errors} />
            </article>
          ))}
        </section>
      ) : null}
    </>
  );
}
