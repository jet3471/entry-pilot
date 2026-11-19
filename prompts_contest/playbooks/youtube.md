# Playbook: YouTube (subscribe / like / comment / capture); shared Google account, lane browser
## verify
- Notes: channel URL, video URL, comment text (comment rule, the channel's language). Unknown channel: one YouTube search for the organizer. Not logged in → manual sns.
- A required YouTube handle or channel name: from the profile block; missing → manual sns.
## submit
- Subscribe: on the channel page `ytd-subscribe-button-renderer button`; skip if it already shows subscribed (구독중 / Subscribed).
- Like: on the video `like-button-view-model button`; check aria-pressed first.
- Comment: repeat `page.mouse.wheel(0, 800)` until `ytd-comments #placeholder-area` is visible (max 15 s); click #placeholder-area, type into #contenteditable-root, click #submit-button; confirm the comment appears in the list.
- Proof: capture each screen to captures\{{ID}}_<n>.png when the form asks for it.
- Logged-in handle: /feed/you or the channel page.
