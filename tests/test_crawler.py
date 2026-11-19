"""Offline tests for ep.crawler: saved HTML snippets, no network."""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

from ep import crawler

TODAY = dt.date(2026, 11, 14)

EH_LIST = """
<div class='list_row' onclick="site_go('d1',320122,0);"><div class='list_no'><a href='/site/view.php?no=320122' target='_blank'>14878</a></div><div class='list_cates'>설문/투표 </div><div class='list_subject' id='sj1'><img src='./img/list/list_7.gif'> [<strong>스포츠가치센터</strong>] 스포츠가치 조사로 네이버페이 받자 이벤트!! <sup title='당첨자수'> 100</sup></div><div class='list_entry'><a href='./fgo.php?fnum=320122&go=2'>x</a></div><div class='list_day'>11/20</div><div class='list_gift'>상품권</div></div>
<div class='list_detail' id='d1'><div class='detail_td'><div class='td_left'>ㆍ응모기간</div><div class='td_rd'> 2026-11-13 ~ 2026-11-20 24 :00</div></div><div class='detail_td'><div class='td_left'>ㆍ응모방법</div><div class='td_rd'>① 유튜브 구독 ② 폼제출</div></div></div>
<div class='list_row'><div class='list_no'><a href='/site/view.php?no=320065'>14839</a></div><div class='list_cates'>퀴즈/퍼즐 </div><div class='list_subject'>[<strong>NH저축은행</strong>] 시니어 적금 맞히고 바닐라라떼 받자 이벤트!!</div><div class='list_day'>11/10</div></div>
<div class='list_detail'><div class='detail_td'><div class='td_left'>ㆍ응모기간</div><div class='td_rd'> 2026-11-01 ~ 2026-11-10 24 :00</div></div></div>
<div class='list_row'></div>
"""

QZ_LIST = """
<a class="v2-card" href="/bbs/qe_event/views/13155597"><div class="v2-card__meta">
<div class="v2-card__row v2-card__row--head"><span>리뷰</span><span class="v2-card__dday">D-5</span></div>
<p class="v2-card__title">필립스 모니터 리뷰 보고 케이블 받자!</p>
<div class="v2-card__row"><span>26.11.13
   ~26.11.19</span><span class="v2-card__stats">39명</span></div></div></a>
<a class="v2-notice-title" href="/bbs/qe_event/views/13137998">공지</a>
"""

QZ_VIEW = """<html><body><div class="header">menu <a href="https://www.instagram.com/quasarzone">q</a></div>
<div class="view-content">이벤트 정보 참여 방법 댓글 남기기
<a href="https://forms.gle/AbC123">폼</a> <a href="https://prod.danawa.com/info/?pcode=1">상품</a></div></body></html>"""

EK_LIST = """
<a href="event.php?id=224"></a><a href="event.php?id=224">광고성 이벤트</a><a href="event.php?id=224">응모하러 가기</a>
<a href="event.php?id=240">설문/투표 [블랙후즈] TV·가전 생활 조사 ~ 11/16</a><a href="event.php?id=240">응모</a>
<a href="event.php?id=237">[플러스메이커스] OX 퀴즈</a>
"""

EK_VIEW = """<html><head><title>TV 가전 설문 이벤트 - 이벤트 코리아</title></head><body>
<h1>TV 가전 설문 이벤트</h1><p>2026년 11월 13일 ~ 11월 16일 경품 정보 커피 5개 이벤트 정보 주최 블랙후즈 당첨자 5명 발표일 2026.11.20 등록자 블랙후즈 등록일 2026.11.13</p>
<a href="https://form.naver.com/response/abcdEFG">참여</a>
<p>광고 댓글 (0) 자동 작성 방지</p></body></html>"""

KK_LIST = """
<div class="list_type event"><ul>
<li><a href="https://aichallenge4all.or.kr/competitions/s4" target="_blank"><span class="text"><strong>[과학기술정보통신부] AI 달인의 꿀팁 챌린지 시즌4</strong><span class="source"><span>기간 : 2026.11.02 ~ 2026.11.20</span></span></span></a></li>
<li><a href="https://old.example.kr/e" target="_blank"><span class="text"><strong>[기상청] 지난 이벤트</strong><span class="source"><span>기간 : 2026.09.01 ~ 2026.10.01</span></span></span></a></li>
</ul></div>"""

