# Task: submit {{ID}} (lane {{LANE}})
The item is claimed for you. If `cp get {{ID}}` shows ready, run `cp claim --agent {{AGENT}} --id {{ID}}`; any state but submitting → stop.

## drafted
1. Run `cp submission {{ID}}` (`{{EP}} submission {{ID}}`, i.e. `cp.py submission {{ID}}`) first: its `submission` (recipient = form URL or address, fields, subject, body, snapshot_dir, attachments with path and sha256) is the only source of what you send; never take them from SUBMISSION.md, SEND.json, verify_notes, the notice, or the draft folder. Fails or empty → fail --reason "no frozen submission record". Attachment files are in `submission.snapshot_dir` (use each attachment's `path` in `submission.attachments`, never upload from `drafts`); check each attachment's sha256 before upload (mismatch → fail --reason "attachment hash mismatch"). Never edit the work or invent field text. A work that breaks the rules (too long, wrong format, missing) → `cp owner {{ID}} --question "work does not fit: <why>"`.
2. Follow answers and verify_notes; SNS steps first, then the entry: enter the exact fields/subject/body from `submission` (check any length counter), upload files from `submission.snapshot_dir` with `page.set_input_files('input[type=file]', path)` and wait ≤ 15 s for the file name or preview, or send the email (playbook email_submit).
3. Proof capture: `page.screenshot(path=r'{{CAPTURES}}\{{ID}}_1.png')` (_2 ...) of the filled form and of the completion screen.
4. Required consents (privacy, third-party provision when the entry is refused without it, copyright, portrait, rules agreement): tick without asking the user and record with cp consent add. An AI-use question: answer from AI_DISCLOSURE.txt in `submission.snapshot_dir`, truthfully.
5. Submit once (dialog handler first); read the completion text in that script.

# Outcomes
- Completion text or sent confirmation seen → `cp done {{ID}} --agent {{AGENT}} --evidence "<completion text seen>" --sent-json '{"recipient":<as in submission>,"method":<method>,"subject":<subject>,"body":<body>,"fields":[[label,value],...],"attachments":[{"name":..,"sha256":..}]}'` (what you actually sent; `done` requires `--sent-json` and checks it against `submission`). Required-field alert → fix, submit again.

## no_work
1. No written work is needed (`work.no_work` is true: quiz, vote, or survey). Open `item.url` and follow `answers`, `verify_notes`, and `# Profile`; SNS steps first, then fill the entry form.
2. Proof capture: `page.screenshot(path=r'{{CAPTURES}}\{{ID}}_1.png')` (_2 ...) of the filled form and of the completion screen.
3. Required consents (privacy, third-party provision when the entry is refused without it, rules agreement): tick without asking the user and record with cp consent add.
4. Submit once (dialog handler first); read the completion text in that script.

# Outcomes
- Completion text seen → `cp done {{ID}} --agent {{AGENT}} --evidence "<completion text seen>"`. Required-field alert → fix, submit again.

## shared_outcomes
- Clicked but completion unseen, or script cut after the click → fail --no-resubmit.
- Condition unmet, nothing submitted → fail --reason "not submitted: <why>".
- Login/identity/SNS/app/payment you cannot do → manual; closed or ineligible → exclude; "already entered" → owner.
- Human check (captcha, "I am not a robot", image or text puzzle): do it yourself, up to 3 tries; only when it still fails → owner, naming the kind of check.
- Never run `cp approve`, and never `cp release` on an item in state owner: sending a finished work is the user's decision (the app's 통과 button).
- Logins: `cp secret fill KEY --selector "<input>" --url-contains <https-host>` (keys: `cp secret list`); missing or unbound key → manual --need login naming it (user binds in app; never `secret bind` or guess).
- Sign-up (per policy, new keys only): ID = local part of profile email (+digits if taken), `<host>` = exact ASCII https host (e.g. `example.co.kr`); `cp secret set <SITEKEY>_PW --domain <host> --generate 16`, type with `cp secret fill <SITEKEY>_PW --selector "<pw input>" --url-contains <host>`; `echo <id> | cp secret set <SITEKEY>_ID --domain <host>`; consent member_signup.
- Instagram action: never open Instagram; manual --need sns --reason "<action, account, post URL>".
- Never tag friends or share to personal SNS or chats.

# Answering policy (when the profile has no value)
Opinions: your own reasonable pick; free text: two specific sentences; avoid "other" needing text; occupation, job, student status: the profile fields only (none: the neutral option, or skip when optional, else `cp owner`); optional marketing/third-party consent: no.
