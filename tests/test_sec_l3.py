"""Security tests for items 13 (owner identity hashes & clean build bundle) and 15 (Excel formula injection).

unittest style; temp directories only; no network, no Chrome, no AI CLI, no PyInstaller build execution.
"""
import ast
import hashlib
import importlib.util
import io
import marshal
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from datetime import timedelta
from unittest import mock

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_build_module():
    spec = importlib.util.spec_from_file_location("ep_build_mod", os.path.join(ROOT, "build", "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_spec_helpers():
    """Load helper definitions from build/entrypilot.spec without running Analysis/PYZ/COLLECT."""
    spec_path = os.path.join(ROOT, "build", "entrypilot.spec")
    with open(spec_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename="entrypilot.spec")
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            keep.append(node)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names == ["SKIP_DATA_EXTS"]:
                keep.append(node)
    mod_ast = ast.Module(body=keep, type_ignores=[])
    ns = {}
    exec(compile(mod_ast, "entrypilot.spec", "exec"), ns)
    return ns


class OwnerIdentityScanTests(unittest.TestCase):
    def setUp(self):
        from tests import test_contest_app
        self.mod = test_contest_app
        self.priv = test_contest_app.Private()

    def test_bad_list_contains_only_sha256_digests(self):
        self.assertEqual(len(self.priv.BAD), 20)
        for entry in self.priv.BAD:
            self.assertRegex(entry, r"^[0-9a-f]{64}$")

    def test_no_plain_identifiers_in_owned_or_scanned_files(self):
        targets = list(self.priv.files()) + [
            os.path.join(ROOT, "tests", "test_contest_app.py"),
            os.path.join(ROOT, "build", "entrypilot.spec"),
            os.path.join(ROOT, "build", "build.py"),
            os.path.join(ROOT, "ep", "render.py"),
            os.path.abspath(__file__),
        ]
        hits = []
        for p in targets:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for tok in self.priv.tokens(text):
                digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()
                if digest in self.priv.BAD:
                    hits.append((os.path.relpath(p, ROOT), digest[:12]))
        self.assertEqual(hits, [])

    def test_token_hash_matching_catches_all_token_shapes(self):
        cases = [
            ("synthowner", "Created by SynthOwner on workstation"),
            ("가상식별", "담당자 가상식별님 메모"),
            (".priv-mesh.net", "https://node1.sub.priv-mesh.net/status"),
            ("custom-helper.py", "run tools/custom-helper.py --flag"),
            ("internal_hook", "enable internal_hook=1"),
            ("x:\\privdir", "path X:\\PrivDir\\sub\\file.txt"),
            ("priv-pkg\\cli", "invoking C:\\opt\\priv-pkg\\cli\\main.py"),
        ]
        tmp = tempfile.mkdtemp(prefix="ep-sec-priv-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for token, sample_text in cases:
            fp = os.path.join(tmp, "sample.py")
            with open(fp, "w", encoding="utf-8") as f:
                f.write(sample_text)
            digest = hashlib.sha256(token.lower().encode("utf-8")).hexdigest()
            with mock.patch.object(self.priv, "files", return_value=[fp]), \
                    mock.patch.object(self.mod.Private, "BAD", frozenset({digest})):
                with self.assertRaises(AssertionError, msg=f"failed to flag token shape for {token!r}"):
                    self.priv.test_no_private_words()


class BuildBundlePrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = _load_build_module()
        cls.spec_ns = _load_spec_helpers()

    def test_check_bundle_passes(self):
        self.build.check_bundle()

    def test_stage_dir_uses_fresh_mkdtemp_not_programdata(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EP_BUILD_STAGE", None)
            stage_root = self.build.create_stage_dir()
            self.addCleanup(shutil.rmtree, stage_root, True)
        self.assertTrue(os.path.isdir(stage_root))
        self.assertNotIn("programdata", stage_root.lower())
        with self.assertRaises(SystemExit):
            self.build.create_stage_dir(stage_root)

    def test_spec_clean_datas_excludes_pycache_and_pyc(self):
        tmp = tempfile.mkdtemp(prefix="ep-sec-spec-")
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = os.path.join(tmp, "ep")
        os.makedirs(os.path.join(pkg, "__pycache__"))
        Path(pkg, "mod.py").write_text("x = 1\n", encoding="utf-8")
        Path(pkg, "stale.pyc").write_bytes(b"\0" * 20)
        Path(pkg, "stale.pyo").write_bytes(b"\0" * 20)
        Path(pkg, "__pycache__", "mod.cpython-314.pyc").write_bytes(b"\0" * 20)
        top_py = os.path.join(tmp, "ep.py")
        top_pyc = os.path.join(tmp, "ep.pyc")
        Path(top_py).write_text("x = 2\n", encoding="utf-8")
        Path(top_pyc).write_bytes(b"\0" * 20)

        cleaned = self.spec_ns["_clean_datas"]([(pkg, "ep"), (top_py, "."), (top_pyc, "."),
                                                (os.path.join(pkg, "__pycache__", "mod.cpython-314.pyc"), "ep")])
        srcs = [os.path.basename(s) for s, _ in cleaned]
        self.assertEqual(sorted(srcs), ["ep.py", "mod.py"])
        for s, d in cleaned:
            self.assertNotIn("__pycache__", s.lower())
            self.assertNotIn("__pycache__", d.lower())
            self.assertFalse(s.lower().endswith((".pyc", ".pyo", ".pdb")))

    def test_spec_strips_local_paths_from_code_objects(self):
        fake_src = os.path.join(os.path.expanduser("~"), "worktree", "ep", "sample.py")
        co = compile("def outer():\n    return [x for x in (1, 2)]\n", fake_src, "exec")
        cache = {"ep.sample": co}
        pure_toc = [("ep.sample", fake_src, "PYMODULE")]
        self.spec_ns["_strip_code_paths"](pure_toc, cache)
        stripped = cache["ep.sample"]
        for fn in self.build._code_filenames(stripped):
            self.assertEqual(fn, os.path.join("ep", "sample.py"))
            self.assertFalse(self.build._has_local_user_path(fn))

    def test_stage_source_and_bundle_verification(self):
        tmp = tempfile.mkdtemp(prefix="ep-sec-stage-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake_root = os.path.join(tmp, "repo")
        for d in ("ep", "prompts", "cp", "prompts_contest", "ui", "app", "assets", "build"):
            os.makedirs(os.path.join(fake_root, d), exist_ok=True)
        for f in ("ep.py", "cp.py"):
            Path(fake_root, f).write_text("pass\n", encoding="utf-8")
        shutil.copy2(os.path.join(ROOT, "build", "entrypilot.spec"), os.path.join(fake_root, "build", "entrypilot.spec"))
        os.makedirs(os.path.join(fake_root, "ep", "__pycache__"))
        Path(fake_root, "ep", "__pycache__", "cached.pyc").write_bytes(b"1234")
        Path(fake_root, "cp", "orphan.pyc").write_bytes(b"1234")
        Path(fake_root, "ep", "ok.py").write_text("ok = True\n", encoding="utf-8")

        staged = self.build.stage_source(fake_root, os.path.join(tmp, "stage"))
        self.assertTrue(os.path.isfile(os.path.join(staged, "ep", "ok.py")))
        self.assertTrue(os.path.isfile(os.path.join(staged, "build", "entrypilot.spec")))
        self.assertFalse(os.path.exists(os.path.join(staged, "ep", "__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(staged, "cp", "orphan.pyc")))

        # Bundle verification catches leaked co_filename in base_library.zip and __pycache__ in data dirs
        app_dir = os.path.join(tmp, "bundle")
        os.makedirs(os.path.join(app_dir, "_internal", "ep", "__pycache__"))
        Path(app_dir, "_internal", "ep", "__pycache__", "x.pyc").write_bytes(b"\0" * 20)
        Path(app_dir, "_internal", "ep", "stray.pyc").write_bytes(b"\0" * 20)
        self.build.prune_bundled_bytecode(app_dir)
        self.assertFalse(os.path.exists(os.path.join(app_dir, "_internal", "ep", "__pycache__")))

        import importlib.util as ilu
        header = ilu.MAGIC_NUMBER + struct.pack("<I", 1) + b"\0" * 8
        leaked_co = compile("a = 1\n", os.path.join(os.path.expanduser("~"), "secret", "a.py"), "exec")
        zpath = os.path.join(app_dir, "_internal", "base_library.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("a.pyc", header + marshal.dumps(leaked_co))
        with self.assertRaises(SystemExit):
            self.build.verify_clean_bundle(app_dir)

        clean_co = compile("a = 1\n", "a.py", "exec")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("a.pyc", header + marshal.dumps(clean_co))
        self.build.verify_clean_bundle(app_dir)


class ExcelFormulaInjectionTests(unittest.TestCase):
    def setUp(self):
        from ep import config, model, render, store, translate
        self.config = config
        self.model = model
        self.render = render
        self.store = store
        self.translate = translate
        self.tmp = tempfile.mkdtemp(prefix="ep-sec-xlsx-")
        self.saved_env = {k: os.environ.get(k) for k in ("EP_DATA_DIR", "EP_OWNER_DIR")}
        os.environ["EP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["EP_OWNER_DIR"] = os.path.join(self.tmp, "owner")
        os.makedirs(os.environ["EP_DATA_DIR"], exist_ok=True)
        render._sensitive_cache = None
        p = mock.patch.object(translate, "run_model", side_effect=lambda prompt, timeout=120: "[]")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.render._sensitive_cache = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_item(self, iid, state, **kw):
        dl = (self.model.today_kst() + timedelta(days=5)).isoformat()
        row = {
            "id": iid, "region": "KR", "title": f"Safe {iid}", "org": f"Org {iid}", "url": "",
            "source_url": f"https://example.com/{iid}", "form_type": "other", "site_kind": "site_form",
            "deadline": dl, "deadline_time": "", "prize": "", "state": state,
            "added_at": "2026-11-01T00:00:00+09:00", "updated_at": "2026-11-01T00:00:00+09:00",
            "tries": {}, "flags": {},
        }
        row.update(kw)
        return row

    def _assert_no_formula_cells(self, xlsx_path: str):
        wb = openpyxl.load_workbook(xlsx_path, data_only=False)
        try:
            for ws in wb.worksheets:
                for row in ws.iter_rows():
                    for cell in row:
                        self.assertNotEqual(cell.data_type, "f", f"formula cell at {ws.title}!{cell.coordinate}: {cell.value!r}")
                        if cell.value is not None:
                            self.assertEqual(cell.data_type, "s", f"non-string cell at {ws.title}!{cell.coordinate}: {cell.data_type}")
                            self.assertFalse(str(cell.value).startswith(("=", "+", "-", "@", "\t", "\r")),
                                             f"unescaped formula lead at {ws.title}!{cell.coordinate}: {cell.value!r}")
        finally:
            wb.close()
        with zipfile.ZipFile(xlsx_path, "r") as zf:
            for name in zf.namelist():
                if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                    xml = zf.read(name).decode("utf-8", errors="replace")
                    self.assertIsNone(re.search(r"<f(?:\s|>)", xml), f"XML formula tag found in {name}")

    def test_render_xlsx_escapes_all_formula_prefixes_without_formula_cells(self):
        payloads = [
            ("=", "=1+1", "=ORG_EQ", "=질문_EQ"),
            ("+", "+1+1", "+ORG_PL", "+질문_PL"),
            ("-", "-1+1", "-ORG_MN", "-질문_MN"),
            ("@", "@A(1)", "@O(1)", "@질문_AT"),
            ("\t", "\t=1+1", "\t=ORG_TB", "\t=질문_TB"),
            ("\r", "\r=1+1", "\r=ORG_CR", "\r=질문_CR"),
            ("sp", "  =1+1", "  =ORG_SP", "  =질문_SP"),
        ]
        with self.store.writer() as db:
            for idx, (_, title_p, org_p, q_p) in enumerate(payloads, 1):
                self.store.insert_item(db, self._make_item(f"E10{idx}", "manual", need="login",
                                                           title=title_p, org=org_p, reason=q_p))
                self.store.insert_item(db, self._make_item(f"E20{idx}", "owner",
                                                           title=title_p, org=org_p, question=q_p))

        # Seed an existing workbook with dangerous answer strings on unapplied / unknown IDs so carry-over is tested too
        xlsx_path = self.config.owner_xlsx_path()
        os.makedirs(os.path.dirname(xlsx_path), exist_ok=True)
        seed_wb = openpyxl.Workbook()
        seed_wb.remove(seed_wb.active)
        ws_m = seed_wb.create_sheet(self.config.XLSX_SHEET_MANUAL)
        ws_m.append(self.render.MANUAL_HEAD)
        ws_m.append(["E101", "", "", "", "", "", "했음", "=ANS_MANUAL()"])
        ws_m.append(["E991", "", "", "", "", "", "했음", "+ANS_KEPT_M"])
        ws_o = seed_wb.create_sheet(self.config.XLSX_SHEET_OWNER)
        ws_o.append(self.render.OWNER_HEAD)
        for idx, ans in enumerate(("=ANS_EQ()", "+ANS_PL", "-ANS_MN", "@A(9)", "\t=ANS_TB", "\r=ANS_CR"), 1):
            ws_o.append([f"E90{idx}", "", "", "", "", "", ans])
            for c in ws_o[ws_o.max_row]:
                if c.value is not None:
                    c.data_type = "s"
        for row in ws_m.iter_rows(min_row=2):
            for c in row:
                if c.value is not None:
                    c.data_type = "s"
        seed_wb.save(xlsx_path)
        seed_wb.close()

        res = self.render.run()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["xlsx"], xlsx_path)
        self._assert_no_formula_cells(xlsx_path)

        # Second render must remain safe and must not double-prefix apostrophes
        res2 = self.render.run()
        self.assertTrue(res2["ok"], res2)
        self._assert_no_formula_cells(xlsx_path)
        wb = openpyxl.load_workbook(xlsx_path, data_only=False)
        try:
            wo = wb[self.config.XLSX_SHEET_OWNER]
            titles = [r[3].value for r in wo.iter_rows(min_row=2) if r[3].value]
            self.assertIn("'=1+1", titles)
            self.assertFalse(any(t.startswith("''") for t in titles), titles)
        finally:
            wb.close()

    def test_write_xlsx_direct_never_writes_formula_cells(self):
        xlsx_path = os.path.join(self.tmp, "direct.xlsx")
        mrows = [["E001", "2099-01-01", "=SITE()", "+TITLE()", "-NEED()", "https://example.com", "=DONE", "@A(1)"]]
        orows = [["E002", "2099-01-01", "\t=SITE", "\r=TITLE", "=QUESTION?", "https://example.com", "-ANSWER"]]
        self.render.write_xlsx(xlsx_path, mrows, orows)
        self._assert_no_formula_cells(xlsx_path)


if __name__ == "__main__":
    unittest.main()
