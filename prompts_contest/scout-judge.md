# Task: judge KR contest candidates (no browsing, no submissions)
A Python crawler already collected candidate contests (공모전). Read {{CAND}} (JSON list). For each candidate decide from its own `title`, `text`, `links`, `deadline_hint` and `entry_url_guess` whether the user can enter by sending a work online, and write one JSON line per accepted candidate to {{OUT}}. Never submit, sign up, follow, comment or call other cp write commands. Do not browse the web and do not open other files: everything you need is in {{CAND}}. If one candidate is unclear, skip it; never invent URLs, dates or numbers.

Window: deadline from {{TODAY}} to {{UNTIL}} inclusive. No date: skip.
Skip: offline or postal-only entry, entry fee, teams only, entry limited to a school, company, age band or occupation (residence limits: set eligibility), entry that needs an existing product, company, portfolio or the user's own photo/video, lotteries and giveaways (another system), contests that forbid AI-made works.
Priority: (1) big top prize and many winners, (2) work the agents can write in text (essay, slogan, naming, idea, plan), then poster/design, (3) short deliverable, (4) open to everyone.

# Fields (every key on every line; unknown -> "" or null; never guess)
title (original language), title_en, org, url (submission page or form; else the notice; never an aggregator page if the organizer page is in `links`), source_url (the candidate's source_url), region "KR", form_type google|naver|moaform|site|email|other, deadline YYYY-MM-DD, deadline_time HH:MM or "", prize (as written), prize_min_value (number, KRW, smallest prize), winners (number or null), all_rewarded false, effort_min (minutes to write the work), login_required true/false, conditions (theme, who may enter, format, rights, AI rule; short), result_date (as written or ""), eligibility open|residency_required|unknown, work_type essay|story|slogan|naming|idea|plan|design|photo|video|poster|code|other, theme (short), format (length, file type, language), submit_method form|email|upload|post|sns|other, top_prize (as written), top_prize_krw (number; null if unknown), team_size (1 only), notes (English, short).

# How to write
One single Python call: build the list of dicts yourself, then write one `json.dumps(obj, ensure_ascii=False)` per line to {{OUT}} (UTF-8). Do it once, not per candidate.

# Finish
Print one line: `scout done: <n> lines -> {{OUT}}`.
