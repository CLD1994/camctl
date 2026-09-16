import { Select, SelectItem } from "./Select";
import { useEffect, useRef, useState } from "react";
import type { Product } from "./result-model";
import type { Followup } from "./Records";
import { previewKind } from "../shared/media";
import { download } from "./api";
import { Badge, Facts } from "./common";

const groupNames = { video: "视频", image: "图片", other: "其他文件" };
const source = (id: string) => `/api/media/${encodeURIComponent(id)}/content`;
type Props = {
  products: Product[];
  active: boolean;
  follow: (f: Followup) => void;
  run: (fn: () => Promise<void>) => void;
  open: (id: string) => void;
};
export function MediaResults({ products, active, follow, run, open }: Props) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const chosen = products.filter((p) => selected.has(p.id));
  const change = (ids: string[], value: boolean) =>
    setSelected((old) => {
      const next = new Set(old);
      for (const id of ids) value ? next.add(id) : next.delete(id);
      return next;
    });
  function obtain(rows: Product[]) {
    // 每次选择归属于一个来源动作；跨来源操作分开准备，保留现有草稿流程。
    const sources = new Map<string, string[]>();
    for (const p of rows) {
      const id =
        p.output?.source_action_instance_id ??
        p.deliveries[0]?.delivery.source_action_instance_id;
      if (id) sources.set(id, [...(sources.get(id) ?? []), p.id]);
    }
    const [id, output_ids] = [...sources][0] ?? [];
    if (id)
      follow({
        action: {
          name: "取回所选产物",
          type: "obtain_action_outputs",
          params: { source: { action_instance_id: id }, output_ids },
        },
        summary: `取回所选 ${output_ids.length} 个产物`,
      });
  }
  const sourceIds = new Set(
    chosen.map(
      (p) =>
        p.output?.source_action_instance_id ??
        p.deliveries[0]?.delivery.source_action_instance_id,
    ),
  );
  return (
    <section className="media-results">
      <div className="button-row">
        <strong>已报告 {products.length} 个产物</strong>
        <span>已选择 {chosen.length} 个文件</span>
        <button
          disabled={!chosen.length || sourceIds.size !== 1}
          onClick={() => obtain(chosen)}
        >
          准备取回所选产物
        </button>
        <button
          disabled={!chosen.length}
          onClick={() => setSelected(new Set())}
        >
          清空选择
        </button>
      </div>
      {sourceIds.size > 1 && (
        <div className="notice">
          <p>所选文件来自多个拍摄动作，按来源分别加入草稿：</p>
          {[...sourceIds].map((id) => (
            <button
              key={id}
              onClick={() =>
                obtain(
                  chosen.filter(
                    (p) =>
                      (p.output?.source_action_instance_id ??
                        p.deliveries[0]?.delivery.source_action_instance_id) ===
                      id,
                  ),
                )
              }
            >
              准备取回来源 {id} 的所选{" "}
              {
                chosen.filter(
                  (p) =>
                    (p.output?.source_action_instance_id ??
                      p.deliveries[0]?.delivery.source_action_instance_id) ===
                    id,
                ).length
              }{" "}
              个文件
            </button>
          ))}
        </div>
      )}
      {(["video", "image", "other"] as const).map((group) => {
        const rows = products.filter((p) => p.group === group);
        return rows.length ? (
          <ProductGroup
            key={group}
            {...{ active, follow, run, open, selected, change }}
            name={groupNames[group]}
            products={rows}
          />
        ) : null;
      })}
    </section>
  );
}
function ProductGroup({
  name,
  products,
  active,
  follow,
  run,
  open,
  selected,
  change,
}: Props & {
  name: string;
  selected: Set<string>;
  change: (ids: string[], value: boolean) => void;
}) {
  const [page, setPage] = useState(1),
    [filter, setFilter] = useState("all");
  const [current, setCurrent] = useState<string>();
  const filtered = products.filter(
    (p) => filter === "all" || p.state === filter,
  );
  const pages = Math.max(1, Math.ceil(filtered.length / 12)),
    number = Math.min(page, pages);
  const rows = filtered.slice((number - 1) * 12, number * 12);
  const images = filtered.filter(
    (p) => previewKind(p.mediaType) === "image" && p.state === "ready",
  );
  const image = current ? images.find((p) => p.id === current) : undefined;
  const trigger = useRef<HTMLElement | null>(null);
  const groupRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!active) setCurrent(undefined);
  }, [active]);
  function close() {
    setCurrent(undefined);
    if (trigger.current?.isConnected) trigger.current.focus();
    else
      groupRef.current
        ?.querySelector<HTMLElement>('[role="combobox"]')
        ?.focus();
  }
  function navigate(offset: number) {
    const i = images.findIndex((p) => p.id === current);
    if (i >= 0 && images[i + offset]) setCurrent(images[i + offset].id);
  }
  return (
    <section className="product-group" ref={groupRef}>
      <div className="section-head">
        <h4>
          {name} · {products.length}
        </h4>
        <label>
          筛选{" "}
          <Select
            aria-label={`${name}筛选`}
            value={filter}
            onValueChange={(selectedValue) => {
              setFilter(selectedValue);
              setPage(1);
            }}
          >
            <SelectItem value="all">全部</SelectItem>
            <SelectItem value="ready">可查看</SelectItem>
            <SelectItem value="waiting">未收到可查看文件</SelectItem>
            <SelectItem value="attention">需处理</SelectItem>
          </Select>
        </label>
      </div>
      <div className="button-row pagination">
        <label>
          <input
            type="checkbox"
            checked={!!rows.length && rows.every((p) => selected.has(p.id))}
            disabled={!rows.length}
            onChange={(e) =>
              change(
                rows.map((p) => p.id),
                e.target.checked,
              )
            }
          />
          全选当前页
        </label>
        <span>
          共 {filtered.length} 个 · 第 {number} / {pages} 页
        </span>
        <button
          aria-label={`${name}上一页`}
          disabled={number === 1}
          onClick={() => setPage(number - 1)}
        >
          上一页
        </button>
        <button
          aria-label={`${name}下一页`}
          disabled={number === pages}
          onClick={() => setPage(number + 1)}
        >
          下一页
        </button>
      </div>
      {!rows.length && <p>没有符合筛选条件的文件。</p>}
      <div className={name === "图片" ? "product-grid" : "product-list"}>
        {rows.map((p) => (
          <ProductCard
            key={p.id}
            product={p}
            {...{ active, follow, run, open }}
            selected={selected.has(p.id)}
            select={(v) => change([p.id], v)}
            preview={(e) => {
              trigger.current = e;
              setCurrent(p.id);
            }}
          />
        ))}
      </div>
      {pages > 1 && (
        <div className="button-row pagination">
          <span>
            第 {number} / {pages} 页
          </span>
          <button
            aria-label={`底部${name}上一页`}
            disabled={number === 1}
            onClick={() => {
              setPage(number - 1);
              groupRef.current?.scrollIntoView({ block: "start" });
            }}
          >
            上一页
          </button>
          <button
            aria-label={`底部${name}下一页`}
            disabled={number === pages}
            onClick={() => {
              setPage(number + 1);
              groupRef.current?.scrollIntoView({ block: "start" });
            }}
          >
            下一页
          </button>
        </div>
      )}
      {active && current && (
        <ImageDialog
          product={image}
          close={close}
          previous={() => navigate(-1)}
          next={() => navigate(1)}
          hasPrevious={!!image && images.indexOf(image) > 0}
          hasNext={!!image && images.indexOf(image) < images.length - 1}
          run={run}
        />
      )}
    </section>
  );
}
function ProductCard({
  product: p,
  active,
  follow,
  run,
  open,
  selected,
  select,
  preview,
}: {
  product: Product;
  active: boolean;
  follow: Props["follow"];
  run: Props["run"];
  open: Props["open"];
  selected: boolean;
  select: (v: boolean) => void;
  preview: (e: HTMLElement) => void;
}) {
  const available = p.deliveries.find((d) => d.video?.status === "verified");
  const video = available?.video;
  const kind = previewKind(p.mediaType);
  const [broken, setBroken] = useState<string>();
  const invalid = video && broken === video.id;
  return (
    <article className="product-card">
      <label className="product-name">
        <input
          aria-label={`选择 ${p.name}`}
          type="checkbox"
          checked={selected}
          onChange={(e) => select(e.target.checked)}
        />
        <strong>{p.name}</strong>
      </label>
      <p className="product-state">
        {p.state === "ready"
          ? "已收到 · 核验通过"
          : p.state === "attention"
            ? "文件需要处理"
            : p.deliveries.some((d) => d.video?.status === "verifying")
              ? "正在核验"
              : p.deliveries.some((d) => d.video?.status === "waiting_report")
                ? "已保存，等待对应报告"
                : p.deliveries.some((d) => d.delivery.status === "published")
                  ? "等待接收"
                  : "尚无本地副本"}
      </p>
      {p.problems > 0 && (
        <p className="notice error">{p.problems} 次交付或文件核验异常</p>
      )}
      {active && video && kind === "image" && !invalid && (
        <button
          className="thumbnail"
          aria-label={`查看图片 ${p.name}`}
          onClick={(e) => preview(e.currentTarget)}
        >
          <img
            data-thumbnail
            src={source(video.id)}
            alt={p.name}
            onError={() => setBroken(video.id)}
          />
        </button>
      )}
      {active && video && kind === "video" && !invalid && (
        <video
          controls
          preload="metadata"
          src={source(video.id)}
          onError={() => setBroken(video.id)}
        />
      )}
      {video && (!kind || invalid) && (
        <p role="alert" className="notice">
          {kind === "video" ? "无法在浏览器播放" : "无法预览此文件"}
          ，可以下载原文件查看。
        </p>
      )}
      {video && (
        <button
          onClick={() =>
            run(() =>
              download(source(video.id) + "?download=true", video.fileName),
            )
          }
        >
          {kind === "video"
            ? "下载原视频"
            : kind === "image"
              ? "下载原图片"
              : "下载原文件"}
        </button>
      )}
      <details className="advanced">
        <summary>更多操作与交付记录</summary>
        <button
          onClick={() =>
            follow({
              action: {
                name: "取回产物",
                type: "obtain_action_outputs",
                params: {
                  source: {
                    action_instance_id:
                      p.output?.source_action_instance_id ??
                      p.deliveries[0]?.delivery.source_action_instance_id,
                  },
                  output_ids: [p.id],
                },
              },
              summary: "取回产物 " + p.name,
            })
          }
        >
          准备取回
        </button>
        {p.output && (
          <div className="button-row">
            <Badge value={p.output.availability} />
            <button
              onClick={() =>
                follow({
                  action: {
                    name: "清理源产物",
                    type: "delete_action_outputs",
                    params: { output_ids: [p.id] },
                  },
                  summary: `清理源文件 ${p.name}`,
                })
              }
            >
              准备清理源产物
            </button>
          </div>
        )}
        {p.deliveries.map(({ delivery: d, video: v, owner }) => (
          <section className="delivery" key={d.delivery_id}>
            <strong>{d.display_name}</strong>
            <Badge value={d.status} />
            {!v && (
              <p>
                本地文件：
                {d.status === "published" ? "等待接收" : "尚无本地副本"}
              </p>
            )}
            {v && <Badge value={v.status} />}
            {v?.message && <p>{v.message}</p>}
            {owner?.request_id && (
              <button onClick={() => open(owner?.request_id)}>
                查看取回计划：{owner!.name}
              </button>
            )}
            {v?.status === "verified" && (
              <button
                onClick={() =>
                  run(() =>
                    download(source(v.id) + "?download=true", v.fileName),
                  )
                }
              >
                下载此次交付
              </button>
            )}
            <details>
              <summary>技术详情</summary>
              <Facts value={d} business />
            </details>
          </section>
        ))}
        {p.sourcePlan && (
          <button onClick={() => open(p.sourcePlan!.request_id)}>
            查看来源计划：{p.sourcePlan.name}
          </button>
        )}
        {p.output && (
          <details>
            <summary>产物技术详情</summary>
            <Facts value={p.output} business />
          </details>
        )}
      </details>
    </article>
  );
}
function ImageDialog({
  product,
  close,
  previous,
  next,
  hasPrevious,
  hasNext,
  run,
}: {
  product?: Product;
  close: () => void;
  previous: () => void;
  next: () => void;
  hasPrevious: boolean;
  hasNext: boolean;
  run: Props["run"];
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [broken, setBroken] = useState<string>();
  const video = product?.deliveries.find(
    (d) => d.video?.status === "verified",
  )?.video;
  useEffect(() => {
    ref.current?.showModal();
    return () => ref.current?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="image-dialog"
      aria-label="图片查看"
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft") {
          e.preventDefault();
          previous();
        }
        if (e.key === "ArrowRight") {
          e.preventDefault();
          next();
        }
      }}
    >
      <div className="section-head">
        <strong>{product?.name ?? "图片当前不可用"}</strong>
        <button autoFocus onClick={close}>
          关闭
        </button>
      </div>
      {video && broken !== video.id ? (
        <img
          key={video.id}
          src={source(video.id)}
          alt={product!.name}
          onError={() => setBroken(video.id)}
        />
      ) : (
        <p>图片当前无法预览，请检查文件状态；核验通过的原文件可以下载。</p>
      )}
      <div className="button-row">
        <button disabled={!hasPrevious} onClick={previous}>
          上一张
        </button>
        <button disabled={!hasNext} onClick={next}>
          下一张
        </button>
        {video && (
          <button
            onClick={() =>
              run(() =>
                download(source(video.id) + "?download=true", video.fileName),
              )
            }
          >
            下载原图片
          </button>
        )}
      </div>
    </dialog>
  );
}
