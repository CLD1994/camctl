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


class TimelapseSettingsChecks(unittest.TestCase):
    def test_original_preset_is_preserved(self):
        self.assertEqual(probe.timelapse_payload(1800), '0400005000080700000000000000000000')

    def test_thirty_second_candidate_is_complete(self):
        payload = probe.timelapse_payload(30)
        self.assertEqual(payload, '04000050001e0000000000000000000000')
        self.assertEqual(len(bytes.fromhex(payload)), 17)

    def test_ten_second_candidate_is_complete(self):
        self.assertEqual(probe.timelapse_payload(10), '04000050000a0000000000000000000000')

    def test_invalid_durations_are_rejected(self):
        for value in (0, -1, 1801, True, 30.0, '30', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe.timelapse_payload(value)

    def test_auto_sequence_sets_timing_before_exposure_and_output(self):
        self.assertEqual(probe.timelapse_settings(30), (
            ('01-mode', 'dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02'),
            ('02-resolution', 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000'),
            ('03-timing', 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 04000050001e0000000000000000000000'),
            ('04-exposure', 'dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100'),
            ('05-output', 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 04000050001e0000000000000000000000'),
        ))


class CaptureChecks(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.commands = []
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def shell(self, label, command, **kwargs):
        self.commands.append(command)
        if label == '12-start':
            self.now += 2

    def test_timelapse_samples_1800_seconds_after_start_dispatch(self):
        result = probe.capture_once('timelapse', self.shell, clock=self.clock, sleep=self.sleep)
        self.assertEqual(self.now, 1900)
        self.assertEqual(result, {'start_call_elapsed_s': 2, 'start_to_observation_s': 1800})
        self.assertEqual(self.commands, ['dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01'])

    def test_blocking_timelapse_start_does_not_wait_another_30_minutes(self):
        def shell(label, command, **kwargs):
            self.commands.append(command)
            self.now += 1810
        result = probe.capture_once('timelapse', shell, clock=self.clock, sleep=self.sleep)
        self.assertEqual(self.sleeps, [])
        self.assertIsInstance(result, dict)
        self.assertEqual(result['start_to_observation_s'], 1810)
        self.assertEqual(len(self.commands), 1)

    def test_short_timelapse_samples_from_start_dispatch(self):
        for seconds in (30, 10):
            with self.subTest(seconds=seconds):
                self.setUp()
                result = probe.capture_once('timelapse', self.shell, duration_s=seconds,
                                            clock=self.clock, sleep=self.sleep)
                self.assertEqual(self.now, 100 + seconds)
                self.assertEqual(self.sleeps, [seconds - 2])
                self.assertEqual(result, {'start_call_elapsed_s': 2,
                                          'start_to_observation_s': seconds})
                self.assertEqual(self.commands, ['dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01'])

    def test_short_blocking_start_samples_without_extra_wait(self):
        def shell(label, command, **kwargs):
            self.commands.append(command)
            self.now += 31
        result = probe.capture_once('timelapse', shell, duration_s=30,
                                    clock=self.clock, sleep=self.sleep)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(result['start_to_observation_s'], 31)
        self.assertEqual(self.commands, ['dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01'])

    def test_invalid_timelapse_duration_does_not_start(self):
        with self.assertRaises(ValueError):
            probe.capture_once('timelapse', self.shell, duration_s=0,
                                clock=self.clock, sleep=self.sleep)
        self.assertEqual(self.commands, [])

    def test_timelapse_start_error_keeps_actual_error_and_unknown_activity(self):
        def shell(label, command, **kwargs):
            self.commands.append(command)
            raise RuntimeError('e3')
        with self.assertRaises(RuntimeError) as caught:
            probe.capture_once('timelapse', shell, clock=self.clock, sleep=self.sleep)
        self.assertIn('e3', str(caught.exception))
        self.assertIn('未知', str(caught.exception))
        self.assertEqual(self.commands, ['dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01'])

    def test_record_waits_ten_seconds_after_start_return_and_stops(self):
        probe.capture_once('record', self.shell, clock=self.clock, sleep=self.sleep)
        self.assertEqual(self.sleeps, [10])
        self.assertEqual(self.commands, ['dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 01',
                                         'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00'])

    def test_record_start_error_still_sends_stop(self):
        def shell(label, command, **kwargs):
            self.commands.append(command)
            if label == '12-start':
                raise RuntimeError('e3')
        with self.assertRaises(RuntimeError):
            probe.capture_once('record', shell, clock=self.clock, sleep=self.sleep)
        self.assertEqual(self.commands[-1], 'dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 02 00')


class DirectoryChecks(unittest.TestCase):
    def test_existing_empty_directory_is_distinct_from_absent(self):
        self.assertEqual(probe.parse_directory(b'', b'CAMCTL_FIND_EXIT=0\r\n'), ('directory', set()))

    def test_absent_directory_is_explicit(self):
        self.assertEqual(probe.parse_directory(b'', b'CAMCTL_STORAGE_ABSENT\r\n'), ('absent', set()))

    def test_directory_paths_are_kept(self):
        self.assertEqual(probe.parse_directory(b'/a\0/b\0', b'CAMCTL_FIND_EXIT=0\n'),
                         ('directory', {b'/a', b'/b'}))

    def test_unexpected_stderr_is_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.parse_directory(b'', b'CAMCTL_FIND_EXIT=1\r\n')

    def test_absent_marker_with_paths_is_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.parse_directory(b'/a\0', b'CAMCTL_STORAGE_ABSENT\n')

    def test_truncated_path_is_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.parse_directory(b'/a', b'CAMCTL_FIND_EXIT=0\n')

    def test_duplicate_path_is_rejected(self):
        with self.assertRaises(RuntimeError):
            probe.parse_directory(b'/a\0/a\0', b'CAMCTL_FIND_EXIT=0\n')


class SampleChecks(unittest.TestCase):
    def test_short_sample_preserves_requested_duration_and_unknown_device_facts(self):
        result = probe.assess_sample('timelapse', set(), {b'/new.mp4'}, duration_s=30)
        self.assertEqual(result['params'], {'resolution': '4k30', 'exposure': 'auto',
                                          'interval_s': 8, 'duration_s': 30, 'outputs': 'video'})
        self.assertEqual(result['preset_basis'], 'experimental_duration')
        for fact in ('actual_start', 'natural_end', 'file_write_complete', 'output_set_finalized'):
            self.assertEqual(result[fact], 'unknown')

    def test_no_new_paths_retains_unknown_device_facts(self):
        result = probe.assess_sample('timelapse', {b'/old.mp4'}, {b'/old.mp4'})
        self.assertEqual(result['sample_status'], 'no_new_mp4')
        self.assertEqual(result['new_files'], [])
        self.assertEqual(result['observed_mp4_files'], [])
        self.assertEqual(result['video_checks'], 'not_attempted')
        for fact in ('actual_start', 'natural_end', 'file_write_complete', 'output_set_finalized'):
            self.assertEqual(result[fact], 'unknown')

    def test_new_non_video_paths_are_retained(self):
        result = probe.assess_sample('timelapse', set(), {b'/new.LRF', b'/new.trinf'})
        self.assertEqual(result['new_files'], ['/new.LRF', '/new.trinf'])
        self.assertEqual(result['sample_status'], 'no_new_mp4')
        self.assertEqual(result['video_checks'], 'not_attempted')

    def test_new_mp4_is_observed_without_claiming_copy_checks(self):
        result = probe.assess_sample('timelapse', {b'/old.mp4'}, {b'/old.mp4', b'/new.MP4'})
        self.assertEqual(result['new_files'], ['/new.MP4'])
        self.assertEqual(result['observed_mp4_files'], ['/new.MP4'])
        self.assertEqual(result['sample_status'], 'mp4_observed')
        self.assertEqual(result['videos'], [])
        self.assertEqual(result['video_checks'], 'incomplete')
        self.assertEqual(result['natural_end'], 'unknown')
        self.assertEqual(result['file_write_complete'], 'unknown')
        self.assertEqual(result['output_set_finalized'], 'unknown')

    def test_paths_from_both_storage_scopes_are_ordered(self):
        result = probe.assess_sample('timelapse', set(), {
            b'/mnt/media_rw/sd/DCIM/b.mp4', b'/mnt/media_rw/emulated/DCIM/a.mp4',
            b'/mnt/media_rw/sd/DCIM/b.LRF',
        })
        self.assertEqual(result['new_files'], [
            '/mnt/media_rw/emulated/DCIM/a.mp4', '/mnt/media_rw/sd/DCIM/b.LRF',
            '/mnt/media_rw/sd/DCIM/b.mp4',
        ])
        self.assertEqual(result['observed_mp4_files'], [
            '/mnt/media_rw/emulated/DCIM/a.mp4', '/mnt/media_rw/sd/DCIM/b.mp4',
        ])

    def test_recording_keeps_unconfirmed_setting_facts(self):
        result = probe.assess_sample('record', set(), {b'/new.mp4'})
        self.assertEqual(result['aperture'], 'unconfirmed')
        self.assertEqual(result['bitrate_effect'], 'unconfirmed')
        self.assertEqual(result['capture'], 'record')


if __name__ == '__main__':
    unittest.main()
