# Task: submit {{ID}} (lane {{LANE}})
The item is claimed for you. If `ep get {{ID}}` shows ready, run `ep claim --agent {{AGENT}} --id {{ID}}`; any state but submitting → stop.

1. Follow answers and verify_notes; SNS steps first, then the form. Unlisted questions: answering policy below. Profile fields only when asked; tick and record required consents only.
2. Proof capture: scroll the subscribed/commented/neighbor screen into view, `page.screenshot(path=r'{{DATA_DIR}}\captures\{{ID}}_1.png')` (_2 ...); `page.set_input_files('input[type=file]', path)`; wait ≤ 15 s for the file name or preview.
3. Submit once (dialog handler first); read the completion text in that script.

# Outcomes
- Completion text seen → done. Required-question alert → fix, submit again.
- Clicked but completion unseen, or script cut after the click → fail --no-resubmit.
- Condition unmet, nothing submitted → fail --reason "not submitted: <why>".
- Login/identity/SNS/app/payment you cannot do → manual; closed or ineligible → exclude; captcha, "already answered" → owner.
- Quiz/OX/초성 answer unconfirmed: take it from the notice/event page, else up to 2 web searches, else pick the most plausible answer yourself and submit (a reasoned guess beats no entry; never owner a quiz).
- Logins: `ep secret fill KEY --selector "<input>" --url-contains <https-host>` (keys: `ep secret list`); missing or unbound key → manual --need login naming it (user binds in app; never `secret bind` or guess).
- Sign-up (per policy, new keys only): ID = local part of profile email (+digits if taken), `<host>` = exact ASCII https host (e.g. `example.co.kr`); `ep secret set <SITEKEY>_PW --domain <host> --generate 16`, type with `ep secret fill <SITEKEY>_PW --selector "<pw input>" --url-contains <host>`; `echo <id> | ep secret set <SITEKEY>_ID --domain <host>`; consent member_signup.
- Never tag friends or share to personal SNS, friends or chats; Kakao sharing only to "나와의 채팅" (chat with myself).

# Answering policy
Used/visited/know/subscribed → yes, "within the last year", "occasionally"; opinions: your own reasonable pick; free text: two specific sentences; avoid "other" needing text; occupation, job, student status: from the profile block (occupation, job_role, student; a field it does not give: "other", the smallest, no, or `ep owner` when the form requires a fact); org size: smallest; optional marketing/third-party consent: no.
