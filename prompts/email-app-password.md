# Task
Create one Google app password named "Entry Pilot" on Google's app password page. The user agreed in the Entry Pilot
window and logged in to Google in the entry browser. Nobody answers. Always write the result
file, then end with one line.

# Browser
`python -X utf8 "{{EP_PY}}" chrome ensure`, then Playwright (Python) `connect_over_cdp('http://127.0.0.1:9222')`,
`ctx = b.contexts[0]`; use the tab at `https://myaccount.google.com/apppasswords`, else `ctx.new_page()` and open it.
Scripts at most 20 s. Print only short status words. Never print page.content(), account names, addresses, the
list of existing app passwords or the password.

# Privacy rules (the user was shown these)
- Open only `https://myaccount.google.com/apppasswords`. Do not open Gmail, any other Google page, or any other site.
- Change no setting. Do not turn 2-Step Verification on or off, do not edit the account, do not read, open or delete
  the app passwords that already exist.
- Never type the user's Google password. Never enter, read or handle 2-Step Verification codes, prompts or recovery
  options. If Google asks for a password or a code, stop (see "not_logged_in").
- The password is secret. Put it into the result file from inside the same script that reads it. Do not print it,
  do not put it in a command line, a message to the user or any other file, and do not screenshot the dialog.

# Steps
1. Open the page. If the address changes to `accounts.google.com`, or asks for a password, a code or a phone prompt →
   result `{"ok": false, "reason": "not_logged_in"}`, stop.
2. If the page says app passwords are not available because 2-Step Verification is off (the page asks to turn it on,
   or has no app name field) → result `{"ok": false, "reason": "two_step_off"}`, stop. Do not turn it on.
3. Type `Entry Pilot` in the app name field and click the create button (만들기 / Create).
4. The dialog shows the app password as 16 lowercase letters (in four groups of four). In one Playwright script read
   that text, remove spaces, check it matches `^[a-z]{16}$`, and write it straight into the result file (below).
   Not 16 letters or no dialog → result `{"ok": false, "reason": "no_password"}`.
5. Click the dialog's close button (완료 / Done). Close the tab you opened or used for the page.
6. Result file `{{RESULT_FILE}}` (UTF-8 JSON): `{"ok": true, "password": "<16 letters>"}`; for any failure
   `{"ok": false, "reason": "<short English, no password>"}`. End with one line: ok or the reason.
