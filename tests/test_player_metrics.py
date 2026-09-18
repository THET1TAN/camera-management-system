import unittest

from player_metrics import BitrateAverage
from player_supervisor import PlayerSettings, Progress


class BitrateAverageTests(unittest.TestCase):
    def test_half_second_packet_bursts_do_not_replace_the_one_second_rate(self):
        meter = BitrateAverage()
        self.assertEqual(meter.observe(0, 100_000_000), 0)
        for second in range(1, 8):
            self.assertEqual(meter.observe(second, 100_000_000 + second * 1_000_000), 8)
            self.assertEqual(meter.observe(second + .5, 100_000_000 + second * 1_000_000), 8)

    def test_average_uses_last_five_complete_measurements(self):
        meter = BitrateAverage()
        received = 0
        meter.observe(0, received)
        for second in range(1, 7):
            received += second * 125_000
            actual = meter.observe(second, received)
            rates = list(range(max(1, second - 4), second + 1))
            self.assertAlmostEqual(actual, sum(rates) / len(rates))
        self.assertEqual(actual, 4)

    def test_actual_elapsed_time_is_used_when_sampling_is_delayed(self):
        meter = BitrateAverage()
        meter.observe(10, 3_000_000)
        self.assertEqual(meter.observe(11.5, 3_750_000), 4)
        self.assertEqual(meter.observe(12, 3_750_000), 4)
        self.assertEqual(meter.observe(12.5, 4_250_000), 4)

    def test_counter_reset_and_new_session_do_not_reuse_history(self):
        meter = BitrateAverage()
        meter.observe(0, 1_000_000)
        self.assertEqual(meter.observe(1, 2_000_000), 8)
        self.assertEqual(meter.observe(1.5, 0), 0)
        self.assertEqual(meter.observe(2.5, 250_000), 2)
        replacement = BitrateAverage()
        self.assertEqual(replacement.observe(20, 90_000_000), 0)
        self.assertEqual(replacement.observe(21, 90_125_000), 1)

    def test_missing_stats_expire_and_restart_with_a_fresh_baseline(self):
        meter = BitrateAverage()
        meter.observe(0, 0)
        self.assertEqual(meter.observe(1, 125_000), 1)
        self.assertEqual(meter.observe(1.5, None), 1)
        self.assertEqual(meter.observe(4, None), 1)
        self.assertEqual(meter.observe(4.5, None), 0)
        self.assertEqual(meter.observe(5, 900_000), 0)
        self.assertEqual(meter.observe(6, 1_025_000), 1)

    def test_silence_decays_to_zero_and_legacy_100_mbps_limit_remains(self):
        meter = BitrateAverage()
        meter.observe(0, 0)
        self.assertEqual(meter.observe(1, 25_000_000), 100)
        for second in range(2, 7):
            rate = meter.observe(second, 25_000_000)
        self.assertEqual(rate, 0)

    def test_smoothed_bitrate_does_not_hide_stalled_video_from_recovery(self):
        progress = Progress(0, PlayerSettings(stall_timeout=2))
        progress.observe({'received': 0, 'decoded': 0, 'displayed': 0, 'bitrate': 8}, 0)
        progress.observe({'received': 100, 'decoded': 1, 'displayed': 1, 'bitrate': 8}, 1)
        progress.observe({'received': 100, 'decoded': 1, 'displayed': 1, 'bitrate': 8}, 3)
        self.assertEqual(progress.failure(3), 'no-input-progress')


if __name__ == '__main__':
    unittest.main()
