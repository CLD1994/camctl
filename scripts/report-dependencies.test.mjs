import assert from 'node:assert/strict';
import test from 'node:test';
import {pointer,withLocalSchemas,validateReadScopes} from './report-dependencies.mjs';

test('报告读取阶段拒绝未登记阶段和错误的成员引用方向',()=>{
 const registry={projections:{coverage:{}},relations:{scopeMember:{from:'scope.member_id',to:'members.id',read_phase:['coverage']}},assertions:{scope:{read_phase:['coverage']}},read_expansions:{coverage:{trigger:{kind:'projection',projection:'coverage'},relations:['scopeMember'],assertion:'scope',members:{relation:'scopeMember',selection:'referenced'}}}};
 validateReadScopes(registry,{});
 const unknown=structuredClone(registry);unknown.relations.scopeMember.read_phase=['missing'];
 assert.throws(()=>validateReadScopes(unknown,{}),/未登记的报告读取阶段/);
 const wrong=structuredClone(registry);wrong.relations.scopeMember.to='members.set_id';
 assert.throws(()=>validateReadScopes(wrong,{}),/成员引用/);
});

test('跨文件公共定义保留原文件局部引用并闭合回到根文件的引用',()=>{
 const root={$defs:{id:{type:'string'}},properties:{params:{$ref:'host-notification.schema.json#/$defs/params'}}};
 const external={$defs:{params:{type:'object',properties:{id:{$ref:'status-report.schema.json#/$defs/id'},position:{$ref:'#/$defs/position'}}},position:{type:'integer'}}};
 const expanded=withLocalSchemas(root,{'host-notification.schema.json':external});
 const params=pointer(expanded,expanded.properties.params.$ref);
 assert.equal(pointer(expanded,params.properties.id.$ref).type,'string');
 assert.equal(pointer(expanded,params.properties.position.$ref).type,'integer');
 assert.equal(root.properties.params.$ref,'host-notification.schema.json#/$defs/params');
 assert.equal(external.$defs.params.properties.position.$ref,'#/$defs/position');
});

test('缺少权威文件或非指针引用不能默认解释成文档内字段',()=>{
 assert.throws(()=>withLocalSchemas({$ref:'missing.json#/a'}),/未提供公共 Schema/);
 assert.throws(()=>withLocalSchemas({$ref:'#anchor'}),/仅允许 JSON Pointer/);
});
