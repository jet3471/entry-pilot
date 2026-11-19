"""Round 2 Worker D security tests (D1..D7).

Pure unittest; no network, no real Chrome/Edge execution, no AI CLI execution.
"""
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_BOOT = tempfile.mkdtemp(prefix="ep-r2d-boot-")
os.environ["EP_APP_DIR"] = _BOOT
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import deps, engine, installer, logins, paths, server, settings  # noqa: E402
from cp import browser_check as cp_bcheck, chrome as cp_chrome  # noqa: E402
from ep import browser_check as ep_bcheck, chrome as ep_chrome  # noqa: E402


def _make_junction(link_path: str, target_path: str):
    cmd_exe = paths.system32_exe("cmd.exe")
    subprocess.run([cmd_exe, "/d", "/c", "mklink", "/J", link_path, target_path],
                   capture_output=True, check=True)


def _remove_junction_or_dir(p: str):
    if not os.path.lexists(p):
        return
    if installer._is_reparse(p):
        os.rmdir(p)
    else:
        shutil.rmtree(p, ignore_errors=True)


class TestD1BrowserValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2d-bcheck-")
        self.pf = os.path.join(self.tmp, "PF")
        self.pfx86 = os.path.join(self.tmp, "PFx86")
        self.localapp = os.path.join(self.tmp, "LocalAppData")
        for d in (self.pf, self.pfx86, self.localapp):
            os.makedirs(d, exist_ok=True)
        ep_bcheck._SIG_CACHE.clear()
        cp_bcheck._SIG_CACHE.clear()

    def tearDown(self):
        ep_bcheck._SIG_CACHE.clear()
        cp_bcheck._SIG_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_pe(self, full_path: str, payload: bytes = b"MZ\x90\x00test") -> str:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(payload)
        return full_path

    def test_twins_identical_and_engines_import_without_app_package(self):
        with open(os.path.join(ROOT, "ep", "browser_check.py"), "rb") as f1, \
                open(os.path.join(ROOT, "cp", "browser_check.py"), "rb") as f2:
            self.assertEqual(f1.read(), f2.read())

        code = (
            "import sys; sys.path.insert(0, %r); sys.modules['app'] = None; sys.modules['app.deps'] = None; "
            "import ep.chrome, cp.chrome; "
            "assert callable(ep.chrome.valid_browser) and callable(cp.chrome.valid_browser); "
            "print('isolated-ok')" % ROOT
        )
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("isolated-ok", r.stdout)

    def test_all_four_validators_agree_on_paths_signatures_unc_and_reparse(self):
        chrome_ok = self._write_pe(os.path.join(self.pf, "Google", "Chrome", "Application", "chrome.exe"))
        edge_ok = self._write_pe(os.path.join(self.pfx86, "Microsoft", "Edge", "Application", "msedge.exe"))
        chrome_user = self._write_pe(os.path.join(self.localapp, "Google", "Chrome", "Application", "chrome.exe"))
        wrong_sub = self._write_pe(os.path.join(self.localapp, "Temp", "chrome.exe"))
        wrong_dir = self._write_pe(os.path.join(self.pf, "Evil", "chrome.exe"))
        swapped_name = self._write_pe(os.path.join(self.pf, "Google", "Chrome", "Application", "msedge.exe"))
        unsigned_chrome = self._write_pe(os.path.join(self.pfx86, "Google", "Chrome", "Application", "chrome.exe"))

        sigs = {
            os.path.normcase(os.path.realpath(chrome_ok)): (True, "CN=Google LLC, O=Google LLC, C=US"),
            os.path.normcase(os.path.realpath(chrome_user)): (True, "CN=Google LLC, O=Google LLC, C=US"),
            os.path.normcase(os.path.realpath(edge_ok)): (True, "CN=Microsoft Corporation, O=Microsoft Corporation"),
            os.path.normcase(os.path.realpath(wrong_sub)): (True, "CN=Google LLC, O=Google LLC"),
            os.path.normcase(os.path.realpath(wrong_dir)): (True, "CN=Google LLC, O=Google LLC"),
            os.path.normcase(os.path.realpath(swapped_name)): (True, "CN=Microsoft Corporation, O=Microsoft Corporation"),
            os.path.normcase(os.path.realpath(unsigned_chrome)): (False, ""),
        }

        def fake_sig(path):
            return sigs.get(os.path.normcase(os.path.realpath(path)), (False, ""))

        env = {"ProgramFiles": self.pf, "ProgramFiles(x86)": self.pfx86, "LOCALAPPDATA": self.localapp}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(ep_bcheck, "_read_authenticode_subject", side_effect=fake_sig), \
                mock.patch.object(cp_bcheck, "_read_authenticode_subject", side_effect=fake_sig):
            # Empty string clears setting (auto-detect), whitespace rejected
            self.assertEqual(server._check_browser_path(""), "")
            self.assertIsNone(server._check_browser_path("   "))
            self.assertIsNone(server._check_browser_path(None))

            for valid_p in (chrome_ok, edge_ok, chrome_user):
                self.assertTrue(deps.valid_browser(valid_p), valid_p)
                self.assertTrue(ep_chrome.valid_browser(valid_p), valid_p)
                self.assertTrue(cp_chrome.valid_browser(valid_p), valid_p)
                self.assertEqual(server._check_browser_path(valid_p), os.path.realpath(valid_p))

            # Wrong signer for chrome.exe (signed by Microsoft instead of Google LLC) -> rejected
            sigs[os.path.normcase(os.path.realpath(chrome_user))] = (True, "CN=Microsoft Corporation, O=Microsoft Corporation")
            ep_bcheck._SIG_CACHE.clear()
            cp_bcheck._SIG_CACHE.clear()

            invalid_cases = [
                chrome_user,  # wrong signer
                wrong_sub,    # under %LOCALAPPDATA%\Temp, not standard install path
                wrong_dir,    # under %ProgramFiles%\Evil
                swapped_name, # msedge.exe inside Google\Chrome\Application
                unsigned_chrome,
                chrome_ok + ":ads",
                chrome_ok + ".",
                r"\\evil-server\share\Google\Chrome\Application\chrome.exe",
                r"\\?\C:\Program Files\Google\Chrome\Application\chrome.exe",
                os.path.join(self.pf, "Google", "Chrome", "..", "Chrome", "Application", "chrome.exe"),
            ]
            for bad_p in invalid_cases:
                self.assertFalse(deps.valid_browser(bad_p), f"deps accepted {bad_p!r}")
                self.assertFalse(ep_chrome.valid_browser(bad_p), f"ep accepted {bad_p!r}")
                self.assertFalse(cp_chrome.valid_browser(bad_p), f"cp accepted {bad_p!r}")
                self.assertIsNone(server._check_browser_path(bad_p), f"server accepted {bad_p!r}")

    def test_reparse_parent_rejected_and_signature_cached_per_path_size_mtime(self):
        real_target_dir = os.path.join(self.tmp, "real_edge_app")
        real_edge = self._write_pe(os.path.join(real_target_dir, "msedge.exe"), b"MZ-edge-v1")
        ms_parent = os.path.join(self.localapp, "Microsoft", "Edge")
        os.makedirs(ms_parent, exist_ok=True)
        junc_app = os.path.join(ms_parent, "Application")
        _make_junction(junc_app, real_target_dir)
        self.addCleanup(_remove_junction_or_dir, junc_app)

        junc_edge = os.path.join(junc_app, "msedge.exe")
        calls = []

        def fake_sig(path):
            calls.append(path)
            return True, "CN=Microsoft Corporation, O=Microsoft Corporation"

        env = {"ProgramFiles": self.pf, "ProgramFiles(x86)": self.pfx86, "LOCALAPPDATA": self.localapp}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(ep_bcheck, "_read_authenticode_subject", side_effect=fake_sig):
            # Junction on parent folder `Application` must be rejected before even trusting signature
            self.assertFalse(deps.valid_browser(junc_edge))
            self.assertEqual(calls, [])

            # Direct non-reparse Chrome path caches per (path, size, mtime)
            chrome_p = self._write_pe(os.path.join(self.pf, "Google", "Chrome", "Application", "chrome.exe"), b"MZ-1")
            with mock.patch.object(ep_bcheck, "_read_authenticode_subject",
                                   side_effect=lambda p: calls.append(p) or (True, "O=Google LLC, CN=Google LLC")):
                self.assertTrue(deps.valid_browser(chrome_p))
                self.assertTrue(deps.valid_browser(chrome_p))
                self.assertEqual(len(calls), 1)

                # Modify mtime -> cache miss -> re-verified
                st = os.stat(chrome_p)
                os.utime(chrome_p, (st.st_atime, st.st_mtime + 10.0))
                self.assertTrue(deps.valid_browser(chrome_p))
                self.assertEqual(len(calls), 2)

                # Modify size (keep same mtime as after write? size differs) -> cache miss -> re-verified
                with open(chrome_p, "ab") as f:
                    f.write(b"extra")
                os.utime(chrome_p, (st.st_atime, st.st_mtime + 10.0))
                self.assertTrue(deps.valid_browser(chrome_p))
                self.assertEqual(len(calls), 3)

    def test_host_installed_browser_authenticode_if_present(self):
        # Read-only WinVerifyTrust verification against real installed Chrome/Edge on Windows if present (never executed).
        for env_name, sub in (("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
                              ("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe")):
            base = os.environ.get(env_name)
            cand = os.path.join(base, sub) if base else ""
            if cand and os.path.isfile(cand) and not ep_bcheck._is_reparse(cand):
                self.assertTrue(deps.valid_browser(cand), cand)


class TestD2PinnedNpmIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2d-npm-")
        self.saved_env = os.environ.get("EP_APP_DIR")
        os.environ["EP_APP_DIR"] = self.tmp
        paths.ensure_dirs()
        os.makedirs(deps.node_dir(), exist_ok=True)
        os.makedirs(os.path.dirname(deps.npm_cli()), exist_ok=True)
        with open(deps.node_exe(), "wb") as f:
            f.write(b"MZ")
        with open(deps.npm_cli(), "w", encoding="utf-8") as f:
            f.write("// npm")

    def tearDown(self):
        if self.saved_env is None:
            os.environ.pop("EP_APP_DIR", None)
        else:
            os.environ["EP_APP_DIR"] = self.saved_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exact_versions_and_integrity_table(self):
        self.assertEqual(deps.NPM_PACKAGES, {
            "claude": "@anthropic-ai/claude-code",
            "codex": "@openai/codex",
            "gemini": "@google/gemini-cli",
        })
        self.assertEqual(deps.NPM_VERSIONS, {
            "claude": "2.1.327",
            "codex": "0.169.0",
            "gemini": "0.68.0",
        })
        for v in deps.NPM_VERSIONS.values():
            self.assertNotEqual(v, "latest")
        self.assertEqual(deps.NPM_INTEGRITY, {
            "claude": "sha512-QToNtikIupqVMsxRUdDHUP0nS/uv3Wc43vcbT8YNRo7FtQOE9MvYLqPLQPoVhJ2DQQVPOFMB5PkaBNJbnNYeVg==",
            "codex": "sha512-KhzYlkKPxoNssR8CEm45IjqnFWn7UtlF4CiD3tOJPea51xBs7MaHy0UQG9GSbCqk6Lrjn5QdJDV0QD1/FgWlGw==",
            "gemini": "sha512-eq7AWY+YCijNGKVwyiRjN+GuOdXU/djuOZhX18p+HDmNyUqFdqH7C5V6vDJLl1uffhRPhGijePV+YvY8lymrbQ==",
        })

    def test_pack_sha512_verify_then_global_prefix_install(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((list(cmd), kw.get("cwd")))
            if "ci" in cmd:
                pkg_dir = os.path.join(kw["cwd"], "node_modules", "@google", "gemini-cli", "bundle")
                os.makedirs(pkg_dir, exist_ok=True)
                with open(os.path.join(os.path.dirname(pkg_dir), "package.json"), "w", encoding="utf-8") as f:
                    json.dump({"bin": {"gemini": "bundle/gemini.js"}}, f)
                with open(os.path.join(pkg_dir, "gemini.js"), "w", encoding="utf-8") as f:
                    f.write("console.log('ok')\n")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch("app.deps.subprocess.run", side_effect=fake_run):
            deps.install_clis(["gemini"], lambda *a: None)

        self.assertEqual(len(calls), 1)
        ci_cmd, ci_cwd = calls[0]
        self.assertEqual(ci_cmd[2:], ["ci", "--ignore-scripts", "--no-audit", "--no-fund"])
        self.assertEqual(os.path.normcase(ci_cwd), os.path.normcase(deps.cli_prefix("gemini")))

    def test_integrity_mismatch_refuses_before_install(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            raise AssertionError("npm ci must never be called when top-level lock sha512 mismatches")

        bad_digest = "sha512-" + base64.b64encode(hashlib.sha512(b"tampered").digest()).decode("ascii")
        with mock.patch.dict(deps.NPM_INTEGRITY, {"codex": bad_digest}), \
                mock.patch("app.deps.subprocess.run", side_effect=fake_run):
            with self.assertRaises(deps.StepError):
                deps.install_clis(["codex"], lambda *a: None)
        self.assertEqual(calls, [])


class TestD3DirectLoginNoCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2d-login-")
        self.saved_env = os.environ.get("EP_APP_DIR")
        os.environ["EP_APP_DIR"] = self.tmp

    def tearDown(self):
        if self.saved_env is None:
            os.environ.pop("EP_APP_DIR", None)
        else:
            os.environ["EP_APP_DIR"] = self.saved_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_login_allows_ampersand_and_percent_without_cmd_exe(self):
        tricky_dir = os.path.join(self.tmp, "User&Co%100%Dir")
        os.makedirs(tricky_dir, exist_ok=True)
        exe = os.path.join(tricky_dir, "claude.exe")
        with open(exe, "wb") as f:
            f.write(b"MZ")

        with mock.patch.object(deps, "cli_command", return_value=[exe]), \
                mock.patch.object(logins.subprocess, "Popen") as popen:
            fake_proc = mock.MagicMock()
            popen.return_value = fake_proc
            res = logins.open_login("claude", 1, 1)
            self.assertIs(res, fake_proc)
            popen.assert_called_once()
            argv, kwargs = popen.call_args
            self.assertEqual(argv[0], [exe, "auth", "login"])
            self.assertNotIn("cmd", os.path.basename(argv[0][0]).lower())
            self.assertEqual(kwargs.get("creationflags"), logins.NEW_CONSOLE)

    def test_resolves_cmd_shim_to_node_and_js_entry(self):
        prefix = os.path.join(self.tmp, "A&B%Path", "cli")
        pkg_dir = os.path.join(prefix, "node_modules", "@anthropic-ai", "claude-code")
        os.makedirs(pkg_dir, exist_ok=True)
        js_entry = os.path.join(pkg_dir, "cli.js")
        with open(js_entry, "w", encoding="utf-8") as f:
            f.write("console.log('claude')\n")
        shim = os.path.join(prefix, "claude.cmd")
        with open(shim, "w", encoding="utf-8") as f:
            f.write('@ECHO off\r\n"%_prog%" "%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n')

        fake_node = os.path.join(self.tmp, "A&B%Path", "node.exe")
        with open(fake_node, "wb") as f:
            f.write(b"MZ")

        with mock.patch.object(deps, "node_exe", return_value=fake_node), \
                mock.patch.object(deps, "cli_command", return_value=[shim]), \
                mock.patch.object(logins.subprocess, "Popen") as popen:
            logins.open_login("claude", 1, 1)
            argv, kwargs = popen.call_args
            self.assertEqual(argv[0], [fake_node, os.path.normpath(js_entry), "auth", "login"])
            for part in argv[0]:
                self.assertFalse(part.lower().endswith((".cmd", ".bat")), part)
            self.assertEqual(kwargs.get("creationflags"), logins.NEW_CONSOLE)


class TestD4D7UninstallAndEpAppDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2d-uninst-")
        self.real_inst = os.path.join(self.tmp, "LocalApp", "EntryPilot")
        self.real_app = os.path.join(self.real_inst, "app")
        self.poison_dir = os.path.join(self.tmp, "PoisonEpAppDir")
        os.makedirs(self.real_app, exist_ok=True)
        os.makedirs(os.path.join(self.poison_dir, "app"), exist_ok=True)
        self.fake_exe = os.path.join(self.real_app, "EntryPilot.exe")
        with open(self.fake_exe, "wb") as f:
            f.write(b"MZ")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ep_app_dir_is_kept_for_engine_but_never_trusted_for_uninstall(self):
        with mock.patch.dict(os.environ, {"EP_APP_DIR": self.poison_dir}, clear=False):
            engine.configure()
            self.assertEqual(os.environ["EP_APP_DIR"], self.poison_dir)

            # Without recorded install root in registry -> refused even though EP_APP_DIR has app\
            with mock.patch.object(installer, "recorded_install_root", return_value=""):
                self.assertEqual(installer.valid_uninstall_target(self.poison_dir), "")
                r = installer.uninstall()
                self.assertFalse(r["ok"])
                self.assertTrue(r["error"])

            # With registry pointing to real_inst, poison_dir is still refused; default uninstall targets real_inst
            with mock.patch.object(installer, "recorded_install_root", return_value=self.real_inst), \
                    mock.patch.object(paths, "frozen", return_value=True), \
                    mock.patch.object(sys, "executable", self.fake_exe), \
                    mock.patch.object(installer.subprocess, "Popen") as popen, \
                    mock.patch.object(installer, "threading_exit"):
                bad = installer.uninstall(target=self.poison_dir)
                self.assertFalse(bad["ok"])
                popen.assert_not_called()

                ok = installer.uninstall(keep_data=False)
                self.assertTrue(ok["ok"], ok)
                self.assertEqual(os.path.normcase(ok["removed"]), os.path.normcase(os.path.realpath(self.real_inst)))
                argv, kwargs = popen.call_args
                self.addCleanup(shutil.rmtree, kwargs["cwd"], True)
                self.assertNotEqual(os.path.basename(argv[0][0]).lower(), "cmd.exe")
                self.assertTrue(os.path.normcase(kwargs["cwd"]).startswith(
                    os.path.normcase(os.path.realpath(tempfile.gettempdir()))
                ))

    def test_frozen_exe_and_registry_disagreement_refused(self):
        other_inst = os.path.join(self.tmp, "OtherLocation", "EntryPilot")
        os.makedirs(os.path.join(other_inst, "app"), exist_ok=True)
        other_exe = os.path.join(other_inst, "app", "EntryPilot.exe")
        with open(other_exe, "wb") as f:
            f.write(b"MZ")

        with mock.patch.object(installer, "recorded_install_root", return_value=self.real_inst), \
                mock.patch.object(paths, "frozen", return_value=True), \
                mock.patch.object(sys, "executable", other_exe), \
                mock.patch.object(installer.subprocess, "Popen") as popen:
            r = installer.uninstall()
            self.assertFalse(r["ok"])
            self.assertIn("일치하지 않아", r["error"])
            popen.assert_not_called()

    def test_temp_helper_deletes_valid_dir_and_refuses_reparse_swap(self):
        helper_py = os.path.join(self.tmp, "helper.py")
        with open(helper_py, "w", encoding="utf-8") as f:
            f.write(installer._UNINSTALL_HELPER_PY)

        victim_outside = os.path.join(self.tmp, "outside_keep")
        os.makedirs(victim_outside, exist_ok=True)
        secret_file = os.path.join(victim_outside, "important.txt")
        with open(secret_file, "w", encoding="utf-8") as f:
            f.write("DO NOT DELETE")

        # 1) Valid directory is deleted
        valid_dir = os.path.join(self.tmp, "sub", "to_delete")
        os.makedirs(valid_dir, exist_ok=True)
        with open(os.path.join(valid_dir, "a.txt"), "w", encoding="utf-8") as f:
            f.write("bye")
        r1 = subprocess.run([sys.executable, "-X", "utf8", helper_py, "0", valid_dir], capture_output=True, timeout=15)
        self.assertEqual(r1.returncode, 0)
        self.assertFalse(os.path.exists(valid_dir))

        # 2) Swapped for junction right before helper runs -> helper refuses and victim stays intact!
        swap_dir = os.path.join(self.tmp, "sub", "swapped_junc")
        _make_junction(swap_dir, victim_outside)
        self.addCleanup(_remove_junction_or_dir, swap_dir)
        r2 = subprocess.run([sys.executable, "-X", "utf8", helper_py, "0", swap_dir], capture_output=True, timeout=15)
        self.assertNotEqual(r2.returncode, 0)
        self.assertTrue(os.path.isfile(secret_file))


class TestD5InstallerPidBackupAndSafeZip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r2d-inst-")
        self.localapp = os.path.join(self.tmp, "LocalApp")
        os.makedirs(self.localapp, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_zip(self, zpath: str, entries: dict[str, bytes]):
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        return zpath

    def test_stop_running_app_checks_exe_inside_install_and_ctime_before_mtime(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.localapp}, clear=False):
            inst = paths.install_root()
            os.makedirs(os.path.join(inst, "app"), exist_ok=True)
            inside_exe = os.path.join(inst, "app", "EntryPilot.exe")
            with open(inside_exe, "wb") as f:
                f.write(b"MZ")
            outside_exe = os.path.join(self.tmp, "other", "EntryPilot.exe")
            os.makedirs(os.path.dirname(outside_exe), exist_ok=True)
            with open(outside_exe, "wb") as f:
                f.write(b"MZ")

            run_file = os.path.join(inst, "run.json")
            with open(run_file, "w", encoding="utf-8") as f:
                json.dump({"pid": 999999}, f)
            mtime = os.stat(run_file).st_mtime

            # 1) Process executable outside install folder -> must NOT kill
            with mock.patch.object(installer, "_process_info", return_value=(outside_exe, mtime - 10.0)), \
                    mock.patch.object(installer.subprocess, "run") as mrun:
                self.assertFalse(installer.stop_running_app())
                mrun.assert_not_called()

            # 2) Process created AFTER run.json mtime (PID recycled) -> must NOT kill
            with mock.patch.object(installer, "_process_info", return_value=(inside_exe, mtime + 5.0)), \
                    mock.patch.object(installer.subprocess, "run") as mrun:
                self.assertFalse(installer.stop_running_app())
                mrun.assert_not_called()

            # 3) Process inside install folder AND created before run.json mtime -> killed via System32\taskkill.exe
            with mock.patch.object(installer, "_process_info", return_value=(inside_exe, mtime - 5.0)), \
                    mock.patch.object(installer.subprocess, "run") as mrun:
                self.assertTrue(installer.stop_running_app())
                mrun.assert_called_once()
                cmd = mrun.call_args[0][0]
                self.assertEqual(os.path.normcase(cmd[0]), os.path.normcase(paths.system32_exe("taskkill.exe")))
                self.assertEqual(cmd[1:], ["/PID", "999999", "/T", "/F"])

    def _make_pe_bytes(self, tag: bytes) -> bytes:
        import struct
        dos = b"MZ" + b"\x00" * (0x3C - 2) + struct.pack("<I", 0x40)
        pe = b"PE\x00\x00" + struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 0, 0x0022)
        return dos + pe + tag

    def test_install_zip_keeps_old_until_verified_and_restores_on_failure(self):
        pe_v1 = self._make_pe_bytes(b"-version-1")
        pe_v2 = self._make_pe_bytes(b"-version-2")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.localapp, "EP_TEST_NO_SHELL": "1"}, clear=False):
            z1 = self._make_zip(
                os.path.join(self.tmp, "v1.zip"),
                {"EntryPilot.exe": pe_v1, "_internal/ep.py": b"# v1\n"},
            )
            exe = installer.install_zip(z1)
            self.assertTrue(os.path.isfile(exe))
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), pe_v1)
            self.assertFalse(os.path.exists(os.path.dirname(exe) + ".old"))

            # Broken upgrade zip (missing EntryPilot.exe / _internal) -> fails verification -> keeps v1!
            z_bad = self._make_zip(os.path.join(self.tmp, "bad.zip"), {"only_readme.txt": b"broken"})
            with self.assertRaises(RuntimeError):
                installer.install_zip(z_bad)
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), pe_v1)
            self.assertFalse(os.path.exists(os.path.dirname(exe) + ".old"))

            # Post-swap verification failure also restores v1 from .old!
            z2 = self._make_zip(
                os.path.join(self.tmp, "v2.zip"),
                {"EntryPilot.exe": pe_v2, "_internal/ep.py": b"# v2\n"},
            )
            with mock.patch.object(installer, "_verify_installed_app", return_value=False):
                with self.assertRaises(RuntimeError):
                    installer.install_zip(z2)
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), pe_v1)
            self.assertFalse(os.path.exists(os.path.dirname(exe) + ".old"))

            # Valid v2 upgrade -> verify sees .old still present while verifying, then .old is removed after!
            seen_old_during_verify = []
            orig_verify = installer._verify_installed_app

            def spy_verify(dest, exe_path):
                seen_old_during_verify.append(os.path.isdir(dest + ".old"))
                return orig_verify(dest, exe_path)

            with mock.patch.object(installer, "_verify_installed_app", side_effect=spy_verify):
                installer.install_zip(z2)
            self.assertEqual(seen_old_during_verify, [True])
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), pe_v2)
            self.assertFalse(os.path.exists(os.path.dirname(exe) + ".old"))

    def test_safe_zip_rejects_traversal_abs_drive_ads_in_installer_and_node(self):
        bad_entries = [
            "../escape.txt",
            "sub/../../escape.txt",
            "/abs/evil.dll",
            "\\abs\\evil.dll",
            "\\\\server\\share\\evil.dll",
            "C:\\Windows\\evil.dll",
            "C:relative.dll",
            "EntryPilot.exe:Zone.Identifier",
            "dir/file.txt:secret",
        ]
        for idx, bad_name in enumerate(bad_entries):
            zpath = os.path.join(self.tmp, f"bad_{idx}.zip")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("EntryPilot.exe", b"MZ")
                zf.writestr(bad_name, b"evil")
            with open(zpath, "wb") as f:
                f.write(buf.getvalue())

            target_dir = os.path.join(self.tmp, f"extract_{idx}")
            with zipfile.ZipFile(zpath) as zf:
                with self.assertRaises(ValueError, msg=bad_name):
                    installer.safe_extract_zip(zf, target_dir)

            # Also verify install_node rejects malicious Node zip
            node_bytes = buf.getvalue()
            node_sha = hashlib.sha256(node_bytes).hexdigest()
            ver = "v22.0.0"
            zname = f"node-{ver}-win-x64.zip"

            def fake_get(url, timeout=60, _sha=node_sha, _zn=zname, _v=ver):
                if url.endswith("index.json"):
                    return json.dumps([{"version": _v, "lts": "Jod", "files": ["win-x64-zip"]}]).encode()
                return f"{_sha}  {_zn}\n".encode()

            def fake_dl(url, dest, *a, _b=node_bytes):
                with open(dest, "wb") as f:
                    f.write(_b)

            with mock.patch.dict(os.environ, {"EP_APP_DIR": os.path.join(self.tmp, f"nodeapp_{idx}")}, clear=False), \
                    mock.patch.object(deps, "node_ok", return_value=False), \
                    mock.patch.object(deps, "_get", side_effect=fake_get), \
                    mock.patch.object(deps, "_download", side_effect=fake_dl):
                with self.assertRaises(deps.StepError, msg=bad_name):
                    deps.install_node(lambda *a: None)


