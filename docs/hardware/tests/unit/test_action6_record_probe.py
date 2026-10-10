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


class CopyChecks(unittest.TestCase):
    def test_observed_growth_matches_later_source(self):
        digest = '6e48c0223643dced20c73922924698f005fc16770ebd6cd90c87a4c6ee0cfcb3'
        self.assertEqual(probe.assess_copy((64436061, digest), (64637726, digest), (64637726, digest)),
                         {'size_observation_changed': True, 'digest_observation_changed': False,
                          'matches_source_after': True})

    def test_unchanged_matching_copy(self):
        self.assertEqual(probe.assess_copy((10, 'a' * 64), (10, 'a' * 64), (10, 'a' * 64)),
                         {'size_observation_changed': False, 'digest_observation_changed': False,
                          'matches_source_after': True})

    def test_only_digest_changed(self):
        self.assertEqual(probe.assess_copy((10, 'a' * 64), (10, 'b' * 64), (10, 'b' * 64)),
                         {'size_observation_changed': False, 'digest_observation_changed': True,
                          'matches_source_after': True})

    def test_length_and_digest_both_changed(self):
        self.assertEqual(probe.assess_copy((10, 'a' * 64), (11, 'b' * 64), (11, 'b' * 64)),
                         {'size_observation_changed': True, 'digest_observation_changed': True,
                          'matches_source_after': True})

    def test_wrong_copy_length_is_rejected(self):
        self.assertFalse(probe.assess_copy((10, 'a' * 64), (10, 'a' * 64), (9, 'a' * 64))['matches_source_after'])

    def test_wrong_copy_digest_is_rejected(self):
        self.assertFalse(probe.assess_copy((10, 'a' * 64), (10, 'a' * 64), (10, 'b' * 64))['matches_source_after'])

    def test_copy_matches_earlier_but_not_later_digest(self):
        self.assertFalse(probe.assess_copy((10, 'a' * 64), (10, 'b' * 64), (10, 'a' * 64))['matches_source_after'])

    def test_copy_matches_earlier_but_not_later_length(self):
        self.assertFalse(probe.assess_copy((10, 'a' * 64), (11, 'a' * 64), (10, 'a' * 64))['matches_source_after'])


if __name__ == '__main__':
    unittest.main()
