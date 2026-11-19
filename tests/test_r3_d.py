"""Round 3 Worker D security & functional regression tests (D1..D4).

Pure unittest; no network, no real Chrome/Edge execution, no AI CLI execution.
"""
import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import winreg
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_BOOT = tempfile.mkdtemp(prefix="ep-r3d-boot-")
os.environ["EP_APP_DIR"] = _BOOT
os.environ["CP_APP_DIR"] = _BOOT
os.environ["CP_DATA_DIR"] = os.path.join(_BOOT, "data", "contest")
os.environ["EP_TEST_NO_SHELL"] = "1"

from app import deps, engine, installer, logins, paths, server, settings  # noqa: E402
from cp import browser_check as cp_bcheck, chrome as cp_chrome  # noqa: E402
from ep import browser_check as ep_bcheck, chrome as ep_chrome  # noqa: E402


def _make_valid_pe(extra: bytes = b"payload") -> bytes:
    """Build a minimal valid 64-bit Windows PE binary (DOS MZ + PE\\0\\0 + COFF + PE32+ OptionalHeader)."""
    opt_size = 24
    dos = b"MZ" + b"\x00" * (0x3C - 2) + struct.pack("<I", 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, opt_size, 0x0022)
    opt = struct.pack("<H", 0x020B) + b"\x00" * (opt_size - 2)
    return dos + b"PE\x00\x00" + coff + opt + extra


_ORIG_SUBPROCESS_RUN = subprocess.run


def _make_junction(link_path: str, target_path: str):
    cmd_exe = paths.system32_exe("cmd.exe")
    _ORIG_SUBPROCESS_RUN([cmd_exe, "/d", "/c", "mklink", "/J", link_path, target_path],
                         capture_output=True, text=True, encoding="mbcs", errors="replace", check=True)


def _cleanup_path(p: str):
    if not os.path.lexists(p):
        return
    if installer._is_reparse(p):
        os.rmdir(p)
    else:
        shutil.rmtree(p, ignore_errors=True)