class TestD6System32Paths(unittest.TestCase):
    def test_windows_helpers_use_system32_absolute_paths_and_mbcs(self):
        sr = paths.system_root()
        self.assertEqual(os.path.normcase(paths.system32_exe("taskkill.exe")),
                         os.path.normcase(os.path.join(sr, "System32", "taskkill.exe")))
        self.assertEqual(os.path.normcase(paths.powershell_exe()),
                         os.path.normcase(os.path.join(sr, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")))

        with mock.patch.object(installer.subprocess, "run") as mrun:
            mrun.return_value = subprocess.CompletedProcess([], 0, stdout="C:\\Desktop\n", stderr="")
            installer._special("DesktopDirectory")
            cmd, kw = mrun.call_args
            self.assertEqual(os.path.normcase(cmd[0][0]), os.path.normcase(paths.powershell_exe()))
            self.assertEqual(kw.get("encoding"), "mbcs")

        with mock.patch.object(logins.subprocess, "run") as mrun:
            logins.copy_text("ABCD-1234")
            cmd = mrun.call_args[0][0]
            self.assertEqual(os.path.normcase(cmd[0]), os.path.normcase(paths.system32_exe("clip.exe")))

        with mock.patch.object(cp_chrome.subprocess, "run") as mrun:
            mrun.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            cp_chrome.profile_running()
            cmd, kw = mrun.call_args
            self.assertEqual(os.path.normcase(cmd[0][0]), os.path.normcase(paths.powershell_exe()))
            self.assertEqual(kw.get("encoding"), "mbcs")


def tearDownModule():
    shutil.rmtree(_BOOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
