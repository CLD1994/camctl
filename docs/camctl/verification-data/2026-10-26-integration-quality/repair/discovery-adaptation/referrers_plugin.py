import gc

def pytest_exception_interact(node, call, report):
    for entry in call.excinfo.traceback:
        frame = entry.frame.f_locals
        references = frame.get("references")
        if references is None:
            single = frame.get("reference")
            references = (single,) if single is not None else ()
        for reference in references[:1]:
            value = reference()
            if value is None:
                continue
            level = [value]
            seen = {id(value)}
            for depth in range(4):
                next_level=[]
                for current in level:
                    for ref in gc.get_referrers(current):
                        if id(ref) in seen or isinstance(ref, (dict,list)):
                            continue
                        seen.add(id(ref))
                        print("REFERENCE", depth, type(current).__name__, "<-", type(ref).__name__, repr(ref)[:160])
                        next_level.append(ref)
                level=next_level