# ====================================================================== D1: npm supply chain
class TestD1NpmSupplyChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3d-npm-")
        self.saved_app_dir = os.environ.get("EP_APP_DIR")
        os.environ["EP_APP_DIR"] = self.tmp
        paths.ensure_dirs()
        os.makedirs(deps.node_dir(), exist_ok=True)
        os.makedirs(os.path.dirname(deps.npm_cli()), exist_ok=True)
        with open(deps.node_exe(), "wb") as f:
            f.write(_make_valid_pe(b"node"))
        with open(deps.npm_cli(), "w", encoding="utf-8") as f:
            f.write("// npm-cli\n")

    def tearDown(self):
        if self.saved_app_dir is None:
            os.environ.pop("EP_APP_DIR", None)
        else:
            os.environ["EP_APP_DIR"] = self.saved_app_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bundled_lockfiles_exist_have_all_dep_integrities_and_link_python_skips_subdir(self):
        for agent, pkg in deps.NPM_PACKAGES.items():
            ver = deps.NPM_VERSIONS[agent]
            want_integ = deps.NPM_INTEGRITY[agent]
            ldir = os.path.join(ROOT, "assets", "runtime", "locks", agent)
            p_pkg = os.path.join(ldir, "package.json")
            p_lock = os.path.join(ldir, "package-lock.json")
            self.assertTrue(os.path.isfile(p_pkg), p_pkg)
            self.assertTrue(os.path.isfile(p_lock), p_lock)
            with open(p_pkg, encoding="utf-8") as f:
                pj = json.load(f)
            with open(p_lock, encoding="utf-8") as f:
                lj = json.load(f)
            self.assertEqual(pj["dependencies"][pkg], ver)
            self.assertEqual(lj["lockfileVersion"], 3)
            top = lj["packages"][f"node_modules/{pkg}"]
            self.assertEqual(top["version"], ver)
            self.assertEqual(top["integrity"], want_integ)
            for k, entry in lj["packages"].items():
                if k == "":
                    continue
                self.assertTrue(entry.get("integrity", "").startswith("sha512-"), f"{agent}:{k}")

        # link_python must copy runtime helper files without failing on the new `locks` subdirectory
        deps.link_python()
        self.assertTrue(os.path.isfile(os.path.join(paths.runtime_dir(), "ocr-ko.ps1")))
        self.assertFalse(os.path.exists(os.path.join(paths.runtime_dir(), "locks")))

    def test_install_clis_uses_npm_ci_ignore_scripts_per_agent_and_runs_claude_install_cjs(self):
        calls: list[tuple[list[str], str]] = []

        def fake_run(cmd, **kw):
            argv = list(cmd)
            cwd = kw.get("cwd") or ""
            calls.append((argv, cwd))
            if argv[1:] == [deps.npm_cli(), "ci", "--ignore-scripts", "--no-audit", "--no-fund"]:
                agent = os.path.basename(cwd)
                if agent == "claude":
                    pkg_dir = os.path.join(cwd, "node_modules", "@anthropic-ai", "claude-code")
                    os.makedirs(os.path.join(pkg_dir, "bin"), exist_ok=True)
                    with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as f:
                        json.dump({"bin": {"claude": "bin/claude.exe"}}, f)
                    # npm ci with --ignore-scripts leaves the tiny placeholder exe and install.cjs
                    with open(os.path.join(pkg_dir, "bin", "claude.exe"), "wb") as f:
                        f.write(b"placeholder")
                    with open(os.path.join(pkg_dir, "install.cjs"), "w", encoding="utf-8") as f:
                        f.write("// postinstall\n")
                elif agent == "codex":
                    pkg_dir = os.path.join(cwd, "node_modules", "@openai", "codex")
                    os.makedirs(os.path.join(pkg_dir, "bin"), exist_ok=True)
                    with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as f:
                        json.dump({"bin": {"codex": "bin/codex.js"}}, f)
                    with open(os.path.join(pkg_dir, "bin", "codex.js"), "w", encoding="utf-8") as f:
                        f.write("// wrapper\n")
                    win_dir = os.path.join(cwd, "node_modules", "@openai", "codex-win32-x64", "vendor")
                    os.makedirs(win_dir, exist_ok=True)
                    with open(os.path.join(win_dir, "codex.exe"), "wb") as f:
                        f.write(_make_valid_pe(b"codex"))
                elif agent == "gemini":
                    pkg_dir = os.path.join(cwd, "node_modules", "@google", "gemini-cli")
                    os.makedirs(os.path.join(pkg_dir, "bundle"), exist_ok=True)
                    with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as f:
                        json.dump({"bin": {"gemini": "bundle/gemini.js"}}, f)
                    with open(os.path.join(pkg_dir, "bundle", "gemini.js"), "w", encoding="utf-8") as f:
                        f.write("console.log('gemini')\n")
                else:
                    raise AssertionError(f"unexpected agent cwd: {cwd}")
                return subprocess.CompletedProcess(argv, 0, stdout="added packages\n", stderr="")

            # Postinstall for claude ONLY: `node install.cjs`
            if len(argv) == 2 and argv[0] == deps.node_exe() and argv[1].endswith("install.cjs"):
                exe_dst = os.path.join(cwd, "bin", "claude.exe")
                with open(exe_dst, "wb") as f:
                    f.write(b"MZ" + b"\x90" * ((1 << 20) + 64))
                return subprocess.CompletedProcess(argv, 0, stdout="installed claude.exe\n", stderr="")

            raise AssertionError(f"unexpected subprocess call: {argv} in {cwd}")

        with mock.patch("app.deps.subprocess.run", side_effect=fake_run):
            deps.install_clis(["claude", "codex", "gemini"], lambda *a: None)

        # 3 npm ci calls + 1 claude install.cjs call = 4 calls total
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0], ([deps.node_exe(), deps.npm_cli(), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                                    deps.cli_prefix("claude")))
        self.assertEqual(calls[1][0], [deps.node_exe(),
                                       os.path.join(deps.cli_prefix("claude"), "node_modules", "@anthropic-ai", "claude-code", "install.cjs")])
        self.assertEqual(calls[2], ([deps.node_exe(), deps.npm_cli(), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                                    deps.cli_prefix("codex")))
        self.assertEqual(calls[3], ([deps.node_exe(), deps.npm_cli(), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                                    deps.cli_prefix("gemini")))

        # Lockfiles were copied verbatim into each <cli_prefix>\<agent>\ folder
        for agent in ("claude", "codex", "gemini"):
            for fn in ("package.json", "package-lock.json"):
                with open(os.path.join(deps.locks_dir(agent), fn), "rb") as src_f, \
                        open(os.path.join(deps.cli_prefix(agent), fn), "rb") as dst_f:
                    self.assertEqual(src_f.read(), dst_f.read())

        # Executable resolution, engine_env, contest_env, and logins all point to per-agent folders
        claude_exe = os.path.join(deps.cli_prefix("claude"), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
        codex_exe = os.path.join(deps.cli_prefix("codex"), "node_modules", "@openai", "codex-win32-x64", "vendor", "codex.exe")
        gemini_js = os.path.join(deps.cli_prefix("gemini"), "node_modules", "@google", "gemini-cli", "bundle", "gemini.js")
        self.assertEqual(deps.cli_command("claude"), [os.path.normpath(claude_exe)])
        self.assertEqual(deps.cli_command("codex"), [os.path.normpath(codex_exe)])
        self.assertEqual(deps.cli_command("gemini"), [deps.node_exe(), os.path.normpath(gemini_js)])

        eenv = engine.engine_env()
        self.assertEqual(eenv["EP_CLAUDE_EXE"], os.path.normpath(claude_exe))
        self.assertEqual(eenv["EP_CODEX_EXE"], os.path.normpath(codex_exe))
        self.assertEqual(json.loads(eenv["EP_GEMINI_CMD"]), [deps.node_exe(), os.path.normpath(gemini_js)])
        path_entries = [os.path.normcase(p) for p in eenv["PATH"].split(os.pathsep)]
        for a in ("claude", "codex", "gemini"):
            self.assertIn(os.path.normcase(os.path.join(deps.cli_prefix(a), "node_modules", ".bin")), path_entries)

        cenv = engine.contest_env()
        self.assertEqual(cenv["CP_CLAUDE_EXE"], os.path.normpath(claude_exe))
        self.assertEqual(cenv["CP_CODEX_EXE"], os.path.normpath(codex_exe))

        with mock.patch.object(logins.subprocess, "Popen") as popen:
            logins.open_login("claude", 1, 1)
            self.assertEqual(popen.call_args[0][0], [os.path.normpath(claude_exe), "auth", "login"])

    def test_tampered_lockfile_refused_before_npm_ci(self):
        fake_locks = os.path.join(self.tmp, "fake_locks", "codex")
        os.makedirs(fake_locks, exist_ok=True)
        shutil.copy2(os.path.join(deps.locks_dir("codex"), "package.json"), os.path.join(fake_locks, "package.json"))
        with open(os.path.join(deps.locks_dir("codex"), "package-lock.json"), encoding="utf-8") as f:
            lock_data = json.load(f)
        # Drop integrity from an optional dependency -> must refuse before running npm ci
        del lock_data["packages"]["node_modules/@openai/codex-win32-x64"]["integrity"]
        with open(os.path.join(fake_locks, "package-lock.json"), "w", encoding="utf-8") as f:
            json.dump(lock_data, f)

        with mock.patch.object(deps, "locks_dir", return_value=fake_locks), \
                mock.patch("app.deps.subprocess.run") as mrun:
            with self.assertRaises(deps.StepError):
                deps.install_clis(["codex"], lambda *a: None)
            mrun.assert_not_called()

    def test_per_agent_cmd_shim_resolution_and_claude_reparse_install_cjs_refused(self):
        # 1) .cmd shim inside <cli_prefix>\gemini\node_modules\.bin\gemini.cmd resolves without cmd.exe
        g_bundle = os.path.join(deps.cli_prefix("gemini"), "node_modules", "@google", "gemini-cli", "bundle")
        os.makedirs(g_bundle, exist_ok=True)
        g_js = os.path.join(g_bundle, "gemini.js")
        with open(os.path.join(os.path.dirname(g_bundle), "package.json"), "w", encoding="utf-8") as f:
            json.dump({"bin": {"gemini": "bundle/gemini.js"}}, f)
        with open(g_js, "w", encoding="utf-8") as f:
            f.write("// gemini\n")
        g_bin = os.path.join(deps.cli_prefix("gemini"), "node_modules", ".bin")
        os.makedirs(g_bin, exist_ok=True)
        g_shim = os.path.join(g_bin, "gemini.cmd")
        with open(g_shim, "w", encoding="utf-8") as f:
            f.write('@ECHO off\r\n"%~dp0\\..\\@google\\gemini-cli\\bundle\\gemini.js" %*\r\n')
        self.assertEqual(deps.resolve_cli_cmd([g_shim, "--version"], "gemini"),
                         [deps.node_exe(), os.path.normpath(g_js), "--version"])

        # 2) Reparse point on claude's install.cjs directory is refused before executing node install.cjs
        outside = os.path.join(self.tmp, "evil_pkg")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "install.cjs"), "w", encoding="utf-8") as f:
            f.write("throw new Error('must not run');\n")

        def fake_ci_with_junc(cmd, **kw):
            argv = list(cmd)
            if "ci" in argv:
                scope_dir = os.path.join(kw["cwd"], "node_modules", "@anthropic-ai")
                os.makedirs(scope_dir, exist_ok=True)
                junc_pkg = os.path.join(scope_dir, "claude-code")
                _make_junction(junc_pkg, outside)
                self.addCleanup(_cleanup_path, junc_pkg)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            raise AssertionError(f"install.cjs must never run through junction: {argv}")

        with mock.patch("app.deps.subprocess.run", side_effect=fake_ci_with_junc):
            with self.assertRaises(deps.StepError):
                deps.install_clis(["claude"], lambda *a: None)

    def test_backward_compat_finds_legacy_global_prefix_and_prefers_new_per_agent(self):
        # Populate ONLY legacy global-prefix `<cli_prefix>\node_modules\...`
        leg_claude = os.path.join(deps.cli_prefix(), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
        os.makedirs(os.path.dirname(leg_claude), exist_ok=True)
        with open(os.path.join(os.path.dirname(os.path.dirname(leg_claude)), "package.json"), "w", encoding="utf-8") as f:
            json.dump({"bin": {"claude": "bin/claude.exe"}}, f)
        with open(leg_claude, "wb") as f:
            f.write(b"MZ" + b"\0" * ((1 << 20) + 16))

        leg_codex_pkg = os.path.join(deps.cli_prefix(), "node_modules", "@openai", "codex")
        os.makedirs(leg_codex_pkg, exist_ok=True)
        with open(os.path.join(leg_codex_pkg, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"bin": {"codex": "bin/codex.js"}}, f)
        leg_codex_exe = os.path.join(deps.cli_prefix(), "node_modules", "@openai", "codex-win32-x64", "codex.exe")
        os.makedirs(os.path.dirname(leg_codex_exe), exist_ok=True)
        with open(leg_codex_exe, "wb") as f:
            f.write(_make_valid_pe(b"legacy-codex"))

        leg_gemini = os.path.join(deps.cli_prefix(), "node_modules", "@google", "gemini-cli", "bundle", "gemini.js")
        os.makedirs(os.path.dirname(leg_gemini), exist_ok=True)
        with open(os.path.join(os.path.dirname(os.path.dirname(leg_gemini)), "package.json"), "w", encoding="utf-8") as f:
            json.dump({"bin": {"gemini": "bundle/gemini.js"}}, f)
        with open(leg_gemini, "w", encoding="utf-8") as f:
            f.write("// legacy gemini\n")

        self.assertEqual(deps.cli_command("claude"), [os.path.normpath(leg_claude)])
        self.assertEqual(deps.cli_command("codex"), [os.path.normpath(leg_codex_exe)])
        self.assertEqual(deps.cli_command("gemini"), [deps.node_exe(), os.path.normpath(leg_gemini)])

        # Now add a per-agent codex install -> per-agent `<cli_prefix>\codex` takes precedence
        new_codex_pkg = os.path.join(deps.cli_prefix("codex"), "node_modules", "@openai", "codex")
        os.makedirs(new_codex_pkg, exist_ok=True)
        with open(os.path.join(new_codex_pkg, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"bin": {"codex": "bin/codex.js"}}, f)
        new_codex_exe = os.path.join(deps.cli_prefix("codex"), "node_modules", "@openai", "codex-win32-x64", "codex.exe")
        os.makedirs(os.path.dirname(new_codex_exe), exist_ok=True)
        with open(new_codex_exe, "wb") as f:
            f.write(_make_valid_pe(b"new-codex"))

        self.assertEqual(deps.cli_command("codex"), [os.path.normpath(new_codex_exe)])


# ====================================================================== D2: Browser channels, exact O/CN, cache & 10-min TTL
class TestD2BrowserChannelsSignerAndCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3d-browser-")
        self.pf = os.path.join(self.tmp, "PF")
        self.pfx86 = os.path.join(self.tmp, "PFx86")
        self.localapp = os.path.join(self.tmp, "LocalApp")
        for d in (self.pf, self.pfx86, self.localapp):
            os.makedirs(d, exist_ok=True)
        ep_bcheck._SIG_CACHE.clear()
        cp_bcheck._SIG_CACHE.clear()

    def tearDown(self):
        ep_bcheck._SIG_CACHE.clear()
        cp_bcheck._SIG_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_exe(self, path: str, content: bytes = b"MZ-browser") -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_accepts_chrome_and_edge_beta_dev_and_canary_sxs(self):
        cands = [
            (self._write_exe(os.path.join(self.pf, "Google", "Chrome Beta", "Application", "chrome.exe")),
             "C=US, S=California, O=Google LLC, CN=Google LLC"),
            (self._write_exe(os.path.join(self.localapp, "Google", "Chrome Dev", "Application", "chrome.exe")),
             "C=US, O=Google LLC, CN=Google LLC"),
            (self._write_exe(os.path.join(self.localapp, "Google", "Chrome SxS", "Application", "chrome.exe")),
             "C=US, O=Google LLC, CN=Google LLC"),
            (self._write_exe(os.path.join(self.pfx86, "Microsoft", "Edge Beta", "Application", "msedge.exe")),
             "C=US, S=Washington, O=Microsoft Corporation, CN=Microsoft Corporation"),
            (self._write_exe(os.path.join(self.pf, "Microsoft", "Edge Dev", "Application", "msedge.exe")),
             "O=Microsoft Corporation, CN=Microsoft Corporation"),
            (self._write_exe(os.path.join(self.localapp, "Microsoft", "Edge SxS", "Application", "msedge.exe")),
             "O=Microsoft Corporation, CN=Microsoft Corporation"),
        ]
        sigs = {os.path.normcase(os.path.realpath(p)): (True, subj) for p, subj in cands}

        def fake_sig(p):
            return sigs.get(os.path.normcase(os.path.realpath(p)), (False, ""))

        env = {"ProgramFiles": self.pf, "ProgramFiles(x86)": self.pfx86, "LOCALAPPDATA": self.localapp}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(ep_bcheck, "_read_authenticode_subject", side_effect=fake_sig), \
                mock.patch.object(cp_bcheck, "_read_authenticode_subject", side_effect=fake_sig):
            for p, _ in cands:
                self.assertTrue(deps.valid_browser(p), p)
                self.assertTrue(ep_bcheck.valid_browser(p), p)
                self.assertTrue(cp_bcheck.valid_browser(p), p)
                self.assertEqual(server._check_browser_path(p), os.path.realpath(p))
            # find_browser & _find_chrome discover Chrome Beta when Stable is absent
            self.assertEqual(os.path.normcase(deps.find_browser()), os.path.normcase(cands[0][0]))
            self.assertEqual(os.path.normcase(ep_chrome._find_chrome()), os.path.normcase(cands[0][0]))
            self.assertEqual(os.path.normcase(cp_chrome._find_chrome()), os.path.normcase(cands[0][0]))

    def test_exact_o_and_cn_comparison_rejects_substrings_missing_fields_and_multi_rdn(self):
        chrome_p = self._write_exe(os.path.join(self.localapp, "Google", "Chrome SxS", "Application", "chrome.exe"))
        edge_p = self._write_exe(os.path.join(self.localapp, "Microsoft", "Edge SxS", "Application", "msedge.exe"))
        env = {"ProgramFiles": self.pf, "ProgramFiles(x86)": self.pfx86, "LOCALAPPDATA": self.localapp}

        valid_chrome_subjects = [
            "C=US, S=California, L=Mountain View, O=Google LLC, CN=Google LLC",
            'O="Google LLC", CN="Google LLC", C=US',
            "OID.2.5.4.10=Google LLC, OID.2.5.4.3=Google LLC",
        ]
        invalid_chrome_subjects = [
            "CN=Google LLC",                                   # missing O
            "O=Google LLC",                                    # missing CN
            "O=Google LLC Fake, CN=Google LLC",                # O substring
            "O=Google LLC, CN=Google LLC Fake",                # CN substring
            "O=Attacker Inc, OU=Google LLC, CN=Google LLC",    # Google only in OU, wrong O
            "O=google llc, CN=Google LLC",                     # wrong case
            "O=Google LLC, O=Attacker LLC, CN=Google LLC",     # duplicate O RDN
            "O=Google LLC, CN=Google LLC, CN=Attacker LLC",    # duplicate CN RDN
            'O=Attacker LLC, CN="Attacker, O=Google LLC"',     # comma inside quotes
        ]
        invalid_edge_subjects = [
            "CN=Microsoft Corporation",
            "O=Microsoft Corporation Subsidiary, CN=Microsoft Corporation",
            "O=Microsoft Corporation, CN=Microsoft Corporation Beta",
        ]

        with mock.patch.dict(os.environ, env, clear=False):
            for subj in valid_chrome_subjects:
                ep_bcheck._SIG_CACHE.clear()
                cp_bcheck._SIG_CACHE.clear()
                with mock.patch.object(ep_bcheck, "_read_authenticode_subject", return_value=(True, subj)), \
                        mock.patch.object(cp_bcheck, "_read_authenticode_subject", return_value=(True, subj)):
                    self.assertTrue(ep_bcheck.valid_browser(chrome_p), subj)
                    self.assertTrue(cp_bcheck.valid_browser(chrome_p), subj)

            for subj in invalid_chrome_subjects:
                ep_bcheck._SIG_CACHE.clear()
                cp_bcheck._SIG_CACHE.clear()
                with mock.patch.object(ep_bcheck, "_read_authenticode_subject", return_value=(True, subj)), \
                        mock.patch.object(cp_bcheck, "_read_authenticode_subject", return_value=(True, subj)):
                    self.assertFalse(ep_bcheck.valid_browser(chrome_p), f"should reject {subj!r}")
                    self.assertFalse(cp_bcheck.valid_browser(chrome_p), f"should reject {subj!r}")

            for subj in invalid_edge_subjects:
                ep_bcheck._SIG_CACHE.clear()
                with mock.patch.object(ep_bcheck, "_read_authenticode_subject", return_value=(True, subj)):
                    self.assertFalse(ep_bcheck.valid_browser(edge_p), f"should reject {subj!r}")

    def test_cache_key_uses_vol_serial_and_file_id_and_launch_reverifies_after_10_minutes(self):
        chrome_p = self._write_exe(os.path.join(self.localapp, "Google", "Chrome", "Application", "chrome.exe"), b"MZ-11111")
        st1 = os.stat(chrome_p)
        calls = []
        current_sig = [(True, "O=Google LLC, CN=Google LLC")]
        now = [10_000.0]

        def fake_sig(p):
            calls.append(p)
            return current_sig[0]

        env = {
            "ProgramFiles": self.pf, "ProgramFiles(x86)": self.pfx86, "LOCALAPPDATA": self.localapp,
            "EP_CHROME": chrome_p, "CP_CHROME": chrome_p,
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(ep_bcheck, "_read_authenticode_subject", side_effect=fake_sig), \
                mock.patch.object(cp_bcheck, "_read_authenticode_subject", side_effect=fake_sig), \
                mock.patch.object(ep_bcheck.time, "time", side_effect=lambda: now[0]), \
                mock.patch.object(cp_bcheck.time, "time", side_effect=lambda: now[0]):
            self.assertTrue(ep_bcheck.valid_browser(chrome_p))
            self.assertEqual(len(calls), 1)
            cache_keys = list(ep_bcheck._SIG_CACHE.keys())
            self.assertEqual(cache_keys, [(int(st1.st_dev), int(st1.st_ino), int(st1.st_size), st1.st_mtime)])

            # Replace file with a brand-new inode having IDENTICAL size and IDENTICAL mtime -> file_id differs -> re-verified!
            replacement = os.path.join(self.tmp, "swapped_chrome.exe")
            with open(replacement, "wb") as f:
                f.write(b"MZ-22222")  # exact same length (8 bytes)
            os.utime(replacement, (st1.st_atime, st1.st_mtime))
            os.replace(replacement, chrome_p)
            st2 = os.stat(chrome_p)
            self.assertEqual(st2.st_size, st1.st_size)
            self.assertEqual(st2.st_mtime, st1.st_mtime)
            self.assertNotEqual(st2.st_ino, st1.st_ino)
            self.assertTrue(ep_bcheck.valid_browser(chrome_p))
            self.assertEqual(len(calls), 2)

            # Launch within 10 minutes (now + 300s) -> uses cached signature, no extra WinVerifyTrust call
            now[0] += 300.0
            fake_proc = mock.Mock(pid=54321)
            with mock.patch.object(ep_chrome.subprocess, "Popen", return_value=fake_proc) as ep_popen, \
                    mock.patch.object(ep_chrome, "alive", return_value={"Browser": "Chrome/130"}):
                r_launch1 = ep_chrome.launch(hidden=False)
                self.assertTrue(r_launch1["ok"], r_launch1)
                ep_popen.assert_called_once()
            self.assertEqual(len(calls), 2)

            # Advance past 10 minutes (> 600s since verification) and tamper signer -> launch re-verifies and REFUSES!
            now[0] += 305.0  # total 605s > 600s
            current_sig[0] = (False, "")
            with mock.patch.object(ep_chrome.subprocess, "Popen", return_value=fake_proc) as ep_popen:
                r_launch2 = ep_chrome.launch(hidden=False)
                self.assertFalse(r_launch2["ok"])
                ep_popen.assert_not_called()
            self.assertEqual(len(calls), 3)

            # Same 10-minute pre-launch re-verification in cp.chrome
            current_sig[0] = (True, "O=Google LLC, CN=Google LLC")
            cp_bcheck._SIG_CACHE.clear()
            with mock.patch.object(cp_chrome.subprocess, "Popen", return_value=fake_proc) as cp_popen, \
                    mock.patch.object(cp_chrome, "alive", return_value={"Browser": "Chrome/130"}):
                self.assertTrue(cp_chrome.launch(hidden=False)["ok"])
                cp_popen.assert_called_once()
            calls_before = len(calls)
            now[0] += 601.0
            current_sig[0] = (False, "")
            with mock.patch.object(cp_chrome.subprocess, "Popen", return_value=fake_proc) as cp_popen:
                self.assertFalse(cp_chrome.launch(hidden=False)["ok"])
                cp_popen.assert_not_called()
            self.assertEqual(len(calls), calls_before + 1)


# ====================================================================== D3: Update manifest/PE, uninstall wait & reg, run-<hash>.json
class TestD3UpdateUninstallAndRunJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3d-inst-")
        self.localapp = os.path.join(self.tmp, "LocalApp")
        os.makedirs(self.localapp, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_zip(self, zpath: str, entries: dict[str, bytes]) -> str:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return zpath

    def test_manifest_sha256_verified_before_swap_and_fallback_checks_pe_and_internal(self):
        pe1 = _make_valid_pe(b"v1-exe")
        ep1 = b"# ep v1\n"
        pe2 = _make_valid_pe(b"v2-exe")
        ep2 = b"# ep v2\n"
        manifest2 = json.dumps({
            "files": {
                "EntryPilot.exe": hashlib.sha256(pe2).hexdigest(),
                "_internal/ep.py": hashlib.sha256(ep2).hexdigest(),
            }
        }).encode("utf-8")

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.localapp, "EP_TEST_NO_SHELL": "1"}, clear=False):
            # Fallback mode (no manifest.json): rejects non-PE exe and rejects missing _internal\
            z_not_pe = self._make_zip(os.path.join(self.tmp, "not_pe.zip"),
                                      {"EntryPilot.exe": b"MZ-not-a-pe-header", "_internal/ep.py": ep1})
            with self.assertRaises(RuntimeError):
                installer.install_zip(z_not_pe)

            z_no_internal = self._make_zip(os.path.join(self.tmp, "no_int.zip"),
                                           {"EntryPilot.exe": pe1})
            with self.assertRaises(RuntimeError):
                installer.install_zip(z_no_internal)

            # Valid fallback install (valid PE + _internal\)
            z1 = self._make_zip(os.path.join(self.tmp, "v1.zip"),
                                {"EntryPilot.exe": pe1, "_internal/ep.py": ep1})
            exe = installer.install_zip(z1)
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), pe1)

            # Upgrade with tampered file vs manifest.json -> rejected BEFORE swapping; v1 untouched!
            z2_tampered = self._make_zip(os.path.join(self.tmp, "v2_bad.zip"), {
                "EntryPilot.exe": pe2,
                "_internal/ep.py": b"# TAMPERED\n",
                "manifest.json": manifest2,
            })
            with self.assertRaises(RuntimeError):
                installer.install_zip(z2_tampered)
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), pe1)

            # Upgrade missing a required file listed in manifest.json -> rejected; v1 untouched!
            z2_missing = self._make_zip(os.path.join(self.tmp, "v2_missing.zip"), {
                "EntryPilot.exe": pe2,
                "_internal/other.txt": b"x",
                "manifest.json": manifest2,
            })
            with self.assertRaises(RuntimeError):
                installer.install_zip(z2_missing)
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), pe1)

            # Upgrade with matching manifest.json -> succeeds!
            z2_ok = self._make_zip(os.path.join(self.tmp, "v2_ok.zip"), {
                "EntryPilot.exe": pe2,
                "_internal/ep.py": ep2,
                "manifest.json": manifest2,
            })
            installer.install_zip(z2_ok)
            with open(exe, "rb") as f:
                self.assertEqual(f.read(), pe2)

    def test_hashed_run_json_preferred_with_fallback_to_legacy_run_json(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.localapp}, clear=False):
            inst = paths.install_root()
            app_folder = os.path.join(inst, "app")
            os.makedirs(app_folder, exist_ok=True)
            inside_exe = os.path.join(app_folder, "EntryPilot.exe")
            with open(inside_exe, "wb") as f:
                f.write(_make_valid_pe(b"app"))

            expected_tag = hashlib.sha256(os.path.normpath(os.path.realpath(app_folder)).lower().encode("utf-8")).hexdigest()[:8]
            hashed_run = os.path.join(inst, f"run-{expected_tag}.json")
            legacy_run = os.path.join(inst, "run.json")

            # Fallback to run.json when run-<8hex>.json does not exist
            with open(legacy_run, "w", encoding="utf-8") as f:
                json.dump({"pid": 11111, "port": 5001}, f)
            self.assertEqual(os.path.normcase(installer._run_json_path(inst)), os.path.normcase(legacy_run))
            self.assertEqual(installer.running_instance(inst)["pid"], 11111)

            # When run-<8hex>.json exists, it takes precedence over legacy run.json
            with open(hashed_run, "w", encoding="utf-8") as f:
                json.dump({"pid": 22222, "port": 5002}, f)
            mtime = os.stat(hashed_run).st_mtime
            self.assertEqual(os.path.normcase(installer._run_json_path(inst)), os.path.normcase(hashed_run))
            self.assertEqual(installer.running_instance(inst)["pid"], 22222)

            with mock.patch.object(installer, "_process_info", side_effect=lambda pid: (inside_exe, mtime - 5.0) if pid == 22222 else None), \
                    mock.patch.object(installer.subprocess, "run") as mrun:
                self.assertTrue(installer.stop_running_app())
                self.assertEqual(mrun.call_args[0][0][1:], ["/PID", "22222", "/T", "/F"])

    def test_uninstall_waits_20s_refuses_if_still_running_and_deletes_registry_only_after_files_gone(self):
        real_inst = os.path.join(self.localapp, "EntryPilot")
        app_folder = os.path.join(real_inst, "app")
        os.makedirs(app_folder, exist_ok=True)
        fake_exe = os.path.join(app_folder, "EntryPilot.exe")
        with open(fake_exe, "wb") as f:
            f.write(_make_valid_pe(b"uninst"))

        tag = hashlib.sha256(os.path.normpath(os.path.realpath(app_folder)).lower().encode("utf-8")).hexdigest()[:8]
        hashed_run = os.path.join(real_inst, f"run-{tag}.json")
        with open(hashed_run, "w", encoding="utf-8") as f:
            json.dump({"pid": 77777}, f)
        run_mtime = os.stat(hashed_run).st_mtime

        # 1) Active PID never exits within 20 s -> waits 20 s, shows refusal message, refuses without spawning helper
        slept = []
        msgs = []
        env_shell = {"LOCALAPPDATA": self.localapp}
        with mock.patch.dict(os.environ, env_shell, clear=False), \
                mock.patch.object(installer, "recorded_install_root", return_value=real_inst), \
                mock.patch.object(paths, "frozen", return_value=False), \
                mock.patch.object(installer, "_process_info", return_value=(fake_exe, run_mtime - 5.0)), \
                mock.patch.object(installer.time, "sleep", side_effect=lambda s: slept.append(s)), \
                mock.patch.object(installer, "message", side_effect=lambda txt, title="", error=False: msgs.append((txt, error))), \
                mock.patch.object(installer, "shortcut_paths", return_value=[]), \
                mock.patch.object(installer.subprocess, "Popen") as popen:
            os.environ.pop("EP_TEST_NO_SHELL", None)
            try:
                r_busy = installer.uninstall()
            finally:
                os.environ["EP_TEST_NO_SHELL"] = "1"
        self.assertFalse(r_busy["ok"])
        self.assertAlmostEqual(sum(slept), 20.0, places=2)
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0][1])
        popen.assert_not_called()

        # 2) Active PID exits after 3 checks -> uninstall proceeds as soon as it exits!
        checks = [0]

        def exit_soon(pid):
            checks[0] += 1
            return (fake_exe, run_mtime - 5.0) if checks[0] <= 3 else None

        slept2 = []
        test_reg_key = rf"Software\EntryPilotR3Test_{secrets.token_hex(6)}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, test_reg_key) as k:
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, os.path.realpath(real_inst))

        def cleanup_reg():
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_reg_key)
            except OSError:
                pass

        self.addCleanup(cleanup_reg)

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.localapp, "EP_TEST_NO_SHELL": "1"}, clear=False), \
                mock.patch.object(installer, "recorded_install_root", return_value=real_inst), \
                mock.patch.object(paths, "frozen", return_value=False), \
                mock.patch.object(installer, "_process_info", side_effect=exit_soon), \
                mock.patch.object(installer.time, "sleep", side_effect=lambda s: slept2.append(s)), \
                mock.patch.object(installer.subprocess, "Popen") as popen, \
                mock.patch.object(installer, "threading_exit"):
            r_ok = installer.uninstall()
            self.assertTrue(r_ok["ok"], r_ok)
            self.assertLess(sum(slept2), 5.0)
            helper_cmd = popen.call_args[0][0]
            helper_cwd = popen.call_args[1]["cwd"]
            self.addCleanup(shutil.rmtree, helper_cwd, True)

        # Registry key MUST still exist before helper finishes deleting files!
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, test_reg_key):
            pass

        # 3) Helper refuses (e.g. reparse swap) -> files not deleted -> registry key STILL NOT removed!
        helper_py = helper_cmd[3]
        outside_keep = os.path.join(self.tmp, "keep_dir")
        os.makedirs(outside_keep, exist_ok=True)
        junc_target = os.path.join(self.tmp, "sub", "junc_target")
        os.makedirs(os.path.dirname(junc_target), exist_ok=True)
        _make_junction(junc_target, outside_keep)
        self.addCleanup(_cleanup_path, junc_target)

        helper_env = dict(os.environ, EP_UNINSTALL_REG_KEY=test_reg_key)
        r_fail = subprocess.run([sys.executable, "-X", "utf8", helper_py, "0", junc_target],
                                env=helper_env, capture_output=True, timeout=15)
        self.assertNotEqual(r_fail.returncode, 0)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, test_reg_key):
            pass

        # 4) Helper succeeds deleting real_inst -> files really gone -> registry key IS removed!
        r_del = subprocess.run([sys.executable, "-X", "utf8", helper_py, "0", os.path.realpath(real_inst)],
                               env=helper_env, capture_output=True, timeout=20)
        self.assertEqual(r_del.returncode, 0)
        self.assertFalse(os.path.exists(real_inst))
        with self.assertRaises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, test_reg_key)

        # 5) Self-uninstall from UI (run-<hash>.json pid == os.getpid()) does NOT deadlock waiting for itself
        os.makedirs(app_folder, exist_ok=True)
        with open(fake_exe, "wb") as f:
            f.write(_make_valid_pe(b"self-uninst"))
        with open(hashed_run, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid()}, f)
        slept_self = []
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.localapp, "EP_TEST_NO_SHELL": "1"}, clear=False), \
                mock.patch.object(installer, "recorded_install_root", return_value=real_inst), \
                mock.patch.object(paths, "frozen", return_value=False), \
                mock.patch.object(installer.time, "sleep", side_effect=lambda s: slept_self.append(s)), \
                mock.patch.object(installer.subprocess, "Popen") as popen_self, \
                mock.patch.object(installer, "threading_exit"):
            r_self = installer.uninstall()
            self.assertTrue(r_self["ok"], r_self)
            self.assertEqual(slept_self, [])
            self.addCleanup(shutil.rmtree, popen_self.call_args[1]["cwd"], True)