DN_LIST = """<table><tbody>
<tr><td class="state"><span class="ico_state t_ing">진행중</span></td><td class="company"><span>샌디스크</span></td><td class="type">퀴즈</td>
<td class="title"><a href="/sandisk1_261112" class="link_tit" target="_blank">Sandisk OX 퀴즈 이벤트!</a></td><td class="due">2026.11.12~2026.11.18</td><td class="date">2026.11.19</td></tr>
<tr><td class="state"><span class="ico_state t_ing">진행중</span></td><td class="company"><span>이엠텍</span></td><td class="type">체험단</td>
<td class="title"><a href="/emtek_261111" class="link_tit">그래픽카드 체험단</a></td><td class="due">2026.11.11~2026.11.17</td><td class="date">2026.11.18</td></tr>
<tr><td class="state"><span class="ico_state t_end">종료</span></td><td class="company"><span>X</span></td><td class="type">댓글</td>
<td class="title"><a href="/x_261001" class="link_tit">끝난 이벤트</a></td><td class="due">2026.10.01~2026.10.07</td><td class="date">2026.10.08</td></tr>
</tbody></table>"""

WV_LIST_HTML = """<ul class="list"><li class="top"><div class="tit">공모전명</div></li>
<li class="bg"><div class="tit"><a href="?c=find&amp;s=1&amp;mode=ing&amp;gub=1&amp;gbn=view&amp;gp=1&amp;ix=112343">저상버스 네이밍 공모전  <span class="stat spec">SPECIAL</span> </a>
<div class="sub-tit">분야 : 네이밍/슬로건, 기타</div></div><div class="organ">교통공단</div>
<div class="day">D-16 <span class="dday ing">접수중</span></div><div class="read">3,997</div></li></ul>"""

WV_VIEW = """<html><body><div class="cd-area"><ul class="cd-info-list">
<li><span class="tit">분야</span> 네이밍/슬로건</li><li><span class="tit">주최/주관</span> 교통공단</li>
<li><span class="tit">접수기간</span> 2026-11-13 ~ 2026-11-29 D-15</li>
<li><span class="tit">홈페이지</span> <a href="http://www.kappd.or.kr/bbs/board.php?bo_table=a&wr_id=15" target="_blank">http://www.kappd.or.kr/bbs/board.php?bo_table=a&wr_id=15</a></li></ul>
<div>참가는 <a href="https://forms.gle/WvForm1">구글폼</a> 으로 제출. 본 정보의 사실 여부를 보증하지 않습니다. 목록</div></div></body></html>"""

SEOUL_FEED = """<?xml version="1.0"?><rss><channel>
<item><title>2026년 민원서비스 개선 우수사례 시민 온라인 투표</title><link>https://news.seoul.go.kr/gov/archives/581971</link>
<pubDate>2026-11-12 10:57:03</pubDate><dc:creator><![CDATA[시민봉사담당관]]></dc:creator>
<description><![CDATA[<p>투표기간 11.12.~11.20. 참여 링크 <a href="https://form.naver.com/response/xyz">투표하기</a></p>]]></description></item>
<item><title>[11.16(월)까지] 서울도시공간포털 이용만족도 설문</title><link>https://news.seoul.go.kr/citybuild/archives/571000</link>
<pubDate>2026-11-06 10:13:25</pubDate><description><![CDATA[<p>설문 응답</p>]]></description></item>
<item><title>2026년 겨울편 서울꿈새김판 수상작 발표</title><link>https://news.seoul.go.kr/gov/archives/581962</link><pubDate>2026-11-13 09:00:02</pubDate><description>x</description></item>
<item><title>전기차 충전구역 주차 과태료</title><link>https://news.seoul.go.kr/env/archives/571508</link><pubDate>2026-11-13 11:03:05</pubDate><description>x</description></item>
</channel></rss>"""


def make_fetch(pages: dict, fail=()):
    calls = []

    def fetch(url):
        calls.append(url)
        if any(f in url for f in fail):
            raise OSError("boom " + url)
        for k, v in pages.items():
            if k in url:
                return v
        raise OSError("404 " + url)
    fetch.calls = calls
    return fetch


