# Playbook: email verification code or link (entry mailbox in the entry browser)
The entry email address is the profile `email`. Its Gmail must be logged in in the entry browser (CDP 9222); if it is not, manual --need login. Use it only for mails the current entry triggered.
1. Trigger the mail on the entry page first (send/resend code); note the sender name or subject words you expect.
2. Open a NEW tab via CDP: `https://mail.google.com/mail/u/0/#search/<sender or subject words>` (add `newer_than:1d`). Wait up to 20 s, press nothing else.
3. Open the newest matching mail and read its body only. Extract the code (digits/letters) or the verification link. No mail after 2 refreshes (wait 30 s between) → fail "not submitted: verification mail not received".
4. Code: switch back to the entry tab and type it. Link: open it in the same tab as the mail, read the result text.
5. Close the mail tab. Never write the code or the link anywhere (evidence says "email code entered" only).
6. Sign-up verification → `ep consent add ... --kind member_signup`. Never change Gmail settings or filters; never delete, archive, label, star or send mail; never read unrelated mail; never click anything else in a mail.
