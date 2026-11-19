# Task: judge KR event candidates (no browsing, no submissions)
A Python crawler already collected candidate events. Read {{CAND}} (JSON list). For each candidate decide from its own `title`, `text`, `links`, `deadline_hint` and `entry_url_guess` whether the user can enter online, and write one JSON line per accepted candidate to {{OUT}}. Never submit, sign up, follow, comment or call other ep write commands. Do not browse the web and do not open other files: everything you need is in {{CAND}}. If one candidate is unclear, skip it; never invent URLs, dates or numbers.

Window: deadline from {{TODAY}} to {{UNTIL}} inclusive. No date ("always open"): skip.
Skip: on-site visit, purchase or payment, creative work (own photo, video, essay, drawing, idea contest), residents-only, age-limited events that exclude the user.
Priority: (1) everyone or many first-come rewarded, (2) many winners, (3) high smallest-prize value, (4) 1-5 minute entries (surveys, quizzes, comments, follows).

# Fields (every key on every line; unknown → "" or null; never guess)
title (original language), title_en, org, url (the entry page: form, post, widget; never an aggregator page; choose it from `links` or `entry_url_guess`), source_url (the candidate's source_url), region "KR", form_type google|naver|moaform|site|x|instagram|youtube|other, deadline YYYY-MM-DD, deadline_time HH:MM or "", prize, prize_min_value (number, KRW, smallest prize), winners (number or null), all_rewarded true/false, effort_min (number), login_required true/false, conditions (what an entrant must do, short), result_date (as written or ""), eligibility open|residency_required|unknown, sns_account (@handle of every account to follow, comma-separated), post_url (original SNS post), notes (English, short).

# How to write
One single Python call: build the list of dicts yourself, then write one `json.dumps(obj, ensure_ascii=False)` per line to {{OUT}} (UTF-8). Do it once, not per candidate.

# Finish
Print one line: `scout done: <n> lines -> {{OUT}}`.
