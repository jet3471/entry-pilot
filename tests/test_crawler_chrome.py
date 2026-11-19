import datetime as dt
import unittest

from ep.crawler_chrome import parse_links


class ParseLinks(unittest.TestCase):
    def test_posts_only_title_and_snippet(self):
        links = [["abc님의 블로그\n새 창 열림", "https://blog.naver.com/abc"],
                 ["댓글 이벤트 vol.1\n새 창 열림", "https://blog.naver.com/abc/123456789012"],
                 ["이벤트 기간 11월 15일 경품 기프티콘 응모는 댓글", "https://blog.naver.com/abc/123456789012"]]
        r = parse_links(links, dt.date(2026, 11, 14))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["title"], "댓글 이벤트 vol.1")
        self.assertIn("기프티콘", r[0]["text"])


if __name__ == "__main__":
    unittest.main()
