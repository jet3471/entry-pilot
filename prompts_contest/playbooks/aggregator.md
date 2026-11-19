# Playbook: aggregator listings (eventhouse, 이벤트몬, supertoday, blog roundups)
- Never open supertoday (suto.co.kr, supertoday.co.kr): it blocks automated access.
- eventhouse hides outbound links (fgo.php) and sometimes gives only an account display name.
- An aggregator page is never the entry URL. Find the original page or post by organizer + title search; for SNS events record every account to follow as @id.
## verify
- Original found → verify with --url <original URL> and the matching lane. Not found after 2 searches → exclude "original entry page not found".
## submit
- The url is still an aggregator page → fail "not submitted: original entry page unknown".
