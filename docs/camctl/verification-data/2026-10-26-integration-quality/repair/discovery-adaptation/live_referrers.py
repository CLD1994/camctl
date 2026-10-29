import gc, inspect, sys

def diagnose(reference):
    value=reference()
    print("LIVE CANDIDATE", repr(value), flush=True)
    def chain(current, depth, ancestors):
        if depth > 6:
            return
        holders=gc.get_referrers(current)
        for holder in holders:
            if id(holder) in ancestors or holder is holders:
                continue
            if isinstance(holder, list):
                continue
            if inspect.isframe(holder) and holder.f_code.co_filename == __file__:
                continue
            if isinstance(holder, dict):
                matched=[str(k) for k,v in holder.items() if v is current]
                print("LIVE REF", depth, type(current).__name__, "<- dict", matched[:8], flush=True)
                continue
            detail = (holder.f_code.co_name if inspect.isframe(holder) else repr(holder)[:180])
            print("LIVE REF", depth, type(current).__name__, "<-", type(holder).__name__, detail, flush=True)
            chain(holder, depth+1, ancestors | {id(holder)})
    chain(value, 0, {id(value)})
    for frame in sys._current_frames().values():
        while frame is not None:
            if frame.f_code.co_filename.endswith("/persistence/executor.py"):
                print("WORKER FRAME", frame.f_code.co_name, {k:type(v).__name__ for k,v in frame.f_locals.items()}, flush=True)
            frame=frame.f_back
