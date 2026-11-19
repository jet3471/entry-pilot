# Task
Make a Telegram bot for the user with @BotFather in Telegram Web. The user agreed in the Entry Pilot window and
logged in to Telegram Web in the entry browser. Nobody answers. Always write the result file, then end with one line.

# Browser
`python -X utf8 "{{EP_PY}}" chrome ensure`, then Playwright (Python) `connect_over_cdp('http://127.0.0.1:9222')`,
`ctx = b.contexts[0]`; use the tab at `https://web.telegram.org/`, else `ctx.new_page()`. Scripts at most 20 s.
Print only BotFather's newest reply (at most 600 chars). Never print page.content(), chat lists, names or the token.

# Privacy rules (the user was shown these)
- Open only @BotFather and the new bot, by URL only (no chat list, no search):
  `https://web.telegram.org/k/#@BotFather`, `https://web.telegram.org/k/#@<bot username>`.
- Do not open, read, scroll, forward or delete any other chat, contact, group, channel or setting.
- Send nothing except the messages below. Before sending, check the header shows "BotFather" with the verified mark.

# Steps
1. Open the BotFather URL. QR or phone login page instead → result `{"ok": false, "reason": "not_logged_in"}`, stop.
2. Click START if shown. Send `/newbot`; wait for the name question.
3. Send `{{BOT_NAME}}`; wait for the username question.
   If BotFather refuses the name → result `{"ok": false, "reason": "name"}`, stop now (no logout: the user retries).
4. Send these usernames in order until one is accepted: {{USERNAMES}}. All refused →
   result `{"ok": false, "reason": "username"}`, stop now (no logout: the user picks another).
5. Token: in the "Done! Congratulations" reply, the text matching `\d{6,12}:[A-Za-z0-9_-]{30,}`.
6. Open the new bot's URL, click START if shown, send exactly `/start {{START_CODE}}`.
7. Log out: ☰ menu (top left) > Settings > ⋮ (top right) > Log out > confirm. Check the QR page shows.
8. Write the result file `{{RESULT_FILE}}` (UTF-8 JSON):
   `{"ok": true, "token": "...", "username": "...", "start_sent": true, "logout": true}`;
   false for any step that failed, plus `"reason"` (short English, never include the token or chat text). Close the tabs you opened.
