import functools, inspect, json, os, platform, sqlite3, sys, time
from pathlib import Path
import pytest
records=[]
phases=[]
started=time.perf_counter()

def wrap(owner,name,label):
    original=owner[name] if isinstance(owner,dict) else getattr(owner,name)
    def details(args,kwargs):
        return {'count':kwargs.get('count'), 'batch_size':kwargs.get('batch_size')}
    def save(start,cpu,args,kwargs):
        records.append({'name':label,'start_s':start-started,'wall_s':time.perf_counter()-start,'parent_cpu_s':time.process_time()-cpu,**details(args,kwargs)})
    if inspect.iscoroutinefunction(original):
        @functools.wraps(original)
        async def measured(*args,**kwargs):
            start=time.perf_counter();cpu=time.process_time()
            try:return await original(*args,**kwargs)
            finally:save(start,cpu,args,kwargs)
    else:
        @functools.wraps(original)
        def measured(*args,**kwargs):
            start=time.perf_counter();cpu=time.process_time()
            try:return original(*args,**kwargs)
            finally:save(start,cpu,args,kwargs)
    if isinstance(owner,dict):owner[name]=measured
    else:setattr(owner,name,measured)

def pytest_collection_modifyitems(items):
    module=sys.modules[items[0].function.__module__]
    wrap(module,'_ready_world','prepare_complete_world')
    wrap(module,'_world','prepare_source_files')
    wrap(module.transactions,'finish_segment_recording','finish_segment_recording')
    wrap(module.transactions,'verify_segment_finish','verify_segment_finish')
    import gc
    wrap(gc,'collect','gc_collect')

def pytest_runtest_logreport(report):
    phases.append({'nodeid':report.nodeid,'phase':report.when,'wall_s':report.duration,'outcome':report.outcome})

def pytest_sessionfinish(session,exitstatus):
    import camctl
    result={'source_head':os.environ['CAMCTL_TIMING_HEAD'],'command':sys.argv,'cwd':os.getcwd(),'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'camctl_file':camctl.__file__,'exit_code':int(exitstatus),'plugin_wall_s':time.perf_counter()-started,'phases':phases,'spans':records,'scope':'single existing capacity test; external observational wrappers; nested spans must not be added'}
    Path(os.environ['CAMCTL_TIMING_RESULT']).write_text(json.dumps(result,indent=2)+'\n')
