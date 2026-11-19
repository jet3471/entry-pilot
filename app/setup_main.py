"""EntryPilot-Setup.exe: the single file people download.

Shows a small window, unpacks the bundled app.zip into %LOCALAPPDATA%\\EntryPilot\\app (per user, no admin), makes
Start menu and desktop shortcuts, adds the Windows "Apps" entry, then starts Entry Pilot, whose own window continues
with the setup wizard (AI tools, logins, Telegram, profile).
"""
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

from app import installer, paths

INK, MUTED, LINE = "#020817", "#64748b", "#e2e8f0"


def bundled_zip() -> str:
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "app.zip")


def main():
    root = tk.Tk()
    root.title("Entry Pilot 설치")
    root.configure(bg="white")
    root.resizable(False, False)
    w, h = 460, 230
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    try:
        ico = os.path.join(getattr(sys, "_MEIPASS", ""), "icon.ico")
        if os.path.exists(ico):
            root.iconbitmap(ico)
    except tk.TclError:
        pass
    font = ("Malgun Gothic", 10)
    tk.Label(root, text="Entry Pilot", font=("Malgun Gothic", 17, "bold"), fg=INK, bg="white").place(x=32, y=28)
    tk.Label(root, text=f"버전 {paths.VERSION}. 관리자 권한 없이 현재 Windows 계정에 설치합니다", font=("Malgun Gothic", 9),
             fg=MUTED, bg="white").place(x=34, y=66)
    style = ttk.Style(root)
    style.theme_use("default")
    style.configure("EP.Horizontal.TProgressbar", troughcolor="#eef2f7", background="#2563eb", thickness=6,
                    borderwidth=0)
    bar = ttk.Progressbar(root, style="EP.Horizontal.TProgressbar", length=w - 64, maximum=100)
    bar.place(x=32, y=118)
    msg = tk.Label(root, text="준비 중", font=font, fg=MUTED, bg="white")
    msg.place(x=32, y=136)
    state = {"pct": 0, "text": "준비 중", "done": False, "error": "", "exe": ""}

    def report(pct, text):
        state.update(pct=pct, text=text)

    def work():
        try:
            state["exe"] = installer.install_zip(bundled_zip(), report)
            state["done"] = True
        except Exception as e:
            state["error"] = str(e) or type(e).__name__

    def tick():
        bar["value"] = state["pct"]
        msg["text"] = state["text"]
        if state["error"]:
            msg["text"] = "설치 실패: " + state["error"]
            msg["fg"] = "#ef4444"
            tk.Button(root, text="닫기", command=root.destroy, relief="flat", bg=INK, fg="white", padx=16).place(x=w - 100, y=180)
            return
        if state["done"]:
            msg["text"] = "설치 완료. Entry Pilot을 엽니다."
            subprocess.Popen([state["exe"]], cwd=os.path.dirname(state["exe"]), close_fds=True)
            root.after(1500, root.destroy)
            return
        root.after(150, tick)

    threading.Thread(target=work, daemon=True).start()
    root.after(150, tick)
    root.mainloop()


if __name__ == "__main__":
    main()
