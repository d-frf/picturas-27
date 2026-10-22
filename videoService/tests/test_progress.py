"""Regression checks for FFmpeg progress values before timestamps are available."""
import unittest

from jobs.runner import _progress_percent


class ProgressTests(unittest.TestCase):
    def test_unavailable_timestamp_does_not_fail_processing(self):
        for raw in ("N/A", "", None, "not-a-timestamp"):
            with self.subTest(raw=raw):
                self.assertIsNone(_progress_percent(raw, 10))

    def test_non_finite_timestamp_is_ignored(self):
        for raw in ("NaN", "inf", "-inf"):
            with self.subTest(raw=raw):
                self.assertIsNone(_progress_percent(raw, 10))

    def test_microseconds_are_converted_to_percentage(self):
        self.assertEqual(_progress_percent("5000000", 10), 50)
        self.assertEqual(_progress_percent("0", 10), 0)

    def test_running_progress_stays_between_zero_and_ninety_nine(self):
        self.assertEqual(_progress_percent("-1000000", 10), 0)
        self.assertEqual(_progress_percent("12000000", 10), 99)


if __name__ == "__main__":
    unittest.main(verbosity=2)
