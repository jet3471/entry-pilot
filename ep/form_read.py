"""Extract the questions and choices of an entry form as JSON (headless Chromium). For verification. Read only; never submits.

  python -m ep.form_read <URL> [--pages N] [--json]

Google Forms: read every section's questions, required flags and choices from FB_PUBLIC_LOAD_DATA_ in the page.
Naver Form: all pages' questions are in the DOM together, so hidden pages are read at once (questions: question,
  required, choices, input fields).
Multi-page forms: read visible text and input fields, and click only a button with type="button" (never anchors <a>,
  submit controls, or elements inside POST forms) whose text is one of NEXT_WORDS ("next", "start", ...) to go to the
  next page (up to --pages, default 8). Never fills dummy values into inputs, blocks service workers and popups, and
  routes all GET/HEAD requests through Python's DNS-pinned fetcher while aborting non-GET/HEAD or non-public requests.
  When advancing requires an anchor link, submit, or non-GET request, stops with stopped="needs-submit" and
  needs_submit=True so the item goes to manual review.
Output: ok, kind (google|naver|multi), url, login_page, text (per-page text), inputs, pages (text, inputs, buttons per
  page), stopped (no-next | blocked | final-page | max-pages | needs-submit), needs_submit.
"""
import contextlib
import html as htmllib
import json
import re
import sys
import urllib.parse

from ep.urlpolicy import (
    ALLOWED_BROWSER_METHODS,
    PolicyBlockedError,
    fetch_pinned,
    is_public_http_url,
    resolve_public_http_url,
)

# Button texts matched on the page (exact match, lower-cased). Korean: "next", "next page", "start", "continue"
NEXT_WORDS = ("다음", "다음 페이지", "다음페이지", "시작하기", "시작", "next", "start", "계속")
# Buttons containing any of these are never clicked. Korean: "submit", "complete", "send", "register", "participate", "enter (apply)"
NEVER_WORDS = ("제출", "완료", "보내기", "등록", "참여하기", "응모", "submit", "send", "finish", "done")
TEXT_LIMIT = 14000

INPUTS_JS = """els => els.filter(e => e.type !== 'hidden' && e.getClientRects().length > 0).map(e => ({
    tag: e.tagName, type: e.type, name: e.name || e.id,
    required: !!(e.required || e.getAttribute('aria-required') === 'true'),
    label: (e.labels && e.labels[0] ? e.labels[0].innerText : e.placeholder || '').trim().slice(0, 80)}))"""
BUTTONS_JS = """els => els.filter(e => e.getClientRects().length > 0)
    .map(e => (e.value || e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 30)).filter(Boolean)"""
