import assert from 'node:assert/strict';
import test from 'node:test';
import {pointer,withLocalSchemas} from './report-dependencies.mjs';

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
