import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

# ── Browser ───────────────────────────────────────────────────────────────────
HEADLESS: bool = os.getenv("HEADLESS", "True").lower() in ("true", "1", "yes")

# ── Kleinanzeigen ─────────────────────────────────────────────────────────────
KA_REGISTER_URL = "https://www.kleinanzeigen.de/m-registrierung-zugeordnet.html"
KA_BASE_URL     = "https://www.kleinanzeigen.de"

# ── Email verification ─────────────────────────────────────────────────────────
EMAIL_CHECK_TIMEOUT: int = int(os.getenv("EMAIL_CHECK_TIMEOUT", "180"))

# ── IMAP servers map ──────────────────────────────────────────────────────────
IMAP_SERVERS: dict[str, tuple[str, int]] = {
    "gmail.com":       ("imap.gmail.com", 993),
    "googlemail.com":  ("imap.gmail.com", 993),
    "outlook.com":     ("outlook.office365.com", 993),
    "hotmail.com":     ("outlook.office365.com", 993),
    "live.com":        ("outlook.office365.com", 993),
    "msn.com":         ("outlook.office365.com", 993),
    "yahoo.com":       ("imap.mail.yahoo.com", 993),
    "yahoo.de":        ("imap.mail.yahoo.com", 993),
    "web.de":          ("imap.web.de", 993),
    "gmx.de":          ("imap.gmx.net", 993),
    "gmx.com":         ("imap.gmx.net", 993),
    "gmx.net":         ("imap.gmx.net", 993),
    "t-online.de":     ("secureimap.t-online.de", 993),
    "icloud.com":      ("imap.mail.me.com", 993),
    "me.com":          ("imap.mail.me.com", 993),
    "mail.ru":         ("imap.mail.ru", 993),
    "yandex.ru":       ("imap.yandex.ru", 993),
    "rambler.ru":      ("imap.rambler.ru", 993),
}
