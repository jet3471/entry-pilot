# Playbook: X (x.com, twitter.com); the X account logged in in the entry browser, lane browser
Allowed actions: the X policy line. Login cookie: auth_token on .x.com.
## verify
- Logged in and allowed → lane browser; notes: accounts to follow, post URL to repost/like/reply, reply text (comment rule, campaign language), required hashtag. Not logged in → manual sns. Not allowed by policy → exclude.
- Quote posts or replies that must tag friends → manual sns. Winners by DM: say so in notes.
## submit
- Follow: on the profile the button whose data-testid ends in "-follow"; done when it ends in "-unfollow".
- Repost: data-testid="retweet", then the menu item data-testid="retweetConfirm"; done when data-testid="unretweet" shows.
- Like: data-testid="like" → becomes "unlike".
- Reply: data-testid="reply", type into data-testid="tweetTextarea_0", click data-testid="tweetButton"; confirm the reply is listed.
- Hashtag post: compose with the required tags plus 1-2 specific sentences; never mention people.
