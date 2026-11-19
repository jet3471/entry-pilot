# Playbook: Google Forms (docs.google.com/forms, forms.gle)
Closed: "no longer accepting responses" / "더 이상 응답을 받지 않습니다" / "回答を受け付けていません" → exclude.
## verify
- form-read parses FB_PUBLIC_LOAD_DATA_: every section, question, required flag and option.
- No login → lane form with --answers and notes (answer source, required consents).
- Sign-in required or one response per account (needs Google login) → lane browser; the shared Google account is logged in via YouTube.
- form-read shows a login page: questions are hidden; quiz answers from the notice, else a reasoned best guess (never owner); lane browser.
## submit
- Answers, snapshot file paths and the form URL come only from `cp submission {{ID}}` (`{{EP}} submission {{ID}}`, i.e. `cp.py submission {{ID}}`); upload files from `submission.snapshot_dir` and finish with `cp done ... --sent-json` (submit.md).
- One-response forms record the shared account's email automatically: submit anyway.
- Logged-in forms autosave a draft and restore it on revisit: finish in one tab; re-finding the tab per script is expected.
- Next section: click the div[role=button] whose text contains "다음" / "Next" / "次へ", wait until the next questions show.
- Click each option once and confirm aria-checked="true"; check aria-checked before clicking: a second click un-selects (div[role=checkbox] toggles).
- An overlay div.QSj8ac blocking clicks: dismiss it first.
- After submit, "필수 질문입니다" / "This is a required question" or div[role=alert] → not submitted: fill it, submit again.
- Completion: "응답이 기록되었습니다" / "Your response has been recorded".
- Never POST to formResponse directly; the completion screen is the evidence.
## submit_no_work
- Quiz, vote, or survey (`work.no_work`): open `item.url`, fill from `answers`, `verify_notes` and `# Profile`, and finish with `cp done ... --evidence`.
- One-response forms record the shared account's email automatically: submit anyway.
- Logged-in forms autosave a draft and restore it on revisit: finish in one tab; re-finding the tab per script is expected.
- Next section: click the div[role=button] whose text contains "다음" / "Next" / "次へ", wait until the next questions show.
- Click each option once and confirm aria-checked="true"; check aria-checked before clicking: a second click un-selects (div[role=checkbox] toggles).
- An overlay div.QSj8ac blocking clicks: dismiss it first.
- After submit, "필수 질문입니다" / "This is a required question" or div[role=alert] → not submitted: fill it, submit again.
- Completion: "응답이 기록되었습니다" / "Your response has been recorded".
- Never POST to formResponse directly; the completion screen is the evidence.
