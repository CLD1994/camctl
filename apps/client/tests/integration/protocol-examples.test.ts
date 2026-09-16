import { readFile, readdir } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { describe, it, expect } from 'vitest';
import { parseReport, mergeReport } from '../../src/domain/reports';
import { loadCapabilities, validateParams } from '../../src/shared/capabilities';
import { parseJson } from '../../src/shared/json';

const root = join(import.meta.dirname, '../../../../protocol/examples');
const files = (await readdir(root, { recursive: true })).filter(f => basename(f).startsWith('status-report-') && f.endsWith('.json'));
async function reportFile(directory: string, id: number) { const file = files.find(f => f.startsWith(directory) && basename(f).startsWith(`status-report-${id}-`))!; return parseReport(basename(file), await readFile(join(root, file))); }
describe('公共协议样例的真实文件集成', () => {
  it.each(files)('报告原文通过摘要、结构及语义检查：%s', async file => { const r = parseReport(basename(file), await readFile(join(root, file))); expect(r.report_id).toBeGreaterThan(0); });
  it('能力说明的组合约束由真实导出 Schema 校验', async () => { const caps = loadCapabilities(parseJson(await readFile(join(root, 'capabilities/demo-device.json'), 'utf8'))); expect(validateParams('demo_cam0', 'camera_record', { type: 'demo_adjustable', resolution: '1080p', frame_rate_fps: 60 }, caps)).toEqual([]); expect(validateParams('demo_cam0', 'camera_record', { type: 'demo_adjustable', resolution: '4K', frame_rate_fps: 60 }, caps).length).toBeGreaterThan(0); });
  it('增量报告与规定的完整合并计划一致', async () => { const first = await reportFile(join('client-protocol', '02-action-invalid'), 1); const next = await reportFile(join('client-protocol', '02-action-invalid'), 2); const expected = parseJson(await readFile(join(root, 'client-protocol/02-action-invalid/client-merged-plan.json'), 'utf8')); expect(mergeReport(first, next).plans?.[0]).toEqual(expected); });
  it('旧备份按补齐报告保留未出现交付并更新清理结果', async () => { const first = await reportFile('status-sync', 1); const next = await reportFile('status-sync', 5); const merged = mergeReport(first, next); expect(merged.to_wm).toBe(48); expect(merged.plans?.flatMap(p => p.actions ?? []).flatMap(a => a.deliveries ?? [])).toHaveLength(2); expect(merged.plans?.flatMap(p => p.actions ?? []).flatMap(a => a.outputs ?? []).filter(o => o.availability === 'cleaned')).toHaveLength(1); });
});
