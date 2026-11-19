# Playbook: NowNSurvey (나우앤서베이)
Reading the form leaves a partial response session (aidx) with temporary answers on their server. Read it at most once, during verify; the submitter never runs form-read on it.
## verify
- No login → lane form; answers per page from the form-read pages output.
- stopped="needs-submit" (needs_submit true: link or submit needed) → manual --need other; never follow links or submit to reach the next page.
## submit
- Radio inputs are hidden: click label[for=<input id>], then confirm the input is checked.
- Buttons "다음" / "Next". The page with name/contact fields: real profile values only, then submit and read the completion text.
