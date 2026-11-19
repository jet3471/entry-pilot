"""E-mail notices to the user's own mailbox through their own mail account (SMTP, app password).

Only notices go out; answers are given in the app window ("확인할 일"). The password is stored encrypted (DPAPI)."""
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

from app import settings

# Only Gmail sends: the stored secret is a Google app password and must never go to another SMTP server.
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
GMAIL_SMTP = ("smtp.gmail.com", 465, True)
HOSTS = {d: GMAIL_SMTP for d in GMAIL_DOMAINS}


class MailError(Exception):
    pass


PROVIDERS = {"gmail.com": "google", "googlemail.com": "google", "naver.com": "naver",
             "daum.net": "daum", "hanmail.net": "daum"}


# Pages the window may open in the user's default browser. The Naver addresses are best knowledge, not checked
# against the live site (assumption); the Google ones are the documented pages.
OPEN_URLS = {
    "apppw": "https://myaccount.google.com/apppasswords",
    "twosv": "https://myaccount.google.com/signinoptions/twosv",
    "naver-mail": "https://mail.naver.com",
    "naver-security": "https://nid.naver.com/user2/help/myInfoV2?m=viewSecurity",
    "daum-mail": "https://mail.daum.net",
}


def provider(address: str) -> str:
    """"google", "naver", "daum" or "other" by the domain of the address ("" while no domain is typed)."""
    if "@" not in (address or ""):
        return ""
    return PROVIDERS.get(address.rsplit("@", 1)[-1].lower().strip(), "other")


# A Google app password: 16 lowercase letters, shown by Google in four groups of four with spaces.
APP_PW_RE = re.compile(r"^\s*([a-z]{4})[  ]?([a-z]{4})[  ]?([a-z]{4})[  ]?([a-z]{4})\s*$")


def app_password_in(text: str) -> str:
    """The 16 letters when the whole text is a Google app password, else "". Never log or return the result."""
    m = APP_PW_RE.match(text or "")
    return "".join(m.groups()) if m else ""


def mask_app_password(pw: str) -> str:
    """Masked 4x4 preview of a 16-letter Google app password for UI confirmation."""
    s = app_password_in(pw)
    if len(s) != 16:
        return ""
    return f"{s[:2]}** **** **** **{s[-2:]}"


def password_in_clipboard() -> str:
    from app import telegram
    return app_password_in(telegram.clipboard_text() or "")


def host_for(address: str):
    addr = (address or "").strip()
    if addr.count("@") != 1 or any(ch.isspace() or ord(ch) < 32 for ch in addr):
        raise MailError("보내는 메일은 Gmail만 됩니다. 받는 메일은 아무 주소나 됩니다.")
    local, dom = addr.split("@", 1)
    if not local or dom.lower() not in GMAIL_DOMAINS:
        raise MailError("보내는 메일은 Gmail만 됩니다. 받는 메일은 아무 주소나 됩니다.")
    return GMAIL_SMTP


def _connect(address: str, password: str):
    host, port, implicit = host_for(address)
    ctx = ssl.create_default_context()
    try:
        if implicit:
            c = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
        else:
            c = smtplib.SMTP(host, port, timeout=30)
            c.starttls(context=ctx)
        c.login(address, password)
        return c
    except smtplib.SMTPAuthenticationError:
        raise MailError("로그인 거절. 메일 비밀번호가 아니라 앱 비밀번호를 넣었는지 확인하세요.") from None
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"메일 서버 연결 실패 ({host}, {type(e).__name__})") from None


def send(subject: str, text: str, address: str | None = None, password: str | None = None):
    address = (address or settings.load().get("email_address") or "").strip()
    password = password or settings.get_secret("email_password")
    if not address or not password:
        raise MailError("이메일이 연결되지 않았습니다.")
    host_for(address)
    m = EmailMessage()
    m["From"] = address
    m["To"] = (settings.load().get("email_to") or "").strip() or address  # any inbox; empty: the sender itself
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m.set_content(text)
    c = _connect(address, password)
    try:
        c.send_message(m)
    finally:
        try:
            c.quit()
        except Exception:
            pass


def connect(address: str, password: str) -> dict:
    """Check the sending Gmail account by sending a test mail to the receiving address, then keep it.
    Only Gmail sends (owner 2026-11-17: one way to explain; Naver needs 2-step plus its own app password); the receiving
    address (settings email_to) can be any inbox."""
    address, password = address.strip(), password.replace(" ", "").strip()
    if "@" not in address or not password:
        return {"ok": False, "error": "보내는 Gmail 주소와 앱 비밀번호를 넣으세요."}
    if provider(address) != "google":
        return {"ok": False, "error": "보내는 메일은 Gmail만 됩니다. 받는 메일은 아무 주소나 됩니다."}
    try:
        send("Entry Pilot 연결됨", "이 주소로 Entry Pilot 알림을 보냅니다. 답은 Entry Pilot 창의 확인할 일 탭에서 합니다.",
             address, password)
    except MailError as e:
        return {"ok": False, "error": str(e)}
    settings.set_secret("email_password", password)
    settings.update(email_address=address)
    return {"ok": True}
