"""Draw the step-by-step pictures for "make the Telegram bot yourself" (ui/guide/tg1.png ... tg5.png)
and "make the Google app password yourself" (ui/guide/gm1.png, gm2.png).

The pictures are example screens drawn in HTML (layout like Telegram Desktop, BotFather's real English replies,
a made-up bot name and a fake token; the Google pages with a fake password), rendered with headless Chrome. They are labelled "예시 화면" in the window.

usage: python build\\guide\\make_guide.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(ROOT, "ui", "guide")
sys.path.insert(0, ROOT)

CSS = """
* { box-sizing: border-box; margin: 0; }
body { width: 720px; height: 440px; font: 14px/1.45 "Segoe UI", "Malgun Gothic", sans-serif; background: #fff; overflow: hidden; }
.app { display: flex; height: 100%; }
.side { width: 230px; border-right: 1px solid #e5e5e5; background: #fff; }
.search { margin: 10px; height: 34px; border-radius: 17px; background: #f1f1f1; padding: 0 14px; display: flex; align-items: center; color: #222; }
.row { display: flex; gap: 10px; padding: 8px 10px; align-items: center; }
.row.sel { background: #3390ec; color: #fff; }
.av { width: 40px; height: 40px; border-radius: 50%; background: #5aa9e6; color: #fff; display: grid; place-items: center; font-weight: 700; flex: none; }
.name { font-weight: 600; } .sub { font-size: 12px; opacity: .7; }
.ck { display: inline-grid; place-items: center; width: 15px; height: 15px; border-radius: 50%; background: #3390ec; color: #fff; font-size: 10px; margin-left: 4px; }
.row.sel .ck { background: #fff; color: #3390ec; }
.chat { flex: 1; display: flex; flex-direction: column; background: #dfe8d0; }
.head { height: 52px; background: #fff; border-bottom: 1px solid #e5e5e5; display: flex; align-items: center; gap: 10px; padding: 0 14px; }
.msgs { flex: 1; padding: 14px; display: flex; flex-direction: column; gap: 8px; justify-content: flex-end; }
.m { max-width: 78%; padding: 7px 11px; border-radius: 12px; background: #fff; white-space: pre-wrap; font-size: 13px; }
.m.me { align-self: flex-end; background: #e3fcc9; }
.input { height: 48px; background: #fff; border-top: 1px solid #e5e5e5; display: flex; align-items: center; padding: 0 14px; color: #999; }
.start { height: 48px; background: #fff; border-top: 1px solid #e5e5e5; display: grid; place-items: center; color: #3390ec; font-weight: 700; letter-spacing: .06em; }
.mark { outline: 3px solid #ff3b30; outline-offset: 2px; border-radius: 6px; }
.tip { position: absolute; background: #ff3b30; color: #fff; font: 600 13px "Malgun Gothic", sans-serif; padding: 6px 10px; border-radius: 8px; }
.ex { position: absolute; right: 8px; bottom: 6px; font: 11px "Malgun Gothic", sans-serif; color: #888; background: rgba(255,255,255,.8); padding: 1px 6px; border-radius: 4px; }
code { font-family: Consolas, monospace; }
.g { flex: 1; background: #f0f4f9; position: relative; padding: 18px 40px; font-family: "Malgun Gothic", "Segoe UI", sans-serif; }
.g .top { color: #444; font-size: 15px; margin-bottom: 12px; }
.g .card { background: #fff; border-radius: 16px; padding: 20px 24px; max-width: 560px; }
.g h1 { font-size: 22px; font-weight: 400; margin-bottom: 6px; }
.g .help { color: #5f6368; font-size: 13px; margin-bottom: 14px; }
.g .fld { height: 46px; border: 1px solid #747775; border-radius: 4px; padding: 0 14px; display: flex; align-items: center; color: #1f1f1f; margin-bottom: 14px; }
.g .btn { display: inline-block; background: #0b57d0; color: #fff; border-radius: 20px; padding: 9px 24px; font-weight: 600; }
.g .item { border-top: 1px solid #e5e5e5; padding: 10px 0; color: #444; font-size: 13px; }
.dlg { background: #fff; border-radius: 24px; padding: 24px 28px; width: 420px; margin: 24px auto 0; box-shadow: 0 4px 24px rgba(0,0,0,.25); }
.dlg h1 { font-size: 20px; font-weight: 400; margin-bottom: 12px; }
.pw { background: #f0f4f9; border-radius: 8px; padding: 14px; font: 600 24px Consolas, monospace; letter-spacing: .12em; text-align: center; margin-bottom: 12px; }
.dlg .help { color: #5f6368; font-size: 12.5px; margin-bottom: 14px; }
.dlg .btn { display: inline-block; float: right; background: #0b57d0; color: #fff; border-radius: 20px; padding: 8px 22px; font-weight: 600; }
"""

BF = '<div class="av" style="background:#2a9ef1">BF</div>'
TOKEN = "1234567890:AAExampleTokenOnlyForThisPicture_x"


def side(sel=True, search="BotFather"):
    return f"""<div class="side"><div class="search {'mark' if not sel else ''}">{search}</div>
      <div class="row {'sel' if sel else ''}">{BF}<div><div class="name">BotFather<span class="ck">✓</span></div><div class="sub">@BotFather</div></div></div>
      <div class="row"><div class="av" style="background:#9b8be0">B</div><div><div class="name">BotFather Fan</div><div class="sub">@botfather_fan_123</div></div></div></div>"""


def chat(msgs, bottom='<div class="input">Write a message...</div>', title="BotFather", sub="bot", av=BF, check=True):
    m = "".join(f'<div class="m {"me" if who == "me" else ""} {cls}">{t}</div>' for who, t, cls in msgs)
    ck = '<span class="ck">✓</span>' if check else ""
    return f"""<div class="chat"><div class="head">{av}<div><div class="name">{title}{ck}</div><div class="sub">{sub}</div></div></div>
      <div class="msgs">{m}</div>{bottom}</div>"""


MY_AV = '<div class="av" style="background:#e0a33b">내</div>'
MY_SIDE = f"""<div class="side"><div class="search">Search</div>
      <div class="row sel">{MY_AV}<div><div class="name">내 응모 알림</div><div class="sub">@my_entry_alert_bot</div></div></div>
      <div class="row">{BF}<div><div class="name">BotFather<span class="ck">✓</span></div><div class="sub">Done! Congratulations...</div></div></div></div>"""

FAKE_PW = "abcd efgh ijkl mnop"  # fake, only for the picture
PAGES = {
    "gm1": '''<div class="g"><div class="top">Google 계정 &gt; 보안 &gt; 앱 비밀번호</div>
      <div class="card"><h1>앱 비밀번호</h1><div class="help">앱 이름을 쓰고 만들기를 누릅니다.</div>
        <div class="fld mark">Entry Pilot</div><span class="btn mark">만들기</span></div>
      <div class="tip" style="left:60px;top:300px">앱 이름에 Entry Pilot 을 쓰고 만들기를 누릅니다</div></div>''',
    "gm2": '''<div class="g" style="background:#7a7d82"><div class="dlg"><h1>앱 비밀번호가 생성되었습니다</h1>
        <div class="pw mark">''' + FAKE_PW + '''</div>
        <div class="help">이 비밀번호를 복사하면 Entry Pilot이 알아서 읽습니다. 이 창을 닫으면 다시 볼 수 없습니다.</div>
        <span class="btn">완료</span><div style="clear:both"></div></div>
      <div class="tip" style="left:60px;top:370px">빨간 칸의 16자리를 눌러 선택하고 복사(Ctrl+C)합니다</div></div>''',
    "tg1": side(sel=False) + chat([], bottom='<div class="input"></div>', title="", sub="", av="", check=False) +
           '<div class="tip" style="left:250px;top:120px">검색창에 BotFather 입력,<br>파란 체크(✓)가 있는 @BotFather 선택</div>',
    "tg2": side() + chat([("bot", "BotFather can help you create and manage Telegram bots.", ""),
                          ("me", "/newbot", "mark"),
                          ("bot", "Alright, a new bot. How are we going to call it? Please choose a name for your bot.", "")]) +
           '<div class="tip" style="left:250px;top:64px">/newbot 을 보냅니다</div>',
    "tg3": side() + chat([("me", "/newbot", ""),
                          ("bot", "Alright, a new bot. How are we going to call it? Please choose a name for your bot.", ""),
                          ("me", "내 응모 알림", "mark"),
                          ("bot", "Good. Now let's choose a username for your bot. It must end in `bot`. "
                                  "Like this, for example: TetrisBot or tetris_bot.", "")]) +
           '<div class="tip" style="left:250px;top:64px">봇 이름(아무 이름)을 보냅니다</div>',
    "tg4": side() + chat([("me", "my_entry_alert_bot", "mark"),
                          ("bot", "Done! Congratulations on your new bot. You will find it at t.me/my_entry_alert_bot. ...\n\n"
                                  f"Use this token to access the HTTP API:\n<code class=\"mark\" style=\"padding:1px 3px\">{TOKEN}</code>\n"
                                  "Keep your token secure and store it safely, it can be used by anyone to control your bot.", "")]) +
           '<div class="tip" style="left:250px;top:64px">끝이 bot 인 아이디를 보내면 토큰이 나옵니다</div>'
           '<div class="tip" style="left:250px;top:110px">아래 빨간 칸의 토큰을 눌러 복사</div>',
    "tg5": MY_SIDE + chat([("bot", "What can this bot do?", "")],
                          bottom='<div class="start mark">START</div>', title="내 응모 알림", sub="bot", av=MY_AV, check=False) +
           '<div class="tip" style="left:250px;top:64px">Entry Pilot이 봇 대화창을 열면 아래 START를 누릅니다</div>',
}


def main():
    from app import deps
    chrome = deps.find_browser()
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(HERE, "_html")
    os.makedirs(tmp, exist_ok=True)
    for name, body in PAGES.items():
        html = os.path.join(tmp, name + ".html")
        with open(html, "w", encoding="utf-8") as f:
            f.write(f'<!doctype html><meta charset="utf-8"><style>{CSS}</style><div class="app">{body}</div><div class="ex">예시 화면</div>')
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=720,440",
                        "--force-device-scale-factor=2", f"--user-data-dir={os.path.join(tmp, 'prof')}",
                        f"--screenshot={os.path.join(OUT, name + '.png')}", "file:///" + html.replace(os.sep, "/")],
                       capture_output=True, timeout=60)
        print(name, os.path.isfile(os.path.join(OUT, name + ".png")))


if __name__ == "__main__":
    main()
