# Task: scout {{REGION}} events (no submissions)
Find 10-20 new online events in region {{REGION}} the user can enter and write them as JSON lines to {{OUT}}. Never submit, sign up, follow, comment, claim or call other ep write commands. Never open supertoday.co.kr or suto.co.kr (they block automated access).

Window: deadline from {{TODAY}} to {{UNTIL}} inclusive. No date ("always open"): skip.
Known events (skip same URL or same organizer + title): {{KNOWN}}
Skip: on-site visit, purchase or payment, creative work (own photo, video, essay, drawing, idea contest).
Priority: (1) everyone or many first-come rewarded, (2) many winners, (3) high smallest-prize value, (4) 1-5 minute entries (surveys, quizzes, comments, follows).

# Fields (every key on every line; unknown → "" or null; never guess)
title (original language), title_en, org, url (the entry page: form, post, widget; never an aggregator page), source_url (notice), region "{{REGION}}", form_type google|naver|moaform|site|x|instagram|youtube|other, deadline YYYY-MM-DD, deadline_time HH:MM or "", prize, prize_min_value (number, local currency, smallest prize), winners (number or null), all_rewarded true/false, effort_min (number), login_required true/false, conditions (what an entrant must do, short), result_date (as written or ""), eligibility open|residency_required|unknown, sns_account (@handle of every account to follow, comma-separated), post_url (original SNS post), notes (English, short).

# Rules
- SNS events: find the original post by organizer + title; aggregator pages only as source_url.
- Confirm each url exists and is still open (web fetch, or a new Chrome tab you close). Never fill or submit forms; do not form-read NowNSurvey.
- Eligibility from the notice text and the region notes; prizes needing a local address → residency_required.
- Never invent events, URLs or numbers. Write with Python, one `json.dumps(obj, ensure_ascii=False)` per line, appending as you confirm each; stop at 20.

# Finish
When 10-20 lines are written (or sources are exhausted), print one line: `scout done: <n> lines -> {{OUT}}`.
