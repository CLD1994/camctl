// 演示素材生成器。只读取日常客户端；所有导入验证均使用独立临时数据库。
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, mkdtempSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import assert from 'node:assert/strict';
import { loadCapabilities } from '../../apps/client/src/shared/capabilities.ts';
import { validatePlan } from '../../apps/client/src/shared/plan.ts';
import { parseReport, mergeReport } from '../../apps/client/src/domain/reports.ts';
import { Application } from '../../apps/client/src/server/application.ts';
import { Files } from '../../apps/client/src/server/files.ts';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../..');
const read = path => JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''));
const json = value => JSON.stringify(value, null, 2) + '\n';
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const intent = read(join(here, 'plan.json'));
const preview = process.argv[2] === '--preview';
let request;
if (preview) {
  request = { ...intent, request_id: 'demo-preview-not-for-import', created_at: '2026-09-16 07:50:00' };
} else {
  const response = await fetch('http://localhost:4310/api/state');
  if (!response.ok) throw new Error(`读取客户端失败：${response.status}`);
  const state = await response.json();
  if (state.startup.state !== 'ready') throw new Error('客户端尚未就绪');
  if (state.coverage !== 0 || state.reports.length || state.imports.length)
    throw new Error('本套报告从水位 0 开始；当前客户端已有导入记录，请先联系生成者安排独立演示环境，不要删除历史数据。');
  if (process.argv[2]) request = read(resolve(process.argv[2]));
  else {
    const matches = state.requests.filter(r => r.body.name === intent.name);
    if (matches.length !== 1) throw new Error('请先在客户端导出本演示计划；若已导出多份，请将选定的导出文件路径作为命令参数。');
    request = matches[0].body;
  }
  const saved = state.requests.find(r => r.id === request.request_id);
  assert.ok(saved, '导出文件必须来自当前客户端');
  assert.deepEqual(saved.body, request, '文件内容必须与客户端保存的导出请求一致');
}
assert.equal(request.name, intent.name, '请选择本演示计划');
assert.equal(request.actions.length, 3);
// 允许观众调整时间、最大延迟和合法画质，但保持演示的三个动作及其来源关系。
for (let i = 0; i < 3; i++) {
  const actual = request.actions[i], expected = intent.actions[i];
  assert.equal(actual.name, expected.name);
  assert.equal(actual.type, expected.type);
  assert.equal(actual.device_id, expected.device_id);
  if (i < 2) assert.equal(actual.params.type, expected.params.type);
  else assert.deepEqual(actual.params, expected.params);
}
const capabilities = loadCapabilities(read(join(root, 'data/device-capabilities.json')));
assert.deepEqual(validatePlan(request, capabilities), [], '执行计划必须通过当前能力校验');
const time = action => Date.parse(action.scheduled_at.replace(' ', 'T') + 'Z');
assert.ok(time(request.actions[1]) - time(request.actions[0]) >= 60000, '同设备的两段 60 秒录像不能重叠');
assert.ok(time(request.actions[2]) - time(request.actions[1]) >= 60000, '本演示先完成两段录像，再开始取回');
const sources = ['HuffmanExplainer.mp4', 'ModelMemoryStoryboard.mp4'].map(name => {
  const bytes = readFileSync(join(root, 'tmp', name));
  return { name, bytes, size: bytes.length, sha256: digest(bytes) };
});
const key = digest(Buffer.from(request.request_id)).slice(0, 16);
const id = name => `demo-${key}-${name}`;
const plan = {
  plan_instance_id: id('plan'), request_id: request.request_id, plan_seq: 1,
  created_at: request.created_at, name: request.name, status: 'pending',
  actions: request.actions.map((input, i) => {
    const { params, ...common } = input;
    return { ...common, action_instance_id: id(`action-${i+1}`), input_params: params,
      ...(i < 2 ? { effective_params: { ...params, duration_s: 60,
        ...(i === 0 ? { resolution: '1080p', frame_rate_fps: 30 } : {}) } } : {}),
      status: 'pending', execution: { started: false },
      waiting: [{ code: 'scheduled_time', details: {} }] };
  })
};
const [fixed, adjustable, obtain] = plan.actions;
const reports = [];
const stages = [];
function snapshot(title) {
  const number = reports.length + 1;
  const report = { report_id: number, from_wm: (number-1)*10, to_wm: number*10, plans: [structuredClone(plan)] };
  const bytes = Buffer.from(json(report));
  const fileName = `status-report-${number}-${digest(bytes)}.json`;
  parseReport(fileName, bytes);
  reports.push({ report, bytes, fileName, title });
  stages.push(`${String(number).padStart(2,'0')} | ${title}`);
}
function start(action) {
  plan.status = 'running'; action.status = 'running'; action.execution.started = true; delete action.waiting;
  action.result = { recording: {
    start: { max_attempts: 3, attempts: [{ attempt_no: 1, status: 'succeeded' }] },
    stop: { max_attempts: 3, attempts: [] }, control_elapsed_s: 0,
    effect: { status: 'registered' }
  }};
}
function stop(action) {
  action.result.recording.control_elapsed_s = 60;
  action.result.recording.stop.attempts = [{ attempt_no: 1, status: 'succeeded' }];
  action.result.recording.effect.status = 'compensated';
}
function finish(action, index) {
  const source = sources[index];
  action.status = 'succeeded'; action.result.repair = { status: 'not_needed' };
  action.outputs = [{ output_id: id(`output-${index+1}`), source_action_instance_id: action.action_instance_id,
    kind: 'original', original_name: source.name, media_type: 'video/mp4', size: source.size,
    availability: 'available', cleanup: { status: 'not_requested' },
    checksum: { status: 'available', sha256: source.sha256 },
    media: { check_status: 'not_performed', duration: { status: 'unknown' } } }];
}
snapshot('计划已受理：三个动作等待执行');
start(fixed); snapshot('固定参数录像开始');
fixed.result.recording.control_elapsed_s = 30; snapshot('固定参数录像进行中：控制计时 30 秒');
stop(fixed); snapshot('固定参数录像已停止：等待正式登记产物');
finish(fixed, 0); snapshot('固定参数录像完成：第一份产物已登记');
start(adjustable); snapshot('可调整参数录像开始');
adjustable.result.recording.control_elapsed_s = 30; snapshot('可调整参数录像进行中：控制计时 30 秒');
stop(adjustable); finish(adjustable, 1); snapshot('两段录像均完成：两份产物已登记');
obtain.status = 'running'; obtain.execution.started = true; delete obtain.waiting;
obtain.result = { failures: [] };
const delivery = {
  delivery_id: id('delivery-fixed'), output_id: fixed.outputs[0].output_id,
  source_action_instance_id: fixed.action_instance_id,
  file_name: `${id('delivery-fixed')}.mp4`, display_name: '固定参数录像-HuffmanExplainer.mp4',
  status: 'preparing', copy: { max_read_attempts: 3, read_idle_timeout_s: 10, max_recopies: 1,
    recopies_used: 0, round: 1, source_size: sources[0].size, committed_bytes: 0,
    read_attempts: [{ attempt_no: 1, status: 'running' }], verification: { status: 'not_performed' },
    work_file_cleanup: { status: 'not_needed' } }
};
obtain.deliveries = [delivery]; snapshot('开始取回固定参数录像：拷贝 0%');
delivery.copy.committed_bytes = Math.floor(sources[0].size * .25); snapshot('取回拷贝 25%');
delivery.copy.committed_bytes = Math.floor(sources[0].size * .75); snapshot('取回拷贝 75%');
delivery.copy.committed_bytes = sources[0].size;
delivery.copy.read_attempts[0].status = 'succeeded'; delivery.copy.verification.status = 'running';
snapshot('取回拷贝 100%：正在核对文件摘要');
delivery.copy.verification.status = 'matched'; delivery.size = sources[0].size; delivery.sha256 = sources[0].sha256;
delivery.status = 'prepared'; snapshot('文件摘要匹配：交付准备完成');
delivery.status = 'publishing'; snapshot('交付文件正在发布');
delivery.status = 'published'; obtain.status = 'succeeded'; plan.status = 'completed';
snapshot('计划完成：固定参数录像已发布，等待传输部门送达');

