import type { PlanRecord } from "./editing";
import { Empty } from "./common";

const RECORDS_PER_PAGE = 10;
export function recordListPage(records: PlanRecord[], requested: number) {
  const pageCount = Math.max(1, Math.ceil(records.length / RECORDS_PER_PAGE));
  const page = Math.max(1, Math.min(requested, pageCount));
  return {
    page,
    pageCount,
    records: records.slice(
      (page - 1) * RECORDS_PER_PAGE,
      page * RECORDS_PER_PAGE,
    ),
  };
}
export function pageForRecord(records: PlanRecord[], id: string) {
  const index = records.findIndex((record) => record.id === id);
  return index < 0 ? undefined : Math.floor(index / RECORDS_PER_PAGE) + 1;
}
export function PlanRecordList({
  records,
  selectedId,
  page,
  onPageChange,
  onOpen,
}: {
  records: PlanRecord[];
  selectedId: string;
  page: number;
  onPageChange: (page: number) => void;
  onOpen: (id: string) => void;
}) {
  const visible = recordListPage(records, page);
  if (!records.length)
    return <Empty>导出计划或导入报告后，可在这里查看记录。</Empty>;
  return (
    <>
      {visible.records.map((record) => (
        <button
          className={`list-item ${selectedId === record.id ? "selected" : ""}`}
          data-testid="record-open-button"
          key={record.id}
          aria-pressed={selectedId === record.id}
          onClick={() => onOpen(record.id)}
        >
          <strong>
            {record.plan?.name ??
              String(record.request?.body.name ?? record.id)}
          </strong>
          <span>{record.plan ? "主机已提供结果" : "已导出，等待报告"}</span>
          <small>{record.id}</small>
        </button>
      ))}
      <nav className="record-pagination" aria-label="计划记录分页">
        <span>
          共 {records.length} 条 · {visible.page} / {visible.pageCount}
        </span>
        <div className="button-row">
          <button
            disabled={visible.page === 1}
            onClick={() => onPageChange(visible.page - 1)}
          >
            上一页
          </button>
          <button
            disabled={visible.page === visible.pageCount}
            onClick={() => onPageChange(visible.page + 1)}
          >
            下一页
          </button>
        </div>
      </nav>
    </>
  );
}
