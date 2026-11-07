import unittest

from emlalock.time_parser import parse_duration


class DurationParserTests(unittest.TestCase):
    def test_single_unit(self):
        self.assertEqual(parse_duration("2 hours"), 7200)

    def test_multiple_units(self):
        self.assertEqual(parse_duration("1 day and 30 minutes"), 88200)

    def test_short_units(self):
        self.assertEqual(parse_duration("2w 3d 4h 5m 6s"), 1483506)

    def test_rejects_months(self):
        with self.assertRaises(ValueError):
            parse_duration("1 month")

    def test_rejects_missing_unit(self):
        with self.assertRaises(ValueError):
            parse_duration("30")

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            parse_duration("0 hours")


if __name__ == "__main__":
    unittest.main()
