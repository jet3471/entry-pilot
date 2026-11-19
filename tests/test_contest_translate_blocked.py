"""Claude safeguard refusals in the translate phase : Codex gets one try, then the text is parked
(no retry until it changes) and the user is told once through the app's notice outbox.

Run: python run_tests.py
No model call, no notice and no network access happens: the runners and cp.notify.send are fakes.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cp import dossier_ko, notify, store, translate  # noqa: E402

DOSSIER = "# C900 - Test contest\n\n## Contest\nFree theme\n\n## Summary\nA plain summary.\n"
FLAG = "API Error: Haiku 5.5's safeguards flagged this message. Details: [cyber]"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp-tblock-test-")
        self.saved = os.environ.get("CP_DATA_DIR")
        os.environ["CP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.makedirs(os.environ["CP_DATA_DIR"])
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.saved is None:
            os.environ.pop("CP_DATA_DIR", None)
        else:
            os.environ["CP_DATA_DIR"] = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


class SafeguardDetection(Base):
    def test_flag_in_stdout_raises_safeguard(self):
        done = subprocess.CompletedProcess([], 1, FLAG.encode(), b"")
        with mock.patch.object(translate.subprocess, "run", return_value=done), \
                mock.patch.object(translate, "child_env", return_value={}):
            with self.assertRaises(translate.SafeguardBlocked):
                translate.run_model("x")

    def test_other_failure_is_plain_model_error(self):
        done = subprocess.CompletedProcess([], 1, b"", b"boom")
        with mock.patch.object(translate.subprocess, "run", return_value=done), \
                mock.patch.object(translate, "child_env", return_value={}):
            with self.assertRaises(translate.ModelError) as cm:
                translate.run_model("x")
        self.assertNotIsInstance(cm.exception, translate.SafeguardBlocked)

    def test_codex_command_is_read_only(self):
        cmd = translate.codex_command("p", "out.txt")
        self.assertIn("exec", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertEqual(cmd[cmd.index("-o") + 1], "out.txt")


class Fallback(Base):
    def test_codex_answers_after_a_refusal(self):
        ko = DOSSIER.replace("Free theme", "자유 주제").replace("A plain summary.", "평범한 요약.")
        with mock.patch.object(translate, "run_model", side_effect=translate.SafeguardBlocked("flagged")), \
                mock.patch.object(translate, "run_codex", return_value=ko) as codex:
            self.assertEqual(translate.translate_long(DOSSIER), ko)
        self.assertEqual(codex.call_count, 1)
        self.assertEqual(translate.long_cached(DOSSIER), ko)

    def test_no_codex_for_other_failures(self):
        with mock.patch.object(translate, "run_model", side_effect=translate.ModelError("down")), \
                mock.patch.object(translate, "run_codex") as codex:
            with self.assertRaises(translate.ModelError) as cm:
                translate.translate_long(DOSSIER)
        self.assertNotIsInstance(cm.exception, translate.Blocked)
        codex.assert_not_called()

    def test_bad_codex_reply_counts_as_blocked(self):
        with mock.patch.object(translate, "run_model", side_effect=translate.SafeguardBlocked("flagged")), \
                mock.patch.object(translate, "run_codex", return_value="too short"):
            with self.assertRaises(translate.Blocked):
                translate.translate_long(DOSSIER)


class Parking(Base):
    ITEM = {"id": "C900", "state": "proposal", "work": {}}

    def run_dossiers(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            translate.main(["translate", "--dossiers"])
        return json.loads(buf.getvalue())

    def test_parked_once_told_once_not_retried(self):
        sent = []

        def fake_send(db, subject, text):
            sent.append((subject, text))
            return {"sent": ["telegram", "email"], "failed": {}}

        model = mock.Mock(side_effect=translate.SafeguardBlocked("flagged"))
        codex = mock.Mock(side_effect=translate.ModelError("codex down"))
        with mock.patch.object(dossier_ko.store, "items", return_value=[dict(self.ITEM)]), \
                mock.patch.object(dossier_ko.analysis, "read_text", return_value=DOSSIER), \
                mock.patch.object(translate, "run_model", model), mock.patch.object(translate, "run_codex", codex), \
                mock.patch.object(notify, "send", side_effect=fake_send):
            r1 = self.run_dossiers()
            calls_after_first = model.call_count
            r2 = self.run_dossiers()
        self.assertEqual(r1["pending"], 3)  # translate, schema, brief of the one dossier
        self.assertTrue(all(": parked: " in f for f in r1["failed"]))
        self.assertEqual(calls_after_first, 3)
        self.assertEqual(codex.call_count, 3)
        self.assertEqual(r2["pending"], 0)
        self.assertEqual(model.call_count, 3)  # nothing sent again
        self.assertEqual(len(sent), 1)
        self.assertIn("C900", sent[0][1])
        db = store.connect()
        try:
            msgs = [m[0] for m in db.execute("SELECT message FROM errors")]
        finally:
            db.close()
        self.assertTrue(any("blocked" in m for m in msgs))

    def test_failed_notice_is_retried_and_recorded(self):
        results = [{"sent": [], "failed": {"telegram": "exit 1: x"}}, {"sent": ["telegram"], "failed": {}}]
        with mock.patch.object(dossier_ko.store, "items", return_value=[dict(self.ITEM)]), \
                mock.patch.object(dossier_ko.analysis, "read_text", return_value=DOSSIER), \
                mock.patch.object(translate, "run_model", side_effect=translate.SafeguardBlocked("flagged")), \
                mock.patch.object(translate, "run_codex", side_effect=translate.ModelError("codex down")), \
                mock.patch.object(notify, "send", side_effect=results) as send:
            self.run_dossiers()
            self.run_dossiers()
            self.run_dossiers()
        self.assertEqual(send.call_count, 2)
        db = store.connect()
        try:
            msgs = [m[0] for m in db.execute("SELECT message FROM errors")]
        finally:
            db.close()
        self.assertTrue(any("translate blocked notice on telegram failed" in m for m in msgs))

    def test_changed_text_is_tried_again(self):
        translate.park("translate", DOSSIER, "C900", "x")
        self.assertTrue(translate.is_parked("translate", DOSSIER))
        self.assertFalse(translate.is_parked("translate", DOSSIER + "\nnew line"))
        self.assertFalse(translate.is_parked("schema", DOSSIER))


KO_DOSSIER = ("# C901 - 테스트 공모전\n\n## 공모전 (Contest)\n자유 주제, https://example.com/notice\n\n"
              "## 요약 (Summary)\n평범한 요약입니다.\n")


class KoreanDossier(Base):
    def test_korean_dossier_is_not_translated(self):
        from cp import analysis
        self.assertTrue(analysis.written_in_korean(KO_DOSSIER))
        self.assertFalse(analysis.written_in_korean(DOSSIER))
        item = {"id": "C901", "state": "proposal", "work": {}}
        with mock.patch.object(dossier_ko.store, "items", return_value=[item]), \
                mock.patch.object(dossier_ko.analysis, "read_text", return_value=KO_DOSSIER):
            kinds = [k for _, _, k in dossier_ko.pending_long()]
            self.assertEqual(dossier_ko.korean_text(item), (KO_DOSSIER, True))
        self.assertNotIn("translate", kinds)
        self.assertEqual(sorted(kinds), ["brief", "schema"])

    def test_heading_shown_without_the_english_keyword(self):
        from cp import analysis
        self.assertIn("## 공모전\n", analysis.plain_headings(KO_DOSSIER))
        self.assertNotIn("(Contest)", analysis.plain_headings(KO_DOSSIER))


if __name__ == "__main__":
    unittest.main()
