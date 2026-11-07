import unittest

from bot.permissions import user_matches_session, require_authorized_role


class SessionPermissionTests(unittest.TestCase):
    def test_wearer_match(self):
        result = user_matches_session("wearer-1", {"wearerid": "wearer-1", "holderid": "holder-1"})
        self.assertTrue(result["wearer"])
        self.assertFalse(result["keyholder"])

    def test_holder_match(self):
        result = user_matches_session("holder-1", {"wearerid": "wearer-1", "holderid": "holder-1"})
        self.assertFalse(result["wearer"])
        self.assertTrue(result["keyholder"])

    def test_subtract_requires_holder(self):
        with self.assertRaises(PermissionError):
            require_authorized_role(is_wearer=True, is_holder=False, operation="subtract")

    def test_add_requires_relationship(self):
        with self.assertRaises(PermissionError):
            require_authorized_role(is_wearer=False, is_holder=False, operation="add")


if __name__ == "__main__":
    unittest.main()
