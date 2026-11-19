# Playbook: Instagram (the Instagram account logged in in the entry browser, lane browser)
Only the minimum action the item needs (follow, like, comment). Never share, post a story, message anyone or follow anything the item does not ask for. Login cookie: sessionid on .instagram.com.
## verify
- Logged in → lane browser. Notes: account(s) to follow (@id), "post URL: <original /p/ or /reel/ URL>" (never an aggregator page), what the comment should address. Not logged in → manual sns.
- Find the post from the organizer account's profile by title words; confirm the URL opens.
- Friend tag required → the comment ends with the profile's `friend_tags` accounts, nobody else (none in the profile → manual sns). Story repost, sharing, or more than 2 tags → manual sns.
- Account cannot be found from the notice or organizer → exclude "Instagram account not found".
## submit
- Follow: the profile button "팔로우"/"Follow"; done when it reads "팔로잉"/"Following" (or "요청됨"/"Requested").
- Like: the heart button (aria-label "좋아요"/"Like"); confirm it changes to "좋아요 취소"/"Unlike".
- Comment: the textarea (aria-label "댓글 달기..."/"Add a comment…"), type, "게시"/"Post"; confirm the comment is listed.
- A challenge, "Try again later" or action-blocked notice → stop: fail "not submitted: Instagram limit".
