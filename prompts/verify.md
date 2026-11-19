# Task: verify {{ID}}
Decide whether and how to enter this item; record it with one Finish command. Never submit, follow or comment.

1. flags.possible_dup_of set → `ep get` it; same organizer's same event → exclude "same event as <ID>".
2. Read the notice (source_url): conditions, deadline, eligibility, quiz answers, result date. Read the form once: `ep form-read "URL"`. Login page or no questions → lane browser.
3. Decide:
- exclude: deadline passed, form closed, region/age/youth-only mismatch, the user's city missing from a required residence list, duplicate, impossible factual proof, visit or creative work, refused sign-up site.
- Foreign address/residency or local phone required: per policy lines, not exclude.
- owner: required fact not in profile/policy; captcha; organizer forbids automatic entry; "already answered". A quiz is never owner - answer it (4).
- manual --need: payment; identity (i-PIN); upload (the user's own photo/file); other (visit; form-read stopped="needs-submit": never submit/follow link); app; kakao (KakaoTalk app step); login (member ID/nickname, sign-up not allowed, missing secret key); sns (not logged in).
- lane form: public, no login, no SNS action. browser: logged-in Chrome (login forms, YouTube, Naver blog, X, Facebook, Threads, captures, sign-ups, script-drawn forms). An SMS/identity code or an app-only step: manual.
4. --answers: JSON {fragment: answer} for every required question. Quiz/OX/초성: from the notice/event page, else up to 2 web searches, else your most plausible guess - enter it, never owner.
5. --notes (English, short): steps, URLs to follow/subscribe/comment, comment text, required consents, captures, answer source.
6. --url if the real form differs; --result-date as written or "not in notice"; --eligibility.

# Answering policy
- Used/visited/know/subscribed/member → yes side; "within the last year"; "occasionally"; answer user surveys as a user.
- Satisfaction/preference/opinion: pick a reasonable answer yourself; never an owner question.
- Free text: two specific sentences of your own. Avoid "other" options needing text.
- Occupation, job, student status: from the profile block (occupation, job_role, student); org size: smallest. A field the profile does not give: the generic answer ("other", the smallest, no) or `ep owner` when the form requires a fact.
- Gender, age, birth year, city: profile. item.answers given by the user: verbatim.
- Required consents yes; optional marketing/third-party no.
