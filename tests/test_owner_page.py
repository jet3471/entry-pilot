"""Owner page tests (ep/owner_page.py). Run: python -m unittest discover -s tests -v. Temp data dir only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_core import Base, ep  # noqa: E402  (sets the temp EP_DATA_DIR before ep is imported)

from ep import owner_page  # noqa: E402


class TestOwnerPage(Base):
    def make_failed_ig(self, url):
        iid = self.ready(lane="browser", url=url)
        self.assertTrue(ep("claim", "--agent", "codex", "--id", iid)["ok"])
        self.assertTrue(ep("manual", iid, "--agent", "codex", "--need", "upload", "--reason", "photo needed")["ok"])
        return iid

    def test_candidates_and_page(self):
        iid = self.make_failed_ig("https://example.com/p/AAA/")
        other = self.ready(lane="form", url="https://forms.gle/zzz")
        owner_page.translate_pending = lambda texts: None  # no model call in tests
        html = owner_page.build_page()
        self.assertIn(iid, html)
        self.assertNotIn(other, html)

    def test_done_skip_submit(self):
        a = self.make_failed_ig("https://example.com/p/BBB/")
        self.assertEqual(owner_page.apply_act(a, "done")["ok"], True)
        self.assertEqual(self.item(a)["state"], "submitted")
        self.assertEqual(self.item(a)["submitted_by"], "owner")
        b = self.make_failed_ig("https://example.com/p/CCC/")
        self.assertEqual(owner_page.apply_act(b, "skip")["ok"], True)
        self.assertEqual(self.item(b)["state"], "excluded")
        c = self.make_failed_ig("https://example.com/p/DDD/")
        up = owner_page.save_upload(c, "shot.png", b"\x89PNG1")
        self.assertTrue(up["ok"], up)
        r = owner_page.apply_act(c, "submit")
        self.assertTrue(r["ok"], r)
        it = self.item(c)
        self.assertEqual(it["state"], "new")
        self.assertIn(f"{c}_owner_1.png", it["verify_notes"])
        self.assertIn("the user did the manual steps", it["verify_notes"])

    def test_upload_rules(self):
        self.assertFalse(owner_page.save_upload("X1", "a.png", b"x")["ok"])
        self.assertFalse(owner_page.save_upload("E1", "a.heic", b"x")["ok"])
        self.assertFalse(owner_page.save_upload("E1", "a.png", b"")["ok"])
        r1 = owner_page.save_upload("E1", "a.jpg", b"x")
        r2 = owner_page.save_upload("E1", "b.png", b"y")
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertTrue(r1["path"].endswith("E1_owner_1.jpg") and r2["path"].endswith("E1_owner_2.png"))
        self.assertEqual(len(owner_page.owner_files("E1")), 2)
        self.assertFalse(owner_page.apply_act("E1", "nope")["ok"])

    def test_http_server_removed_and_safe_url(self):
        for attr in ("serve", "Handler", "prepare", "main"):
            self.assertFalse(hasattr(owner_page, attr), attr)
        self.assertFalse(ep("owner-page", "serve")["ok"])
        iid = self.make_failed_ig("javascript:alert(1)")
        owner_page.translate_pending = lambda texts: None
        html = owner_page.build_page()
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn("<a ", html)
        self.assertIn(iid, html)


if __name__ == "__main__":
    unittest.main()
