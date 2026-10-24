#!/usr/bin/env python3
"""从已归档计时 CSV 与指定 Git 源码派生节点变化，不导入或执行测试。

输入来源：同目录上一级的 2026-10-25-integration-timing CSV、
cd228b16 的原测试定义，以及 --ref 指定的当前测试定义。
运行命令和解释器要求保存在生成的 final-expected-scope.json 中。
本文件只生成范围预期，不能证明任何测试已执行或通过。
"""
from __future__ import annotations
import argparse,ast,collections,csv,itertools,json,subprocess
from pathlib import Path

TIMING='docs/camctl/verification-data/2026-10-25-integration-timing'
CSV_NAMES=('camctl-cases.csv','cross-component-cases.csv','cross-component-subchecks.csv')
CANCEL='apps/camctl/tests/integration/cancellation/test_hasselblad_fixed_request_scope.py'
CAPTURE='apps/camctl/tests/integration/capture/test_record_segment_scope_recovery.py'
OUTPUTS='apps/camctl/tests/integration/outputs/test_read_discovery.py'
REPORTING='apps/camctl/tests/integration/reporting/test_record_segment_error_report.py'
HISTORY='apps/camctl/tests/integration/history/test_record_segment_history.py'
SNAPSHOT='apps/camctl/tests/integration/history/test_snapshot_fix_round1.py'
ACCEPT='apps/camctl/tests/integration/acceptance/test_validation.py'
PACKAGE='apps/camctl/tests/integration/bootstrap/test_package.py'
CACHE='apps/camctl/tests/integration/persistence/test_page_read_cache.py'
WATCH='tests/integration/test_host_wait_failures.py'


def rows(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))


def source(repo,ref,path):
    if ref is None:
        return (repo/path).read_text()
    return subprocess.check_output(['git','show',f'{ref}:{path}'],cwd=repo,text=True)


def functions(text):
    return {node.name:node for node in ast.parse(text).body
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}


def param_id(value,name,index):
    if value is None or isinstance(value,(str,int,float,bool)):
        return str(value)
    return name+str(index)


def literal_cases(text,name):
    function=functions(text)[name]
    decorators=[]
    for decorator in reversed(function.decorator_list):
        if not isinstance(decorator,ast.Call) or not isinstance(decorator.func,ast.Attribute) or decorator.func.attr!='parametrize':
            continue
        names=ast.literal_eval(decorator.args[0])
        names=[part.strip() for part in names.split(',')] if isinstance(names,str) else names
        values=ast.literal_eval(decorator.args[1])
        assert not decorator.keywords, (name,'explicit ids require independent handling')
        alternatives=[]
        for index,raw in enumerate(values):
            values_tuple=(raw,) if len(names)==1 else raw
            alternatives.append((dict(zip(names,values_tuple)), '-'.join(param_id(value,key,index) for key,value in zip(names,values_tuple))))
        decorators.append(alternatives)
    result=[]
    for combined in itertools.product(*decorators):
        params={key:value for data,_ in combined for key,value in data.items()}
        suffix='-'.join(identity for _,identity in combined)
        result.append((name+(f'[{suffix}]' if decorators else ''),params))
    return result


