# Playbook: NowNSurvey (나우앤서베이)
Reading the form leaves a partial response session (aidx) with temporary answers on their server. Read it at most once, during verify; the submitter never runs form-read on it.
## verify
- No login → lane form; answers per page from the form-read pages output.
- stopped="needs-submit" (needs_submit true) → manual --need other; never submit to reach the next page.
## submit
- Answers, snapshot file paths and the form URL come only from `cp submission {{ID}}` (`{{EP}} submission {{ID}}`, i.e. `cp.py submission {{ID}}`); upload files from `submission.snapshot_dir` and finish with `cp done ... --sent-json` (submit.md).
- Radio inputs are hidden: click label[for=<input id>], then confirm the input is checked.
- Buttons "다음" / "Next". The page with name/contact fields: real profile values only, then submit and read the completion text.
## submit_no_work
- Quiz, vote, or survey (`work.no_work`): open `item.url`, fill from `answers`, `verify_notes` and `# Profile`, and finish with `cp done ... --evidence`.
- Radio inputs are hidden: click label[for=<input id>], then confirm the input is checked.
- Buttons "다음" / "Next". The page with name/contact fields: real profile values only, then submit and read the completion text.