# ====================================================================== D4: Zip extraction caps
class TestD4ZipExtractionLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-r3d-zip-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_limits_and_entry_count_cap(self):
        self.assertEqual(installer.APP_ZIP_MAX_BYTES, 2 * 1024 * 1024 * 1024)
        self.assertEqual(installer.NODE_ZIP_MAX_BYTES, 200 * 1024 * 1024)
        self.assertEqual(installer.MAX_ZIP_ENTRIES, 20_000)
        self.assertEqual(installer.MAX_ZIP_RATIO, 200.0)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for i in range(6):
                zf.writestr(f"f{i}.txt", b"ok")
        buf.seek(0)
        dest = os.path.join(self.tmp, "out_entries")
        with zipfile.ZipFile(buf, "r") as zf:
            with self.assertRaises(ValueError) as ctx:
                installer.safe_extract_zip(zf, dest, max_entries=5)
        self.assertIn("entry count", str(ctx.exception))

    def test_total_uncompressed_size_cap_for_app_and_node(self):
        buf = io.BytesIO()
        # Incompressible random bytes so ratio stays ~1:1 while total size exceeds cap
        chunk = secrets.token_bytes(1024)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a.bin", chunk)
            zf.writestr("b.bin", chunk)
            zf.writestr("c.bin", chunk)
        raw_zip = buf.getvalue()

        dest = os.path.join(self.tmp, "out_size")
        with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
            with self.assertRaises(ValueError) as ctx:
                installer.safe_extract_zip(zf, dest, max_total_bytes=2048)
        self.assertIn("uncompressed size", str(ctx.exception))

        # Node installer enforces NODE_ZIP_MAX_BYTES
        ver = "v22.0.0"
        zname = f"node-{ver}-win-x64.zip"
        sha = hashlib.sha256(raw_zip).hexdigest()

        def fake_get(url, timeout=60):
            if url.endswith("index.json"):
                return json.dumps([{"version": ver, "lts": "Jod", "files": ["win-x64-zip"]}]).encode()
            return f"{sha}  {zname}\n".encode()

        def fake_dl(url, dst, *a):
            with open(dst, "wb") as f:
                f.write(raw_zip)

        with mock.patch.dict(os.environ, {"EP_APP_DIR": os.path.join(self.tmp, "node_cap_app")}, clear=False), \
                mock.patch.object(deps, "node_ok", return_value=False), \
                mock.patch.object(deps, "_get", side_effect=fake_get), \
                mock.patch.object(deps, "_download", side_effect=fake_dl), \
                mock.patch.object(installer, "NODE_ZIP_MAX_BYTES", 2048):
            with self.assertRaises(deps.StepError):
                deps.install_node(lambda *a: None)

    def test_rejects_compression_ratio_above_200_to_1_and_spoofed_header_sizes(self):
        # 1) Normal zip bomb: 200 KB of zeros compresses to ~210 bytes (~975:1 > 200:1)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.writestr("bomb.bin", b"\x00" * 200_000)
        buf.seek(0)
        dest1 = os.path.join(self.tmp, "out_bomb")
        with zipfile.ZipFile(buf, "r") as zf:
            with self.assertRaises(ValueError) as ctx:
                installer.safe_extract_zip(zf, dest1)
        self.assertIn("ratio", str(ctx.exception).lower())
        self.assertFalse(os.path.exists(os.path.join(dest1, "bomb.bin")))

        # 2) Spoofed ZipInfo header claiming file_size == compress_size while stream expands > 200:1
        buf.seek(0)
        dest2 = os.path.join(self.tmp, "out_spoof")
        with zipfile.ZipFile(buf, "r") as zf:
            info = zf.infolist()[0]
            info.file_size = info.compress_size  # lie in ZipInfo metadata
            with mock.patch.object(zf, "open", return_value=io.BytesIO(b"\x00" * 200_000)):
                with self.assertRaises(ValueError) as ctx2:
                    installer.safe_extract_zip(zf, dest2)
        self.assertIn("ratio", str(ctx2.exception).lower())
        self.assertFalse(os.path.exists(os.path.join(dest2, "bomb.bin")))


def tearDownModule():
    shutil.rmtree(_BOOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
