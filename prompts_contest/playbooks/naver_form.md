# Playbook: Naver Form (form.naver.com, naver.me)
## verify
- form-read reads every page's questions from the DOM (.nsv_survey_question; required = .nsv_survey_question_required or "답변 필수").
- No login → lane form. Naver login required → lane browser (shared Naver account logged in). Quiz forms: answers from the notice, else a reasoned best guess (never owner).
- Sites that demand Naver login again inside their own service (e.g. 국민생각함, 소통24 once did) → manual login.
## submit
- Answers, snapshot file paths and the form URL come only from `cp submission {{ID}}` (`{{EP}} submission {{ID}}`, i.e. `cp.py submission {{ID}}`); upload files from `submission.snapshot_dir` and finish with `cp done ... --sent-json` (submit.md).
- Pick options by input id `nsv-survey-question-<question no>-item-<option no>`, never by text ("기타", "예" repeat across questions).
- Click the option label, then confirm the input is checked; if not, `page.check(selector, force=True)` and confirm again.
- Checkboxes toggle on every click: already checked → do not click. One question at a time, confirm right away.
- Multi-page: fill only the visible page, then click "다음".
- Password re-entry: `cp secret fill NAVER_PW --selector "<pw input>" --url-contains nid.naver.com`.
- Naver logged out (session lost after a Chrome restart) → manual --need login "Naver session lost".
- Completion: "답변이 제출되었습니다". The account's past entries: "참여한 설문" menu on form.naver.com.
## submit_no_work
- Quiz, vote, or survey (`work.no_work`): open `item.url`, fill from `answers`, `verify_notes` and `# Profile`, and finish with `cp done ... --evidence`.
- Pick options by input id `nsv-survey-question-<question no>-item-<option no>`, never by text ("기타", "예" repeat across questions).
- Click the option label, then confirm the input is checked; if not, `page.check(selector, force=True)` and confirm again.
- Checkboxes toggle on every click: already checked → do not click. One question at a time, confirm right away.
- Multi-page: fill only the visible page, then click "다음".
- Password re-entry: `cp secret fill NAVER_PW --selector "<pw input>" --url-contains nid.naver.com`.
- Naver logged out (session lost after a Chrome restart) → manual --need login "Naver session lost".
- Completion: "답변이 제출되었습니다". The account's past entries: "참여한 설문" menu on form.naver.com.