def derive(repo,ref,before='cd228b16'):
    base=repo/TIMING
    baseline=[row for name in CSV_NAMES[:2] for row in rows(base/name)]
    assert len(baseline)==10394
    assert len({row['nodeid'] for row in baseline})==10394
    by_id={row['nodeid']:row for row in baseline}
    replacements={}
    groups=[]
    def group(label,old,new,contract,kind='merge'):
        assert set(old)<=set(by_id), (label,set(old)-set(by_id))
        assert not set(old)&set(replacements), label
        for prior in old:
            replacements[prior]=new[prior] if isinstance(new,dict) else new
        destinations={replacements[item] for item in old if replacements[item] is not None}
        integrations={item for item in destinations if not item.startswith('apps/camctl/tests/unit/')}
        existing_targets=integrations & (set(by_id)-set(old))
        groups.append({'label':label,'kind':kind,'old_nodes':len(old),'replacement_integration_nodes':len(integrations),
                       'existing_target_nodes':len(existing_targets),'delta':len(integrations-existing_targets)-len(old),'contract':contract,
                       'mapping':[{'old':item,'current':replacements[item]} for item in sorted(old)]})
    old_accept=[item for item in by_id if item.startswith(ACCEPT+'::') and ('::TestPreciseNumbers::' in item or item.endswith('::TestRuleErrors::test_invalid_schema_is_rule_error'))]
    group('acceptance局部重复断言',old_accept,{item:item.replace('/integration/','/unit/',1) for item in old_accept},
          '六项精确数字与非法Schema的同输入/同预期由既有unit/acceptance同名节点承接；真实正文、登记与引用见证保留。','existing_unit')
    old_package=PACKAGE+'::test_resource_names_reject_arbitrary_paths'
    group('资源名拒绝迁移',[old_package],{old_package:'apps/camctl/tests/unit/test_resources.py::test_resource_names_reject_arbitrary_paths'},
          '六个路径反例全部拒绝；新单元节点使用内存登记并阻断真实资源入口，wheel字节及同步集成见证保留。','migrate_unit')
    old_cancel=source(repo,before,CANCEL);new_cancel=source(repo,ref,CANCEL)
    cancel_contracts={
      'test_original_source_cannot_disappear_behind_preview_label':'按(state,kind)共享8个世界，三basis与两个入口48个拒绝组合保留；每次精确回滚、事务结束、完整dump与取消标记不变。',
      'test_complete_model_rejects_omissions_extra_members_and_wrong_effects':'按真实初始(state,kind)共享3个世界；七种错误成员/效果与两个入口14个拒绝组合保留。',
      'test_original_sql_and_complete_rows_keep_same_start_partition':'七个state世界逐一核对HB与generic两类driver，14个SQL/完整行START分区比较保留。',
      'test_complete_scope_requires_authoritative_address_links_and_start_ranges':'按kind共享4个世界；10种覆盖键缺失逐个拒绝，检查事务、完整dump和标记不变。',
    }
    for name,contract in cancel_contracts.items():
        old=literal_cases(old_cancel,name);new=literal_cases(new_cancel,name)
        old_ids={CANCEL+'::'+node for node,_ in old}
        assert old_ids=={node for node in by_id if node.startswith(CANCEL+'::'+name+'[')},name
        mapping={}
        for node,parameters in old:
            matches=[]
            for current,current_parameters in new:
                if name=='test_complete_model_rejects_omissions_extra_members_and_wrong_effects':
                    match=all(parameters[key]==current_parameters[key] for key in ('state','kind')) and parameters['entries'] in current_parameters['entry_sets']
                elif name=='test_complete_scope_requires_authoritative_address_links_and_start_ranges':
                    match=parameters['kind']==current_parameters['kind'] and parameters['missing'] in current_parameters['missing_ranges']
                else:
                    match=all(parameters[key]==value for key,value in current_parameters.items())
                if match: matches.append(CANCEL+'::'+current)
            assert len(matches)==1,(name,parameters,matches)
            mapping[CANCEL+'::'+node]=matches[0]
        group(name,list(old_ids),mapping,contract)
    old=HISTORY+'::test_snapshot_same_s_compares_all_bytes_and_broken_content_fails'
    group('同S快照串入middle取消场景',[old],{old:SNAPSHOT+'::test_cancel_started_save_keeps_source_until_actual_completion[middle]'},
          '同一220成员/H世界；先完成真实worker、来源清理、快照行数并恢复包装，再从current投影准备独立来源，核对同S完整字节、偏移65540来源损坏及BLOB损坏。')
    group('缺段报告与清理后字节重建',[
        REPORTING+'::test_gap_report_preserves_retained_outputs_and_default_prefix',
        REPORTING+'::test_segment_report_rebuilds_identical_bytes_after_cleanup'],
        REPORTING+'::test_gap_report_preserves_outputs_scope_and_bytes_after_cleanup',
        '同一(7,8,10)来源先核对保留产物、9号缺口及默认范围，再改变清理事实并按原冻结依据核对bytes/size/SHA。')
    prefix=OUTPUTS+'::test_candidate_capacity_tracks_batch_not_total'
    group('候选容量三次独立扫描',[item for item in by_id if item.startswith(prefix+'[')],prefix,
          '同库先12再120候选，(12,1)/(120,1)/(120,3)均执行新owner/连接探针；批次、弱引用/GC、游标、数量、查询计划保留并核数据库不变。')
    old_name='test_handler_publishes_directory_rollover_preview_only_start_and_unknown_order'
    new_name='test_handler_preserves_preview_only_start_and_unknown_order'
    old=literal_cases(source(repo,before,CAPTURE),old_name);new=literal_cases(source(repo,ref,CAPTURE),new_name)
    mapping={}
    for node,parameters in old:
        matches=[CAPTURE+'::'+current for current,current_parameters in new if current_parameters==parameters]
        mapping[CAPTURE+'::'+node]=matches[0] if matches else CAPTURE+'::test_frozen_segment_policy_publishes_cross_page_originals_and_preview_without_media'
    group('跨页目录轮换合并与保留分区重命名',list(mapping),mapping,
          '轮换0999→0001并入已有两页成功场景，保留四原片、默认前三、两个精确LRF配对、实际页与幂等；preview-only和未知顺序按原输入保留并重命名。','merge_and_rename')
    additions=[]
    addition_contracts={
     CACHE:'同一读事务内复用；跨连接与变更关系拒绝；reference/attempt_range动态校验；候选页未完整提交前重新核验及回滚；commit/rollback/unknown_commit/cancel前清空；严格成员消费者跨页续读和重复读取保持页身份、未知文件完成与所有权证明，并复用三页的完整关系核验。',
     WATCH:'真实C host的error/abnormal/success/not_started及两种首次故障后重试成功；同一host后续提交和旧日志隔离；SQLite六动作状态按request_id与动作名限定。',
    }
    for file,contract in addition_contracts.items():
        text=source(repo,ref,file)
        nodes=[file+'::'+node for name in functions(text) if name.startswith('test_') for node,_ in literal_cases(text,name)]
        additions.append({'file':file,'count':len(nodes),'nodes':nodes,'contract':contract})
    expected=set()
    for node in by_id:
        current=replacements.get(node,node)
        if current is not None and not current.startswith('apps/camctl/tests/unit/'):
            expected.add(current)
    for addition in additions: expected.update(addition['nodes'])
    # 合并目标若原已存在，只减少被吸收节点；不能按各组unique目标简单相加。
    expected_counts=collections.Counter('camctl' if node.startswith('apps/camctl/tests/integration/') else 'node' if '.test.mjs::' in node else 'root' for node in expected)
    summary={'source_ref':ref,'before_ref':before,'basis':'既有有效CSV主实例+当前源文件静态变换；不是本次实际运行结果',
             'baseline_count':len(baseline),'baseline_outcomes':dict(collections.Counter(row['status'] for row in baseline)),
             'groups':groups,'additions':additions,'expected_total':len(expected),'expected_scopes':dict(sorted(expected_counts.items())),
             'expected_subchecks':len(rows(base/CSV_NAMES[2])),
             'skip_baseline':[row for row in baseline if row['status']=='skipped'],
             'deltas_note':'history目标和capture轮换被并入已存在节点，实际并集计数各减1。'}
    return summary,expected,by_id


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True);parser.add_argument('--ref',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--expected-total',type=int)
    args=parser.parse_args();summary,_,_=derive(args.repo,args.ref)
    if args.expected_total is not None:
        assert summary['expected_total']==args.expected_total,(summary['expected_total'],args.expected_total)
    import hashlib
    summary['input_sources']={
        'original_csv_source_commit':'9da49b20dcd1273074bb70b918b36ee4f1eb095c',
        'csv_files':[{'path':str(Path(TIMING)/name),'sha256':hashlib.sha256((args.repo/TIMING/name).read_bytes()).hexdigest()}
                     for name in CSV_NAMES],
        'source_to_before_test_code_changes':subprocess.check_output([
            'git','diff','--name-only','9da49b20..cd228b16','--',
            'apps/camctl/tests','tests/integration','apps/camctl/src','protocol'],cwd=args.repo,text=True).splitlines(),
    }
    summary['skip_baseline']=[{key:row[key] for key in ('suite','nodeid','reason')} for row in summary['skip_baseline']]
    data_dir=Path('docs/camctl/verification-data/2026-10-25-integration-optimization')
    summary['reproduction']={
        'working_directory':'仓库根目录',
        'interpreter':'Python 3.11；本次归档验证使用 CPython 3.11.17。',
        'command':['python3.11',str(data_dir/'derive_scope.py'),'--repo','.',
                   '--ref',args.ref,'--expected-total',str(summary['expected_total']),
                   '--output',str(data_dir/'final-expected-scope.json')],
        'result_boundary':'只从已登记输入推导范围；不读取实际测试结果，不代表通过。',
    }
    args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({key:summary[key] for key in ('baseline_count','expected_total','expected_scopes','expected_subchecks')},ensure_ascii=False))

if __name__=='__main__': main()
