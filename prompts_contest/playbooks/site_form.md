# Playbook: site forms (organizer sites, SurveyMonkey, Typeform, MS Forms, Qualtrics, comment boards)
## verify
- form-read shows no questions ("참여하기"/"Enter" button first, or script-drawn) → not owner: lane browser; the submitter reads them in Chrome.
- Member ID or nickname of the service required → manual login. Captcha / 보안문자 → owner. Organizer forbids automatic entry → owner. New account → sign-up policy line.
- Newsletter subscription as a condition: note it (subscribe with the profile email first; consent notification_subscription).
## submit
- Field values, snapshot file paths and the form URL come only from `cp submission {{ID}}` (`{{EP}} submission {{ID}}`, i.e. `cp.py submission {{ID}}`); upload files from `submission.snapshot_dir` and finish with `cp done ... --sent-json` (submit.md).
- Custom radios/checkboxes: click the label, or `page.evaluate("([n,i])=>document.getElementsByName(n)[i].click()", [name, index])`; confirm checked.
- Forms inside iframes: work in the frame (e.g. iframe#survey-form-frame).
- Buttons that open material or popups (e.g. "상세 자료 열기"): each must show its ✓ or completion before submit is accepted.
- A comment board asking a delete password: `cp secret fill COMMENT_PW --selector "<pw input>" --url-contains <board https host>`; key missing or unbound → manual --need login "COMMENT_PW".
- Completion: a thank-you or completion page (e.g. OnAirEnd.asp); quote its text.
## submit_no_work
- Quiz, vote, or survey (`work.no_work`): open `item.url`, fill from `answers`, `verify_notes` and `# Profile`, and finish with `cp done ... --evidence`.
- Custom radios/checkboxes: click the label, or `page.evaluate("([n,i])=>document.getElementsByName(n)[i].click()", [name, index])`; confirm checked.
- Forms inside iframes: work in the frame (e.g. iframe#survey-form-frame).
- Buttons that open material or popups (e.g. "상세 자료 열기"): each must show its ✓ or completion before submit is accepted.
- A comment board asking a delete password: `cp secret fill COMMENT_PW --selector "<pw input>" --url-contains <board https host>`; key missing or unbound → manual --need login "COMMENT_PW" (user binds in app settings; never run `secret bind`).
- Completion: a thank-you or completion page (e.g. OnAirEnd.asp); quote its text.