UNSAFE_NEXT_JS = """e => {
  const tag = (e.tagName || '').toUpperCase();
  const rawType = (e.getAttribute('type') || '').trim().toLowerCase();
  const propType = (e.type || '').toLowerCase();
  if (tag === 'A' || (e.closest && e.closest('a'))) {
    return 'anchor-link';
  }
  if (tag === 'INPUT' && (rawType === 'submit' || rawType === 'image' || propType === 'submit' || propType === 'image')) {
    return 'submit-input';
  }
  if (tag !== 'BUTTON' && tag !== 'INPUT') {
    return 'non-button-element';
  }
  if (rawType !== 'button' || propType !== 'button') {
    return tag === 'BUTTON' ? 'submit-button' : 'non-button-input';
  }
  const fm = (e.getAttribute('formmethod') || '').trim().toLowerCase();
  if (fm && fm !== 'get') return 'formmethod-' + fm;
  const form = e.form || (e.closest ? e.closest('form') : null);
  if (form) {
    const m = (form.getAttribute('method') || form.method || 'get').trim().toLowerCase();
    if (m !== 'get') return 'post-form';
    const act = (form.getAttribute('action') || '').trim();
    if (act && /^(javascript|data|file|chrome|about|view-source):/i.test(act)) return 'unsafe-form-action';
  }
  return '';
}"""
# Naver Form reader. Korean page strings: '답변 필수' = "answer required"; '복수' / '모두 골라' / '여러 개' = "multiple" /
# "choose all" / "several" (multiple-choice hints)
NAVER_JS = """() => {
  const clean = s => (s || '').replace(/답변 필수/g, '').replace(/\\s+/g, ' ').trim();
  const out = [];
  for (const q of document.querySelectorAll('.nsv_survey_question')) {
    const t = q.querySelector('.nsv_survey_reply_question_title');
    const d = q.querySelector('.nsv_survey_reply_question_description');
    const opts = [...q.querySelectorAll('.nsv_survey_question_label_multiple_choice_text')].map(e => clean(e.textContent));
    const pinfo = [...q.querySelectorAll('.nsv_survey_question_personal_info_title')].map(e => clean(e.textContent));
    const inputs = [...q.querySelectorAll('input, textarea, select')].filter(e => e.type !== 'hidden' && e.type !== 'checkbox' && e.type !== 'radio')
      .map(e => ({type: e.type, placeholder: (e.placeholder || '').slice(0, 60)}));
    out.push({title: clean(t ? t.textContent : q.textContent.slice(0, 200)),
              required: !!q.querySelector('.nsv_survey_question_required, .nsv_survey_question_required_text') || /답변 필수/.test(q.textContent),
              description: clean(d ? d.textContent : '').slice(0, 400),
              multiple: /복수|모두 골라|여러 개/.test(q.textContent),
              options: opts, personal_info: pinfo, inputs: inputs});
  }
  return out;
}"""


def google(html):
    m = re.search(r"FB_PUBLIC_LOAD_DATA_ = (.*?);</script>", html, re.S)
    if not m:
        return None
    data = json.loads(m.group(1))
    form = data[1]
    out = {"title": form[8] if len(form) > 8 else None, "description": form[0], "questions": []}
    for q in form[1] or []:
        if len(q) < 5 or not q[4]:
            if q and len(q) > 1:
                out["questions"].append({"section_or_text": q[1]})
            continue
        for part in q[4]:
            out["questions"].append({"text": q[1], "help": q[2] if len(q) > 2 else None, "type": q[3],
                                     "required": bool(part[2]) if len(part) > 2 else None,
                                     "options": [o[0] for o in (part[1] or []) if o and o[0]]})
    return out


def _frame_allowed(page, f) -> bool:
    if f == page.main_frame:
        return True
    furl = getattr(f, "url", "") or ""
    if not furl or furl == "about:blank":
        return True
    return is_public_http_url(furl)[0]


def all_text(page):
    parts = []
    for f in page.frames:
        if not _frame_allowed(page, f):
            continue
        try:
            t = f.inner_text("body").strip()
        except Exception:
            t = ""
        if t:
            parts.append(t if f == page.main_frame else f"[frame {f.url[:120]}]\n{t}")
    return "\n".join(parts)


def all_eval(page, selector, js):
    res = []
    for f in page.frames:
        if not _frame_allowed(page, f):
            continue
        try:
            res.extend(f.eval_on_selector_all(selector, js))
        except Exception:
            pass
    return res


# Validation notices that are not a page change. Korean: "required question", "answer required", "required input",
# "input needed", "is missing"
NOTICE = re.compile(r".*(필수 문항|답변필수|필수 입력|입력 필요|모자랍니다|required|answer this question).*\n?", re.I)


def strip_notices(text):
    return NOTICE.sub("", text).strip()


# Personal-field words. Korean: name, full name, contact, phone number, mobile phone, mobile, email
FINAL_RE = re.compile(r"이름|성명|연락처|전화번호|휴대전화|휴대폰|이메일|e-?mail|phone", re.I)


def looks_final(text, inputs):
    """Whether the page asks for name, phone or email. The "next" button of such a page is often the submission (Moaform),
    so it is not clicked."""
    hints = " ".join(f"{i.get('label', '')} {i.get('name', '')} {i.get('type', '')}" for i in inputs)
    if FINAL_RE.search(hints) or any(i.get("type") in ("tel", "email") for i in inputs):
        return True
    for line in text.splitlines():
        # Korean instruction verbs: "enter", "write down", "fill in", "leave"
        if FINAL_RE.search(line) and re.search(r"입력|적어|작성|남겨", line):
            return True
    return False


