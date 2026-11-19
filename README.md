# Entry Pilot

**English** | [한국어](README.ko.md)

> **Beta.** Prize-event entry is in daily use. Contest mode, e-mail notices and the AI-made app password are still being checked in real use, so expect rough edges and please report problems in Issues.

Entry Pilot is a Windows app that enters online prize events and contests for you. It uses the AI subscriptions you already have (Claude, ChatGPT/Codex, or a free Gemini API key): the AI finds events and contests on Korean sites, checks the conditions and fills in the entry forms. For contests it also writes the entry, and nothing is submitted until you approve it in the app. Things only a person can do, such as identity checks or payments, come to you on Telegram, by e-mail or in the app.

![Entry Pilot setup: install, choose the AIs, log in, notices, contest filters, the dashboard and a contest entry waiting for approval](docs/media/demo.webp)

The screens are the real app window; the accounts, codes and numbers in it are made-up examples.

---

## 1. Install

1. Download `EntryPilot-Setup.exe` from [Releases](../../releases).
2. Run it. It installs for your Windows account only; no administrator rights are needed.
3. The app window opens and walks you through the setup.

The file is not code-signed yet, so Windows shows "Windows protected your PC" once: **More info** → **Run anyway**.

## 2. Setup (8 steps)

1. **Choose the AIs and what to enter**: prize events, contests, or both. Claude (Claude Pro or higher), Codex (ChatGPT Plus or higher), Gemini (free API key, for simple tasks). You can log in up to five accounts per AI; when one account reaches its usage limit, the next one is used.
2. **Automatic install**: Node.js, Git (for Claude) and the official Claude Code / Codex / Gemini CLIs go into the Entry Pilot folder. Takes 3 to 10 minutes.
3. **AI login**: the login windows open one after another. For Codex the code is shown large in the app and already copied.
4. **Notices**: Telegram (a bot made by you with example pictures, or by the AI after you agree), e-mail (sent from a Gmail account with an app password, made by you with example pictures or by the AI after you agree; notices can go to any address), or none.
5. **Your details**: the information entry forms ask for. Saved only on this PC; only the fields a form asks for are given to the AI.
6. **Site logins**: log in once to Google, Naver, Kakao and others in the entry browser.
7. **Contest filters**: which fields to enter (writing and ideas, AI and development, photo, video and more) and how to handle offline conditions (on-site writing, on-site final presentation, mail-only entry, award ceremony attendance). You can change these later in Settings.
8. **Start**.

After that the dashboard shows entries, things to check, and a live log.

## 3. Contests

The 공모전 tab lists the contests the AI found for you. For each one the AI first writes an analysis (prize, deadline, where it goes, cautions); you press **통과** to let it write the entry, **수정** with a note to change the direction, or **안 함** to skip it. When the entry is written you see exactly what will be sent and press **통과** again; only then does the AI submit it. Contests that need something the app cannot do (for example HWP forms) come to you as work to do yourself, with the prepared text.

## 4. Where your data goes

* Entry Pilot has no server run by the developer, and the program has no code that sends your information to the developer.
* It connects directly to: Telegram (api.telegram.org), nodejs.org, github.com and registry.npmjs.org during install, Google for checking a Gemini key, Gmail's mail server (smtp.gmail.com), and the event sites it enters.
* What the AI reads is processed by that AI's company (Anthropic, OpenAI or Google), the same as when you use the AI directly.
* The Telegram token, API keys and mail password are stored with Windows encryption (DPAPI) on this PC.

## 5. AI subscriptions and the AI companies' rules

* Entry Pilot runs the official Claude Code and Codex CLI programs as they are published, without changing them.
* You sign in on each company's own login page. Entry Pilot does not read, copy or send your login tokens; it only checks whether you are logged in.
* Each person uses their own subscription on their own PC. Entry Pilot does not pass anyone's requests through another person's subscription, and there is no Entry Pilot server in between.
* Gemini uses your own API key.
* **Several accounts:** if you log in more than one account for an AI, Entry Pilot moves to the next account when one reaches its usage limit. Subscription limits are meant for ordinary individual use, and the companies' terms forbid getting around usage limits, so this may be seen as breaking those rules. Whether to use it is your choice. If you are unsure, use one account per AI (the default); then no account switching happens.

## 6. Rules of the events and contests

* Each event and contest has its own rules, such as no automatic entries, one entry per person, or no AI-made work. Following them is your responsibility.
* Entry Pilot hands events that forbid automatic entry, and captchas, to you as work to do yourself, but it can miss rules that are written unclearly. For contests, check the rules before you press **통과**.

---

## Details

For developers.

### Requirements
* Windows 10 or 11 (x64; tested on Windows 11), Chrome or Edge.
* To build: Python 3 with PyInstaller.

### Develop
* Tests: `python run_tests.py`
* Build: `python build\build.py` → `build\dist\EntryPilot-Setup.exe` (one-file setup) and `build\dist\EntryPilot-Portable\` (put a `portable.txt` next to the exe and all data stays in that folder).
* Window-less dev server: `app/server.py` (`serve()`), UI in `ui/`.
* Test install without touching the desktop: set `EP_APP_DIR` to a temp folder and `EP_TEST_NO_SHELL=1`.

### Where things live (`%LOCALAPPDATA%\EntryPilot`)
| folder | what |
|---|---|
| `app\` | EntryPilot.exe (window), python.exe (console twin the engine and agents call), `_internal\` |
| `runtime\` | node, the AI CLIs (npm packages), git (PortableGit when Git is missing), deskrun.exe |
| `logins\claude`, `logins\codex` (`-2` … `-5`) | logins kept by the official CLIs (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) |
| `data\` | engine board (platform.db), profile.json, policy.json (incl. contest filters), logs, Chrome profile |
| `settings.json` | app settings; secrets encrypted with DPAPI |

### Layout
* `app/`: window app, local server, installer, logins, Telegram and e-mail notices.
* `ui/`: the window (plain HTML, CSS, JS).
* `ep/`: the event engine (crawler, scheduler, agent runs, policy). `ep/policy.py` holds the user-adjustable rules, including the contest filters.
* `cp/`, `prompts_contest/`: the contest engine (its own data folder `data\contest`, in-app review in `cp/review.py`).
* `prompts/`: instructions given to the AI for each kind of task in event mode.

### Not done yet
* HWP/HWPX contest forms (the HWP tool is not public yet), AI image and video generation.
* Connections to LCH-WIKI, LLM-RELAY and LLM-CONSOLE: the Settings tab has a place for them, marked not supported yet.
* Code signing.
* No auto-start at Windows login.

## Disclaimer

Entry Pilot is provided "as is", without warranty of any kind (see the MIT License below). The author is not responsible for any problem that arises from using this program, including loss or damage, account suspension, cancelled prizes or disqualification, and civil or criminal liability. You use it at your own risk. Following the law, the AI companies' terms and the rules of each event, contest and website is your responsibility.

## License

MIT. See [LICENSE](LICENSE).
