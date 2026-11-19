# Playbook: site forms (organizer sites, SurveyMonkey, Typeform, MS Forms, Qualtrics, comment boards)
## verify
- form-read shows no questions ("참여하기"/"Enter" button first, or script-drawn) → not owner: lane browser; the submitter reads them in Chrome.
- Member ID or nickname of the service required → manual login. Captcha / 보안문자 → owner. Organizer forbids automatic entry → owner. New account → sign-up policy line.
- Newsletter subscription as a condition: note it (subscribe with the profile email first; consent notification_subscription).
## submit
- Custom radios/checkboxes: click the label, or `page.evaluate("([n,i])=>document.getElementsByName(n)[i].click()", [name, index])`; confirm checked.
- Forms inside iframes: work in the frame (e.g. iframe#survey-form-frame).
- Buttons that open material or popups (e.g. "상세 자료 열기"): each must show its ✓ or completion before submit is accepted.
- A comment board asking a delete password: `ep secret fill COMMENT_PW --selector "<pw input>" --url-contains <board https host>`; key missing or unbound → manual --need login "COMMENT_PW" (user binds in app settings; never run `secret bind`).
- Completion: a thank-you or completion page; quote its text.