class ParseTests(unittest.TestCase):
    def test_eventhouse(self):
        it = crawler.eventhouse_parse_list(EH_LIST, TODAY)
        self.assertEqual(len(it), 2)
        a = it[0]
        self.assertEqual(a["source_url"], "https://www.eventhouse.kr/site/view.php?no=320122")
        self.assertEqual(a["org"], "스포츠가치센터")
        self.assertEqual(a["title"], "스포츠가치 조사로 네이버페이 받자 이벤트!!")
        self.assertIn("2026-11-20", a["deadline_hint"])
        self.assertEqual(a["_deadline_iso"], "2026-11-20")
        self.assertEqual(a["posted"], "2026-11-13")
        self.assertIn("폼제출", a["text"])

    def test_quasarzone(self):
        it = crawler.quasarzone_parse_list(QZ_LIST, TODAY)
        self.assertEqual(len(it), 1)  # notice link is not a card
        self.assertEqual(it[0]["source_url"], "https://quasarzone.com/bbs/qe_event/views/13155597")
        self.assertEqual(it[0]["_deadline_iso"], "2026-11-19")
        self.assertEqual(it[0]["posted"], "2026-11-13")
        d = crawler._qz_detail(crawler.soup_of(QZ_VIEW), it[0]["source_url"])
        self.assertEqual(d["links"], ["https://forms.gle/AbC123"])
        self.assertIn("댓글 남기기", d["text"])

    def test_eventskr(self):
        it = crawler.eventskr_parse_list(EK_LIST)
        self.assertEqual([x["_order"] for x in it], [240, 237, 224])
        upd = crawler._ek_detail(crawler.soup_of(EK_VIEW), "https://events.kr/event.php?id=240")
        self.assertEqual(upd["title"], "TV 가전 설문 이벤트")
        self.assertEqual(upd["org"], "블랙후즈")
        self.assertEqual(upd["posted"], "2026-11-13")
        self.assertEqual(upd["links"], ["https://form.naver.com/response/abcdEFG"])
        self.assertNotIn("자동 작성 방지", upd["text"])

    def test_koreakr(self):
        it = crawler.koreakr_parse_list(KK_LIST, TODAY)
        self.assertEqual(it[0]["org"], "과학기술정보통신부")
        self.assertEqual(it[0]["title"], "AI 달인의 꿀팁 챌린지 시즌4")
        self.assertEqual(it[0]["entry_url_guess"], "https://aichallenge4all.or.kr/competitions/s4")
        self.assertEqual(it[0]["_deadline_iso"], "2026-11-20")

    def test_danawa(self):
        it = crawler.danawa_parse_list(DN_LIST, TODAY)
        self.assertEqual(len(it), 1)  # reviewer recruiting and ended rows dropped
        a = it[0]
        self.assertEqual(a["source_url"], "https://event.danawa.com/sandisk1_261112")
        self.assertEqual(a["entry_url_guess"], a["source_url"])
        self.assertEqual(a["org"], "샌디스크")
        self.assertEqual(a["_deadline_iso"], "2026-11-18")
        self.assertEqual(a["posted"], "2026-11-12")
        self.assertIn("퀴즈", a["text"])
        got = crawler.src_danawa(make_fetch({"event/lists": DN_LIST}), TODAY, pages=2)
        self.assertEqual(len(got), 1)  # same row on both pages is deduped

    def test_wevity(self):
        it = crawler.wevity_parse_list(WV_LIST_HTML, TODAY)
        self.assertEqual(len(it), 1)
        self.assertEqual(it[0]["title"], "저상버스 네이밍 공모전")
        self.assertEqual(it[0]["source_url"], "https://www.wevity.com/?c=find&s=1&gbn=view&gp=1&ix=112343")
        self.assertEqual(it[0]["_deadline_iso"], "2026-11-30")
        with mock.patch.object(crawler, "today_default", lambda: TODAY):
            d = crawler._wv_detail(crawler.soup_of(WV_VIEW), it[0]["source_url"])
        self.assertEqual(d["entry_url_guess"], "http://www.kappd.or.kr/bbs/board.php?bo_table=a&wr_id=15")
        self.assertIn("https://forms.gle/WvForm1", d["links"])
        self.assertEqual(d["_deadline_iso"], "2026-11-29")
        self.assertEqual(d["org"], "교통공단")
        self.assertNotIn("보증하지", d["text"])

    def test_seoulnews(self):
        it = crawler.seoul_parse_feed(SEOUL_FEED, TODAY)
        self.assertEqual([x["title"] for x in it], [
            "2026년 민원서비스 개선 우수사례 시민 온라인 투표", "[11.16(월)까지] 서울도시공간포털 이용만족도 설문"])
        self.assertEqual(it[0]["links"], ["https://form.naver.com/response/xyz"])
        self.assertEqual(it[0]["_deadline_iso"], "2026-11-20")
        self.assertEqual(it[0]["posted"], "2026-11-12")
        self.assertEqual(it[0]["org"], "시민봉사담당관")
        got = crawler.src_seoulnews(
            make_fetch({"/gov/feed": SEOUL_FEED, "/economy/feed": "<rss/>"}, fail=("citybuild",)), TODAY)
        self.assertEqual(len(got), 2)  # a failing secondary feed does not kill the source
        with self.assertRaises(OSError):
            crawler.src_seoulnews(make_fetch({}), TODAY)

    def test_fetcher_wait_is_per_host(self):
        f = crawler.Fetcher(delay=0.2)
        t0 = crawler.time.monotonic()
        f._wait("https://a.example/1")
        f._wait("https://b.example/1")  # other host: no wait
        self.assertLess(crawler.time.monotonic() - t0, 0.15)
        f._wait("https://a.example/2")  # same host: waits ~0.2 s
        self.assertGreaterEqual(crawler.time.monotonic() - t0, 0.19)

    def test_helpers(self):
        self.assertEqual(crawler.parse_md("11/20", TODAY), "2026-11-20")
        self.assertEqual(crawler.parse_md("01/05", TODAY), "2027-01-05")
        self.assertEqual(crawler.last_date("26.11.13 ~26.11.19", TODAY), "2026-11-19")
        self.assertEqual(crawler.norm_title("[A사] 퀴즈 이벤트!!"), crawler.norm_title("퀴즈  이벤트"))
        links = crawler.find_links(crawler.soup_of(
            '<a href="https://forms.gle/x">a</a><a href="/x">b</a>text https://naver.me/abc and https://example.com/'))
        self.assertEqual(links, ["https://forms.gle/x", "https://naver.me/abc"])
        self.assertEqual(crawler.guess_entry(["https://www.instagram.com/p/1", "https://forms.gle/x"]),
                         "https://forms.gle/x")

    def test_decode_cp949(self):
        raw = "안녕".encode("cp949")
        self.assertEqual(crawler._decode(raw, "text/html; charset=euc-kr"), "안녕")

    def test_blocked_hosts(self):
        self.assertTrue(crawler._is_blocked("https://www.suto.co.kr/cpevent"))
        self.assertTrue(crawler._is_blocked("https://supertoday.co.kr/"))
        self.assertFalse(crawler._is_blocked("https://events.kr/"))
        with self.assertRaises(RuntimeError):
            crawler.Fetcher()("https://www.suto.co.kr/x")