def _close_popup(popup) -> None:
    with contextlib.suppress(Exception):
        popup.route("**/*", lambda r: r.abort())
    with contextlib.suppress(Exception):
        popup.close()


def _cookie_header_for(ctx, url: str) -> str:
    if ctx is None or not hasattr(ctx, "cookies"):
        return ""
    try:
        ck = ctx.cookies([url])
    except Exception:
        return ""
    if not isinstance(ck, list):
        return ""
    return "; ".join(f"{c['name']}={c.get('value', '')}" for c in ck if isinstance(c, dict) and c.get("name"))


def _inject_base_if_redirected(body: bytes, orig_url: str, final_url: str, content_type: str) -> bytes:
    if not body or orig_url == final_url or "html" not in (content_type or "").lower():
        return body
    if b"<base " in body[:4096].lower():
        return body
    safe_base = htmllib.escape(final_url, quote=True).encode("ascii", "xmlcharrefreplace")
    m = re.search(rb"<head(?:\s[^>]*)?>", body[:4096], re.I)
    if m:
        pos = m.end()
        return body[:pos] + b'<base href="' + safe_base + b'">' + body[pos:]
    return body


def install_route_guard(page, guard_state=None, pre_resolved=None):
    """Enforce GET/HEAD-only and DNS-pinned public HTTP(S) fetching via Python + route.fulfill on every request."""
    if guard_state is None:
        guard_state = {
            "non_get_blocked": False,
            "blocked_methods": [],
            "url_blocked": "",
            "nav_blocked": "",
            "final_nav_url": "",
            "fulfilled_urls": set(),
        }
    else:
        guard_state.setdefault("final_nav_url", "")
        guard_state.setdefault("fulfilled_urls", set())

    if hasattr(page, "on"):
        with contextlib.suppress(Exception):
            page.on("popup", _close_popup)

    pre_holder = [pre_resolved]

    def _handler(route):
        req = route.request
        raw_m = getattr(req, "method", None)
        method = raw_m.upper() if isinstance(raw_m, str) and raw_m else "GET"
        if method not in ALLOWED_BROWSER_METHODS:
            guard_state["non_get_blocked"] = True
            guard_state["blocked_methods"].append(method)
            route.abort()
            return
        is_nav = bool(getattr(req, "is_navigation_request", lambda: False)())
        req_frame = getattr(req, "frame", None)
        main_nav = is_nav and (req_frame is None or req_frame == getattr(page, "main_frame", None))
        raw_hdrs = getattr(req, "headers", None)
        hdrs = dict(raw_hdrs) if isinstance(raw_hdrs, dict) else {}
        ctx = getattr(page, "context", None)
        used_pre = pre_holder[0]
        pre_holder[0] = None
        try:
            status, resp_hdrs, body, final_url = fetch_pinned(
                req.url,
                method=method,
                headers=hdrs,
                pre_resolved=used_pre,
                cookie_provider=(lambda u: _cookie_header_for(ctx, u)) if ctx is not None else None,
            )
        except PolicyBlockedError as e:
            msg = str(e)
            if not guard_state["url_blocked"]:
                guard_state["url_blocked"] = msg
            if main_nav:
                guard_state["nav_blocked"] = msg
            route.abort()
            return
        except (OSError, Exception):
            route.abort()
            return
        guard_state["fulfilled_urls"].add(req.url)
        guard_state["fulfilled_urls"].add(final_url)
        if main_nav:
            guard_state["final_nav_url"] = final_url
            body = _inject_base_if_redirected(body, req.url, final_url, resp_hdrs.get("content-type", ""))
        route.fulfill(status=status, headers=resp_hdrs, body=body)

    page.route("**/*", _handler)
    return guard_state


def _element_text(el) -> str:
    tag = (el.evaluate("e => (e.tagName || '').toUpperCase()") or "").upper()
    if tag == "INPUT":
        val = el.input_value()
        if not val and hasattr(el, "get_attribute"):
            val = el.get_attribute("value") or el.get_attribute("alt") or ""
        return (val or "").strip()
    return (el.inner_text() or "").strip()


