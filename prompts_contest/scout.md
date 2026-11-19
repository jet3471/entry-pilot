# Task: scout {{REGION}} contests (no submissions)
Find 10-20 open online contests (공모전, コンテスト: essay, story, slogan, naming, idea, plan, design, poster, photo, video) in region {{REGION}} that the user can enter by sending a work online; write them as JSON lines to {{OUT}}. Never submit, sign up, follow, comment, claim or call other cp write commands. Never open supertoday.co.kr or suto.co.kr.

Window: deadline from {{TODAY}} + 3 days (time to write the work) to {{UNTIL}} inclusive. No date: skip.
Known contests (skip same URL or same organizer + title): {{KNOWN}}
Skip: offline or postal-only entry, entry fee, teams only, entry limited to a school, company, age band or occupation (residence limits: set eligibility), entry that needs an existing product, company, portfolio or the user's own photo/video, lotteries and giveaways (another system), contests that forbid AI-made works.
Priority: (1) big top prize and many winners, (2) work the agents can write in text (essay, slogan, naming, idea, plan), then poster/design, (3) short deliverable, (4) open to everyone.

# Fields (every key on every line; unknown → "" or null; never guess)
title (original language), title_en, org, url (submission page or form; else the notice), source_url (official notice), region "{{REGION}}", form_type google|naver|moaform|site|email|other, deadline YYYY-MM-DD, deadline_time HH:MM or "", prize (as written), prize_min_value (number, local currency, smallest prize), winners (number or null), all_rewarded false, effort_min (minutes to write the work), login_required true/false, conditions (theme, who may enter, format, rights, AI rule; short), result_date (as written or ""), eligibility open|residency_required|unknown, work_type essay|story|slogan|naming|idea|plan|design|photo|video|poster|code|other, theme (short), format (length, file type, language), submit_method form|email|upload|post|sns|other, top_prize (as written), top_prize_krw (number; about 1,400 per USD, 9.5 per JPY; null if unknown), team_size (1 only), notes (English, short).

# Rules
- Find the official organizer notice; listing sites only as source_url (never the sole source).
- Confirm each url exists and the contest is still open (web fetch, or a new Chrome tab you close). Never fill or submit forms.
- Never invent contests, URLs or numbers. Write with Python, one `json.dumps(obj, ensure_ascii=False)` per line, appending as you confirm each; stop at 20.

# Finish
When 10-20 lines are written (or sources are exhausted), print one line: `scout done: <n> lines -> {{OUT}}`.
