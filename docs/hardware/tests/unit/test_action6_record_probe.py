import importlib.util
from pathlib import Path
import unittest

script = Path(__file__).resolve().parents[2] / 'action6_record_probe.py'
spec = importlib.util.spec_from_file_location('record_probe', script)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ReplyChecks(unittest.TestCase):
    def test_observed_ack_is_accepted(self):
        probe.expect_ack(b'log 00\r\nResp message, len = 1, data:\r\n  00 \r\n')

    def test_unknown_response_reports_actual_byte(self):
        with self.assertRaises(RuntimeError) as caught:
            probe.expect_ack(b'Resp message, len = 1, data:\r\n  e3 \r\n')
        self.assertIn('e3', str(caught.exception))

    def test_other_logs_do_not_replace_response(self):
        with self.assertRaises(RuntimeError):
            probe.expect_ack(b'Send message, data:\r\n  00 \r\n')

    def test_truncated_response_is_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.expect_ack(b'Resp message, len = 1, data:\r\n')

    def test_declared_length_is_checked(self):
        with self.assertRaises(RuntimeError):
            probe.expect_ack(b'Resp message, len = 2, data:\r\n  00 \r\n')

    def test_extra_payload_is_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.expect_ack(b'Resp message, len = 1, data:\r\n  00 e3 \r\n')

    def test_multiple_responses_are_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.expect_ack(b'Resp message, len = 1, data:\n  00\nResp message, len = 1, data:\n  e3\n')

    def test_malformed_hex_is_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.expect_ack(b'Resp message, len = 1, data:\n  0\n')

    def test_unrecognized_response_suffix_is_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.expect_ack(b'Resp message, len = 1, data:\n  00\nUNKNOWN\n')


if __name__ == '__main__':
    unittest.main()
