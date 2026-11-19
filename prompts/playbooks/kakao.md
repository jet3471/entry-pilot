# Playbook: KakaoTalk (channel add, channel chat, channel form, Kakao login)
Allowed only: channel add, Kakao login, the entry message in the event channel's 1:1 chat, the entry form in the channel chatroom menu. Never message friends or chatrooms; sharing only to "나와의 채팅" (chat with myself). Every Kakao action → ep consent add --kind notification_subscription (or other).
## verify
- Channel add on pf.kakao.com or Kakao login in Chrome → lane browser.
- Any step that needs the KakaoTalk app (1:1 channel chat, chatroom-menu form, app-only share) → manual --need kakao (an agent cannot use the KakaoTalk app).
## submit
- Kakao login: the user logs in once in the entry browser (site-login step in the app). Not logged in → manual --need login "Kakao login".
- Channel add: on pf.kakao.com/<channel> click "채널 추가"; confirm it shows added; record the consent.
- A Kakao certificate or payment screen → stop; manual kakao.
