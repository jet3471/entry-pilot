"""ep.scout_run: candidates are marked as seen only after the judge wrote its result file.

The crawler and the agent are replaced by fakes: no network, no model call. Data goes to a fresh temp EP_DATA_DIR.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ep import config, scout_run

CANDS = [{"source_url": "https://a.example/1", "title": "first"}, {"source_url": "https://a.example/2", "title": "second"}]


class FakeProc:
    def __init__(self, out, write, code):
        self.stdin = mock.Mock()
        self.out, self.write, self.code = out, write, code

    def wait(self):
        if self.write:
            with open(self.out, "w", encoding="utf-8") as f:
                f.write('{"title": "first"}\n')
        return self.code


class TestScoutRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-scoutrun-")
        self.saved = os.environ.get("EP_DATA_DIR")
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.makedirs(os.environ["EP_DATA_DIR"])
        self.assertNotEqual(os.path.normcase(config.data_dir()), os.path.normcase(config.DEFAULT_DATA_DIR))
        self.out = os.path.join(self.tmp, "20261114-000000-codex-KR.jsonl")
        self.known = os.path.join(self.tmp, "known.txt")
        self.seen = os.path.join(config.data_dir(), "crawler-seen.tsv")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("EP_DATA_DIR", None)
        else:
            os.environ["EP_DATA_DIR"] = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, cands, write, code):
        real_run = scout_run.subprocess.run
        real_popen = scout_run.subprocess.Popen

        def fake_crawler(cmd, **kw):
            if "--out" not in cmd:
                return real_run(cmd, **kw)
            with open(cmd[cmd.index("--out") + 1], "w", encoding="utf-8") as f:
                json.dump(cands, f)
            return mock.Mock(returncode=0, stderr="")

        def fake_popen(cmd, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and os.path.basename(str(cmd[0])).lower() in ("icacls.exe", "whoami.exe"):
                return real_popen(cmd, **kw)
            return FakeProc(self.out, write, code)

        with mock.patch.object(scout_run.subprocess, "run", fake_crawler), \
                mock.patch.object(scout_run.subprocess, "Popen", fake_popen), \
                mock.patch.object(scout_run.sys, "stdout", mock.Mock()), mock.patch("builtins.print"):
            return scout_run.main(["--agent", "codex", "--out", self.out, "--known", self.known])

    def seen_lines(self):
        if not os.path.exists(self.seen):
            return []
        with open(self.seen, encoding="utf-8") as f:
            return [x for x in f.read().splitlines() if x]

    def test_judge_wrote_its_file_candidates_are_marked_seen(self):
        self.assertEqual(self.run_main(CANDS, write=True, code=0), 0)
        self.assertEqual(self.seen_lines(), ["https://a.example/1\tfirst", "https://a.example/2\tsecond"])

    def test_judge_without_a_file_is_a_failure_and_marks_nothing(self):
        self.assertEqual(self.run_main(CANDS, write=False, code=0), 4)  # exit 0 without a file (429 case)
        self.assertEqual(self.seen_lines(), [])
        self.assertEqual(self.run_main(CANDS, write=False, code=1), 1)  # the agent's own failure code is kept
        self.assertEqual(self.seen_lines(), [])

    def test_no_candidates_writes_an_empty_file_without_a_model_call(self):
        self.assertEqual(self.run_main([], write=False, code=9), 0)
        self.assertTrue(os.path.exists(self.out))
        self.assertEqual(self.seen_lines(), [])


if __name__ == "__main__":
    unittest.main()