def src_a(fetch, today=None):
    return [
        {"source": "a", "source_url": f"https://a.example/{i}", "title": f"A event {i}", "org": "",
         "deadline_hint": "", "entry_url_guess": "", "posted": f"2026-11-{10 + i:02d}", "text": f"a{i}",
         "_has_detail": True} for i in range(5)]


def src_b(fetch, today=None):
    return [
        {"source": "b", "source_url": f"https://b.example/{i}", "title": ("[X] A event 0" if i == 0 else f"B event {i}"),
         "org": "", "deadline_hint": "", "entry_url_guess": "", "posted": f"2026-11-{10 + i:02d}",
         "text": f"b{i} https://forms.gle/b{i}", "_has_detail": True} for i in range(5)]


def src_bad(fetch, today=None):
    raise OSError("down")


class CollectTests(unittest.TestCase):
    def run_collect(self, sources, known=(set(), set()), mx=40):
        errs = []
        r = crawler.collect(make_fetch({}), known[0], known[1], mx, TODAY, sources, errs)
        return r

    def test_spread_and_dedup(self):
        items, counts, errors, worked = self.run_collect([("a", src_a), ("b", src_b)], mx=6)
        self.assertEqual(len(items), 6)
        self.assertEqual(sum(counts.values()), 6)
        self.assertGreaterEqual(min(counts.values()), 2)
        titles = [i["title"] for i in items]
        # "[X] A event 0" duplicates "A event 0" from source a
        self.assertNotIn("[X] A event 0", titles)
        # newest first
        posted = [i["posted"] for i in items]
        self.assertEqual(posted, sorted(posted, reverse=True))

    def test_known_skip(self):
        known = ({"https://a.example/0"}, {"A event 1"})
        items, *_ = self.run_collect([("a", src_a)], known)
        self.assertEqual(sorted(i["title"] for i in items), ["A event 2", "A event 3", "A event 4"])

    def test_known_entry_url_skip(self):
        known = ({"https://forms.gle/b2"}, set())
        items, *_ = self.run_collect([("b", src_b)], known)
        self.assertNotIn("B event 2", [i["title"] for i in items])

    def test_expired_skipped(self):
        def src_old(fetch, today=None):
            return [{"source": "o", "source_url": "https://o.example/1", "title": "old", "_deadline_iso": "2026-11-01",
                     "text": "t", "_has_detail": True}]
        items, _, errors, worked = self.run_collect([("o", src_old)])
        self.assertEqual(items, [])
        self.assertEqual(worked, 1)

    def test_failed_source_does_not_crash(self):
        items, counts, errors, worked = self.run_collect([("bad", src_bad), ("a", src_a)], mx=2)
        self.assertEqual(worked, 1)
        self.assertEqual(len(items), 2)
        self.assertTrue(any(e.startswith("bad:") for e in errors))

    def test_blocked_stub_dropped(self):
        def src_blk(fetch, today=None):
            return [{"source": "s", "source_url": "https://www.suto.co.kr/x", "title": "t", "text": "x",
                     "_has_detail": True}]
        items, *_ = self.run_collect([("s", src_blk)])
        self.assertEqual(items, [])

    def test_detail_failure_recorded(self):
        def src_d(fetch, today=None):
            return [{"source": "d", "source_url": "https://d.example/1", "title": "T", "_detail": crawler._generic_detail}]
        items, _, errors, _ = self.run_collect([("d", src_d)])
        self.assertEqual(len(items), 1)
        self.assertTrue(any("detail" in e for e in errors))
        self.assertEqual(items[0]["text"], "T")

    def test_per_source_cap(self):
        def src_big(fetch, today=None):
            return [{"source": "big", "source_url": f"https://big.example/{i}", "title": f"Big {i}", "text": "t",
                     "_has_detail": True} for i in range(30)]

        def src_small(fetch, today=None):
            return [{"source": "small", "source_url": "https://small.example/1", "title": "Small 1", "text": "t",
                     "_has_detail": True}]
        items, counts, *_ = self.run_collect([("big", src_big), ("small", src_small)], mx=10)
        self.assertEqual(counts["big"], 4)  # 40 % of --max, not 9
        self.assertEqual(counts["small"], 1)
        with mock.patch.dict(crawler.SOURCE_CAPS, {"big": 2}):
            _, counts, *_ = self.run_collect([("big", src_big)], mx=10)
        self.assertEqual(counts["big"], 2)

    def test_lists_fetched_in_parallel_one_failure_isolated(self):
        items, counts, errors, worked = self.run_collect(
            [("a", src_a), ("bad", src_bad), ("b", src_b)], mx=6)
        self.assertEqual(worked, 2)
        self.assertEqual(len(items), 6)
        self.assertTrue(any(e.startswith("bad: list failed") for e in errors))

    def test_text_trim_and_links_cap(self):
        def src_t(fetch, today=None):
            return [{"source": "t", "source_url": "https://t.example/1", "title": "T", "_has_detail": True,
                     "text": "x" * 5000 + " " + " ".join(f"https://forms.gle/{i}" for i in range(20))}]
        items, *_ = self.run_collect([("t", src_t)])
        self.assertLessEqual(len(items[0]["text"]), 1800)