let merged = { report_id: 1, from_wm: 0, to_wm: 0 };
for (const item of reports) merged = mergeReport(merged, item.report);
assert.equal(merged.plans[0].status, 'completed');
assert.equal(merged.plans[0].actions.flatMap(a => a.outputs ?? []).length, 2);
assert.equal(merged.plans[0].actions.flatMap(a => a.deliveries ?? []).length, 1);

// 用客户端真实导入服务验证报告链、视频映射、大小和 SHA-256，避免只验证 JSON 结构。
const validationDir = mkdtempSync(join(tmpdir(), 'camctl-demo-validation-'));
copyFileSync(join(root, 'data/device-capabilities.json'), join(validationDir, 'device-capabilities.json'));
const app = new Application(validationDir); app.store.initialize();
const files = new Files(app);
try {
  for (const item of reports) {
    const batch = files.createBatch([{ fileName: item.fileName, size: item.bytes.length, kind: 'report' }]);
    await files.upload(batch.fileIds[0], Readable.from([item.bytes])); await files.idle();
    assert.equal(app.store.get('imports', batch.fileIds[0]).status, 'accepted', item.title);
  }
  const batch = files.createBatch([{ fileName: delivery.file_name, size: sources[0].size, kind: 'video' }]);
  await files.upload(batch.fileIds[0], Readable.from([sources[0].bytes])); await files.idle();
  assert.equal(app.store.get('imports', batch.fileIds[0]).status, 'verified');
  assert.equal(app.coverage(), 150);
} finally { await files.idle(); app.store.close(); }

