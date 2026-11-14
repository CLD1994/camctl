"""普通业务等待不代表下载准备尚未收场。"""


def test_ordinary_business_waiting_is_not_a_preparation_error():
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.outputs.read_attempts import require_preparation_settled
    assert require_preparation_settled(InputStep(InputPhase.WAITING)) is True