def find_next_action(page):
    """Inspect visible NEXT_WORDS controls.
    Returns (el, label, unsafe_reason):
      - (el, label, "") when a safe button[type=button] (outside any POST form and never an <a>) is found;
      - (None, label, reason) when only anchor/submit/POST next control(s) exist (must NOT click -> needs-submit);
      - (None, None, "") when no NEXT_WORDS control exists.
    """
    unsafe_found = None
    for f in page.frames:
        if not _frame_allowed(page, f):
            continue
        try:
            loc = f.locator("button, a, input[type=button], input[type=submit], input[type=image], [role=button]")
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 200)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                el_tag = (el.evaluate("e => (e.tagName || '').toUpperCase()") or "").upper()
                txt = _element_text(el)
            except Exception:
                continue
            low = txt.lower()
            if not low or any(w in low for w in NEVER_WORDS):
                continue
            if not (low in NEXT_WORDS or low.split("/")[0].strip() in NEXT_WORDS):  # bilingual labels like '다음/Next'
                continue
            if el_tag == "A":
                unsafe = "anchor-link"
            else:
                try:
                    unsafe = el.evaluate(UNSAFE_NEXT_JS) or ""
                except Exception:
                    unsafe = "eval-failed"
            if not unsafe and el_tag not in ("BUTTON", "INPUT"):
                unsafe = "non-button-element"
            if not unsafe:
                try:
                    href_and_act = el.evaluate(
                        "e => ({tag: (e.tagName||'').toUpperCase(), href: e.getAttribute('href')||'', "
                        "act: (e.form || (e.closest ? e.closest('form') : null))?.getAttribute('action') || ''})"
                    ) or {}
                except Exception:
                    href_and_act = {}
                if (href_and_act.get("tag") or "").upper() == "A" or (href_and_act.get("href") or "").strip():
                    unsafe = "anchor-link"
                else:
                    f_url = getattr(f, "url", "") or page.url
                    act = (href_and_act.get("act") or "").strip()
                    if act and not act.startswith("#"):
                        ok, reason = is_public_http_url(urllib.parse.urljoin(f_url, act))
                        if not ok:
                            unsafe = f"blocked-form-action:{reason}"
            if unsafe:
                if unsafe_found is None:
                    unsafe_found = (txt, unsafe)
                continue
            return el, txt, ""
    if unsafe_found is not None:
        return None, unsafe_found[0], unsafe_found[1]
    return None, None, ""


def find_next(page):
    """Find a visible safe type='button' button whose text is one of NEXT_WORDS."""
    el, txt, unsafe = find_next_action(page)
    return (el, txt) if (el is not None and not unsafe) else (None, None)


def _assert_public_page(page, guard_state=None):
    if guard_state and guard_state.get("nav_blocked"):
        raise RuntimeError("blocked navigation: " + guard_state["nav_blocked"])
    if guard_state and page.url in guard_state.get("fulfilled_urls", ()):
        return
    ok, reason = is_public_http_url(page.url)
    if not ok:
        raise RuntimeError(f"blocked page url ({reason}): {page.url}")


def walk(page, max_pages, guard_state=None):
    pages, stopped = [], "max-pages"
    for idx in range(max_pages):
        _assert_public_page(page, guard_state)
        text = all_text(page)
        pages.append({"index": idx + 1, "url": page.url, "text": text[:6000],
                      "inputs": all_eval(page, "input, textarea, select", INPUTS_JS)[:80],
                      "buttons": all_eval(page, "button, a[role=button], input[type=button], input[type=submit], [role=button]", BUTTONS_JS)[:30]})
        if looks_final(text, pages[-1]["inputs"]):
            stopped = "final-page"
            break
        el, label, unsafe_reason = find_next_action(page)
        if unsafe_reason:
            stopped = "needs-submit"
            break
        if el is None:
            stopped = "no-next"
            break
        before = text
        if guard_state is not None:
            guard_state["non_get_blocked"] = False
            guard_state["nav_blocked"] = ""
        try:
            el.click(timeout=5000)
        except Exception:
            if guard_state and guard_state.get("nav_blocked"):
                raise RuntimeError("blocked navigation: " + guard_state["nav_blocked"])
            if guard_state and guard_state.get("non_get_blocked"):
                stopped = "needs-submit"
            else:
                stopped = "blocked"
            break
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(700)
        _assert_public_page(page, guard_state)
        if guard_state and guard_state.get("non_get_blocked"):
            stopped = "needs-submit"
            break
        # a newly shown "required question" notice alone is not a page change
        if strip_notices(all_text(page)) == strip_notices(before):
            stopped = "blocked"
            break
    return pages, stopped


