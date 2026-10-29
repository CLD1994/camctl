"""Narrow ownership diagnosis using real segment save/resume functions and held DB responses."""
import asyncio
import json
from types import SimpleNamespace
from camctl.capture.segment_completion import _save, resume_segment_writes
from camctl.persistence.models import DbOutcome, DbOutcomeKind

class HeldDatabase:
    def __init__(self): self.calls=[]
    async def write(self, callback, *, key, description):
        future=asyncio.get_running_loop().create_future()
        self.calls.append((key,future))
        return await future

async def main():
    database=HeldDatabase()
    runtime=SimpleNamespace(database=database,pending_segment_writes={})
    original=asyncio.create_task(_save(runtime,1,object(),'save_file_presence','original-command'))
    while len(database.calls)<1: await asyncio.sleep(0)
    resumed=asyncio.create_task(resume_segment_writes(database,runtime.pending_segment_writes,1))
    while len(database.calls)<2: await asyncio.sleep(0)
    keys=[str(key) for key,_ in database.calls]
    for _,future in database.calls:
        future.set_result(DbOutcome(DbOutcomeKind.COMPLETED,value='original-response'))
    result=await asyncio.gather(original,resumed,return_exceptions=True)
    print(json.dumps({'same_key_submitted_concurrently':keys[0]==keys[1], 'db_submission_count':len(keys), 'actual_database_worker':False,'results':[{'kind':type(item).__name__,'value':str(item)} for item in result], 'remaining_pending':list(runtime.pending_segment_writes)}))

asyncio.run(main())