class MainTests(unittest.TestCase):
    def _run(self, sources):
        d = tempfile.mkdtemp()
        known = os.path.join(d, "known.txt")
        out = os.path.join(d, "out.json")
        with open(known, "w", encoding="utf-8") as f:
            f.write("\tTitle only known\nhttps://a.example/0\t\n\n")
        with mock.patch.object(crawler, "SOURCES", sources), mock.patch.object(crawler, "Fetcher", lambda: make_fetch({})):
            rc = crawler.main(["--known", known, "--out", out, "--max", "3", "--today", "2026-11-14"])
        return rc, out

    def test_exit_0_and_output_shape(self):
        rc, out = self._run([("a", src_a), ("bad", src_bad)])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 3)
        self.assertEqual(set(data[0]), set(crawler.FIELDS))
        self.assertNotIn("https://a.example/0", [d["source_url"] for d in data])

    def test_exit_2_when_all_fail(self):
        rc, out = self._run([("bad", src_bad)])
        self.assertEqual(rc, 2)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_load_known(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "k.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("https://x\tTitle X\n\tTitle Y\nhttps://z\n")
        urls, titles = crawler.load_known(p)
        self.assertEqual(urls, {"https://x", "https://z"})
        self.assertEqual(titles, {"Title X", "Title Y"})


if __name__ == "__main__":
    unittest.main()
