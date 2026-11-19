# PyInstaller spec: one _internal folder, two executables.
#   EntryPilot.exe  windowed app (setup wizard, dashboard, local server)
#   python.exe      console twin used as `python` by the engine and the AI agents (app/pyshim.py)
import importlib.util
import marshal
import os
import struct
from PyInstaller.building.utils import replace_filename_in_code_object
from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

SKIP_DATA_EXTS = (".pyc", ".pyo", ".pdb")


def _is_bytecode_path(path: str) -> bool:
    norm = os.path.normcase(os.path.normpath(path))
    if "__pycache__" in norm.split(os.sep):
        return True
    return norm.endswith(SKIP_DATA_EXTS)


def _clean_datas(entries):
    out = []
    for src, dst in entries:
        if _is_bytecode_path(src) or _is_bytecode_path(dst):
            continue
        if os.path.isdir(src):
            for dp, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d.lower() != "__pycache__"]
                rel = os.path.relpath(dp, src)
                target = dst if rel == "." else os.path.join(dst, rel)
                for f in files:
                    fp = os.path.join(dp, f)
                    if not _is_bytecode_path(fp):
                        out.append((fp, target))
        elif os.path.isfile(src):
            out.append((src, dst))
    return out


def _strip_code_paths(pure_toc, code_cache):
    if not code_cache:
        return
    for name, src_path, _ in pure_toc:
        co = code_cache.get(name)
        if co is None:
            continue
        base = os.path.splitext(os.path.basename(src_path or ""))[0]
        rel = os.path.join(*name.split("."), "__init__.py") if base == "__init__" else os.path.join(*name.split(".")) + ".py"
        code_cache[name] = replace_filename_in_code_object(co, rel)


def _strip_script_paths(scripts_toc, code_cache, work_dir):
    out = []
    out_dir = os.path.join(work_dir, "localscripts")
    for idx, (dest_name, src_path, typecode) in enumerate(scripts_toc):
        src_base = os.path.basename(src_path or dest_name or "script.py")
        if not os.path.splitext(src_base)[1]:
            src_base += ".py"
        clean_dest = os.path.splitext(os.path.basename(dest_name or src_base))[0]
        if code_cache and dest_name in code_cache and code_cache[dest_name] is not None:
            code_cache[dest_name] = replace_filename_in_code_object(code_cache[dest_name], src_base)
        if src_path and os.path.isfile(src_path):
            if src_path.lower().endswith(".pyc"):
                with open(src_path, "rb") as fh:
                    raw_pyc = fh.read()
                hdr = raw_pyc[:16]
                co = marshal.loads(raw_pyc[16:])
            else:
                with open(src_path, "rb") as fh:
                    src_bytes = fh.read()
                co = compile(src_bytes, src_base, "exec")
                hdr = importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, 0, len(src_bytes) & 0xFFFFFFFF)
            co = replace_filename_in_code_object(co, src_base)
            os.makedirs(out_dir, exist_ok=True)
            pyc_out = os.path.join(out_dir, f"{idx}_{clean_dest}.pyc")
            with open(pyc_out, "wb") as fh:
                fh.write(hdr + marshal.dumps(co))
            out.append((clean_dest, pyc_out, typecode))
        else:
            out.append((clean_dest, src_path, typecode))
    return out


ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
hidden = collect_submodules("ep") + collect_submodules("cp") + collect_submodules("app") + ["pymupdf", "tzdata", "openpyxl", "bs4", "playwright.sync_api", "playwright.async_api"]
# ep = event engine, cp = contest engine (cp.py is its command line, prompts_contest its agent prompts)
raw_datas = [(os.path.join(ROOT, "ep"), "ep"), (os.path.join(ROOT, "ep.py"), "."), (os.path.join(ROOT, "prompts"), "prompts"),
             (os.path.join(ROOT, "cp"), "cp"), (os.path.join(ROOT, "cp.py"), "."), (os.path.join(ROOT, "prompts_contest"), "prompts_contest"),
             (os.path.join(ROOT, "ui"), "ui"), (os.path.join(ROOT, "assets", "runtime"), os.path.join("assets", "runtime"))]
raw_datas += collect_data_files("playwright") + collect_data_files("tzdata")
datas = _clean_datas(raw_datas)

a = Analysis([os.path.join(ROOT, "app", "main.py")], pathex=[ROOT], hiddenimports=hidden, datas=datas,
             excludes=["tkinter", "matplotlib", "numpy", "PIL", "pytest", "torch", "torchvision", "torchaudio", "scipy", "pandas", "jedi", "IPython", "sklearn", "tensorflow", "cv2", "onnxruntime", "transformers"], noarchive=False)
_workpath = workpath if "workpath" in globals() else CONF["workpath"]
_localpycs = os.path.normcase(os.path.normpath(os.path.join(_workpath, "localpycs"))) + os.sep
a.datas = [
    e for e in a.datas
    if "__pycache__" not in os.path.normcase(e[0]).split(os.sep)
    and "__pycache__" not in os.path.normcase(e[1]).split(os.sep)
    and (
        not (_is_bytecode_path(e[0]) or _is_bytecode_path(e[1]))
        or os.path.normcase(os.path.normpath(e[1])).startswith(_localpycs)
        or os.path.normcase(os.path.normpath(e[0])).startswith(_localpycs)
    )
]
_strip_code_paths(a.pure, CONF.get("code_cache", {}).get(id(a.pure)))
a.scripts = _strip_script_paths(a.scripts, CONF.get("code_cache", {}).get(id(a.scripts)), _workpath)
pyz = PYZ(a.pure)
icon = os.path.join(ROOT, "assets", "icon.ico")
icon = icon if os.path.exists(icon) else None
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="EntryPilot", console=False, icon=icon, upx=False)
py = EXE(pyz, a.scripts, [], exclude_binaries=True, name="python", console=True, icon=icon, upx=False)
coll = COLLECT(gui, py, a.binaries, a.datas, name="EntryPilot", upx=False)
