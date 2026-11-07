"""在共同编码器故障时保留原异常及编码错误。"""
import traceback


def _fallback(error):
    seen = {}
    def convert(item, path):
        if item is None:
            return None
        if id(item) in seen:
            return {'$ref': seen[id(item)]}
        seen[id(item)] = path
        result = {'type': f'{type(item).__module__}.{type(item).__qualname__}',
                  'message': str(item), 'arguments': repr(item.args),
                  'traceback': [{'filename': frame.f_code.co_filename, 'lineno': line,
                                 'function': frame.f_code.co_name}
                                for frame, line in traceback.walk_tb(item.__traceback__)],
                  'suppress_context': item.__suppress_context__}
        if isinstance(item, OSError):
            result.update(errno=item.errno, filename=item.filename, filename2=item.filename2)
        if hasattr(item, '__notes__'):
            result['notes'] = list(item.__notes__)
        result['cause'] = convert(item.__cause__, path + '/cause')
        result['context'] = convert(item.__context__, path + '/context')
        if isinstance(item, BaseExceptionGroup):
            result['exceptions'] = [convert(child, path + '/exceptions/' + str(index))
                                    for index, child in enumerate(item.exceptions)]
        return result
    return convert(error, '#')


def freeze(error, stage, encoder):
    try:
        diagnostic = encoder(error)
    except Exception as encoder_error:
        diagnostic = _fallback(error)
        diagnostic['encoder_error'] = _fallback(encoder_error)
    diagnostic['stage'] = stage
    return diagnostic
