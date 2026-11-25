import unittest
from monitor_core import BoxInfo, NewBoxTracker


def b(idx, key):
    return BoxInfo(idx, key, "盒子", "可點")


class MonitorTests(unittest.TestCase):
    def test_first_scan_only_creates_baseline(self):
        t = NewBoxTracker()
        v = t.observe([b(1, "A"), b(2, "B")], 1)
        self.assertTrue(v.baseline_created)
        self.assertFalse(v.should_click)
        self.assertEqual(t.seen, {"A", "B"})

    def test_new_card_at_selected_position(self):
        t = NewBoxTracker()
        t.observe([b(1, "A"), b(2, "B")], 1)
        v = t.observe([b(1, "C"), b(2, "A"), b(3, "B")], 1)
        self.assertEqual(v.new_identities, ["C"])
        self.assertTrue(v.should_click)
        t.clicked("C", True)
        self.assertFalse(t.observe([b(1, "C"), b(2, "A"), b(3, "B")], 1).should_click)

    def test_new_non_target_ignored(self):
        t = NewBoxTracker()
        t.observe([b(1, "A")], 1)
        result = t.observe([b(1, "A"), b(2, "C")], 1)
        self.assertEqual(result.new_identities, ["C"])
        self.assertFalse(result.should_click)

    def test_empty_page_does_not_reset_baseline(self):
        t = NewBoxTracker()
        t.observe([b(1, "A")], 1)
        t.observe([], 1)
        self.assertFalse(t.observe([b(1, "A")], 1).should_click)

    def test_retry_limited(self):
        t = NewBoxTracker(max_retries=2)
        t.observe([b(1, "A")], 1)
        self.assertTrue(t.observe([b(1, "B")], 1).should_click)
        self.assertFalse(t.clicked("B", False))
        self.assertEqual(t.observe([b(1, "B")], 1).retry_number, 2)
        self.assertTrue(t.clicked("B", False))
        self.assertFalse(t.observe([b(1, "B")], 1).should_click)

    def test_duplicate_identity_does_not_click(self):
        t = NewBoxTracker()
        t.observe([b(1, "A")], 1)
        v = t.observe([b(1, "B"), b(2, "B")], 1)
        self.assertTrue(v.duplicate_selected_id)
        self.assertFalse(v.should_click)

    def test_no_first_page_boxes_keeps_waiting(self):
        t = NewBoxTracker()
        self.assertFalse(t.observe([], 1).baseline_created)
        self.assertTrue(t.observe([b(1, "A")], 1).baseline_created)


if __name__ == "__main__":
    unittest.main()
