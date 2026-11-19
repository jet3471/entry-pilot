# Playbook: Moaform (moaform.com)
The page that asks name / contact / email: its "다음" IS the submit button. Never press it with placeholder values (a junk entry was once submitted that way).
## verify
- form-read walks pages by clicking only next/start words and stops at that page (stopped=final-page). Use its `pages` output as the map; never re-read a form in progress.
- No login → lane form.
- "이미 답변하신 설문" (already answered) → owner: "Did you answer this survey yourself?"
## submit
- Answers, snapshot file paths and the form URL come only from `cp submission {{ID}}` (`{{EP}} submission {{ID}}`, i.e. `cp.py submission {{ID}}`); upload files from `submission.snapshot_dir` and finish with `cp done ... --sent-json` (submit.md).
- No input elements: choose by clicking the option text: ul.js-choice → visible li (li.select-one-and-many) → its p or div; confirm it shows selected.
- Move on by clicking the "다음" text.
- Final page: type the real profile values, dialog handler, click "다음", read the completion text.
## submit_no_work
- Quiz, vote, or survey (`work.no_work`): open `item.url`, fill from `answers`, `verify_notes` and `# Profile`, and finish with `cp done ... --evidence`.
- No input elements: choose by clicking the option text: ul.js-choice → visible li (li.select-one-and-many) → its p or div; confirm it shows selected.
- Move on by clicking the "다음" text.
- Final page: type the real profile values, dialog handler, click "다음", read the completion text.
