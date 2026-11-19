# Playbook: KakaoTalk (channel add, channel chat, channel form, Kakao login)
Allowed only: channel add, Kakao login, the entry message in the event channel's 1:1 chat, the entry form in the channel chatroom menu. Never message friends or chatrooms; sharing only to "나와의 채팅" (chat with myself). Every Kakao action → cp consent add --kind notification_subscription (or other).
## verify
- Channel add on pf.kakao.com or Kakao login in Chrome → lane browser.
- Any step that needs the KakaoTalk app (1:1 channel chat, chatroom-menu form, app-only share) → manual --need kakao (no phone Kakao command exists; never tap the phone).
## submit
- Kakao login: `cp secret fill KAKAO_ID --selector "<id input>" --url-contains kakao.com`, then `cp secret fill KAKAO_PW --selector "<pw input>" --url-contains kakao.com` (accounts/kauth/logins.kakao.com), press login yourself. Missing keys → manual --need login naming them.
- Channel add: on pf.kakao.com/<channel> click "채널 추가"; confirm it shows added; record the consent.
- A Kakao certificate or payment screen → stop; manual kakao.