def read_form(url: str, max_pages: int = 8, pw_factory=None) -> dict:
    ok, reason, initial_ips = resolve_public_http_url(url)
    if not ok:
        raise RuntimeError(f"blocked url ({reason}): {url}")
    if pw_factory is None:
        from playwright.sync_api import sync_playwright
        pw_factory = sync_playwright
    with pw_factory() as p:
        b = p.chromium.launch(headless=True)
        ctx = None
        try:
            ctx = b.new_context(service_workers="block")
            page = ctx.new_page()

            def _on_new_page(new_pg):
                if new_pg is not page:
                    _close_popup(new_pg)

            if hasattr(ctx, "on"):
                with contextlib.suppress(Exception):
                    ctx.on("page", _on_new_page)
            guard_state = install_route_guard(page, pre_resolved=(url, initial_ips))
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(500)
            _assert_public_page(page, guard_state)
            final = guard_state.get("final_nav_url") or page.url
            login = bool(re.search(r"nid\.naver\.com|accounts\.google\.com|login", final))
            html = page.content()
            g = google(html) if ("docs.google.com/forms" in final or "FB_PUBLIC_LOAD_DATA_" in html) else None
            if g:
                res = {"ok": True, "kind": "google", "url": final, "login_page": login, "needs_submit": False, **g}
            elif "form.naver.com" in final and not login:
                body = page.inner_text("body")
                m = re.search(r"(\d+)\s*/\s*(\d+)", body[:400])
                res = {"ok": True, "kind": "naver", "url": final, "login_page": login,
                       "page_count": int(m.group(2)) if m else 1, "needs_submit": False,
                       "text": body[:TEXT_LIMIT], "questions": page.evaluate(NAVER_JS),
                       "inputs": page.eval_on_selector_all("input, textarea, select", INPUTS_JS)[:80]}
            else:
                pages, stopped = walk(page, max_pages, guard_state)
                text = "\n".join(f"--- page {pg['index']} ---\n{pg['text']}" for pg in pages)
                inputs, seen = [], set()
                for pg in pages:
                    for i in pg["inputs"]:
                        k = (i["type"], i["name"], i["label"])
                        if k not in seen:
                            seen.add(k)
                            inputs.append(i)
                needs_sub = stopped == "needs-submit"
                last_url = final if (pages and pages[-1]["url"] == url and final != url) else (pages[-1]["url"] if pages else final)
                res = {"ok": True, "kind": "multi", "url": last_url, "login_page": login,
                       "page_count": len(pages), "stopped": stopped, "needs_submit": needs_sub,
                       "text": text[:TEXT_LIMIT], "inputs": inputs[:120], "pages": pages}
                if needs_sub:
                    res["manual_review"] = True
                    res["reason"] = "next page needs an anchor navigation or submit; send item to manual review"
        finally:
            if ctx is not None:
                with contextlib.suppress(Exception):
                    ctx.close()
            b.close()
    return res


def run(argv):
    args = [a for a in argv if not a.startswith("--")]
    url = args[0]
    max_pages = 8
    if "--pages" in argv:
        max_pages = int(argv[argv.index("--pages") + 1])
    res = read_form(url, max_pages=max_pages)
    print(json.dumps(res, ensure_ascii=False), flush=True)


def main(argv):
    try:
        run(list(argv))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:500]}, ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
