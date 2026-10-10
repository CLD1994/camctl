import assert from 'node:assert/strict';
import test from 'node:test';
import * as checker from './check-protocol.mjs';
import { readFile } from 'node:fs/promises';

const estimateCases = JSON.parse(await readFile(new URL('../protocol/examples/video-size-estimate/cases.json', import.meta.url), 'utf8'));
for (const entry of estimateCases) {
  test(`视频大小估算能力结构：${entry.name}`, () => {
    assert.equal(checker.checkCapabilityStructure(entry.json), entry.schema_valid);
  });
}

// 预期独立于共享夹具，检验检查器不会把二进制浮点舍入误认作合法整数。
test('协议检查器精确处理位置 token 和受理层次', () => {
  assert.equal(typeof checker.checkMotorFixture, 'function');
  const notification = position => `{"type":"motor_control","action_instance_id":"12","params":{"position":${position}}}`;
  for (const [token, valid] of [
    ['-2147483648', true], ['2147483647', true], ['1.0', true], ['1e2', true],
    ['1.00000000000000000001', false], ['2147483647.00000000000001', false],
    ['1e-1000', false], ['1e1000', false], ['true', false], ['"1"', false],
  ]) assert.equal(checker.checkMotorFixture('notification', notification(token)).valid, valid, token);
  assert.equal(checker.checkMotorFixture('notification', '{"type":"motor_control","action_instance_id":"12","params":{"position":1,"position":2}}').valid, false);
  const plan = action => JSON.stringify({ request_id:'1', created_at:'2026-10-08 08:00:00', name:'定位', actions:[action] });
  const action = {name:'定位',type:'motor_control',scheduled_at:'2026-10-08 09:00:00',params:{position:1},policy:{max_delay_ms:0}};
  assert.equal(checker.checkMotorFixture('plan', plan({...action,params:{position:true}})).admission, 'action_failed');
  assert.equal(checker.checkMotorFixture('plan', plan({...action,type:'unknown'})).admission, 'plan_rejected');
  assert.equal(checker.checkMotorFixture('plan', plan({...action,device_id:'motor'})).admission, 'action_failed');
});
