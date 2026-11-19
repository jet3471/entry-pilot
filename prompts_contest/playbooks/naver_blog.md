# Playbook: Naver blog (neighbor-add / 공감 / comment / scrap); shared Naver account only, lane browser
## verify
- Naver logged in → lane browser; notes: blog URL to neighbor-add, post URL to comment on, comment text (Korean), secret comment yes/no, scrap yes/no. Not logged in → manual sns.
- Post URL missing: look in the notice; the submitter fails "not submitted: no post URL to comment on" if still missing.
## submit
- blog.naver.com posts are inside iframe#mainFrame: use `page.frame(name='mainFrame')`, or open the m.blog.naver.com URL.
- Neighbor-add opens a popup (BuddyAdd.naver): catch it with `ctx.wait_for_event('page')`; choose 이웃 (or 서로이웃 if the blog accepts mutual), 다음, the group, 다음. Confirm "이웃으로 추가하였습니다" or the dialog "이미 ... 추가되어 있습니다", then close the popup. Without one of these texts it is not done.
- 공감 (like): the like button inside the frame.
- Comment: on the m.blog.naver.com post press the comment button (page CommentList.naver?blogId=<blogId>&logNo=<postNo>); type into .u_cbox_text or textarea, tick secret comment if required, press .u_cbox_btn_upload; confirm it is listed.
- Scrap needs a public category; none public → fail "not submitted: no public scrap category".
- Neighbor status: button text at m.blog.naver.com/<id> (admin.blog.naver.com demands re-login).