const outputRoot = join(root, '.local', 'capture-and-retrieve');
mkdirSync(outputRoot, { recursive: true });
const output = preview ? join(outputRoot, `preview-${Date.now()}`) : join(outputRoot, `ready-${key}`);
mkdirSync(output); // 不覆盖此前交接过的报告或视频。
for (const [index, item] of reports.entries()) {
  const folder = join(output, `${String(index+1).padStart(2,'0')}-${item.title.replace(/[：%]/g,'-')}`);
  mkdirSync(folder); writeFileSync(join(folder, item.fileName), item.bytes);
}
const videoFolder = join(output, '16-导入固定参数视频并播放'); mkdirSync(videoFolder);
copyFileSync(join(root, 'tmp', sources[0].name), join(videoFolder, delivery.file_name));
writeFileSync(join(output, 'exported-plan.json'), json(request));
writeFileSync(join(output, 'manifest.json'), json({ preview, request_id: request.request_id,
  sources: sources.map(({ bytes, ...source }, i) => ({ ...source, action: request.actions[i].name, output_id: id(`output-${i+1}`), retrieved: i === 0 })),
  reports: reports.map(({ report, bytes, ...item }) => ({ ...item, report_id: report.report_id, from_wm: report.from_wm, to_wm: report.to_wm })),
  delivery_file: delivery.file_name, validation: { reports_accepted: 15, video_status: 'verified', coverage: 150, directory: validationDir } }));
writeFileSync(join(output, '演示顺序.txt'), (preview ? '仅供预览。真实演示须在导出后运行配对命令。\n\n' : '与客户端实际导出请求已配对。\n\n') + stages.join('\n') + '\n16 | 导入本目录第 16 步中的视频，等待校验通过后播放。\n每步只选该步骤内的文件；不要一次性导入全部报告。\n');
console.log(`已生成：${output}\n15 份报告已逐份通过真实导入；视频已校验通过。\n请求：${request.request_id}\n验证数据库：${validationDir}`);
