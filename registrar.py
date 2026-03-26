"""
registrar.py — Автоматическая регистрация аккаунта на kleinanzeigen.de
через Playwright (Chromium).
"""
from __future__ import annotations

import asyncio
import random
import string
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from config import HEADLESS, KA_REGISTER_URL, KA_BASE_URL


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def generate_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    while True:
        pwd = "".join(random.choices(chars, k=length))
        has_upper = any(c.isupper() for c in pwd)
        has_lower = any(c.islower() for c in pwd)
        has_digit = any(c.isdigit() for c in pwd)
        has_spec  = any(c in "!@#$%" for c in pwd)
        if has_upper and has_lower and has_digit and has_spec:
            return pwd


def parse_proxy(proxy_str: str) -> Optional[dict]:
    """
    Форматы:
      ip:port
      ip:port:login:pass
      http://ip:port
      http://login:pass@ip:port
    """
    if not proxy_str or proxy_str.strip().lower() in ("нет", "no", "-", "none"):
        return None

    s = proxy_str.strip()

    # Already has scheme
    if "://" in s:
        return {"server": s}

    parts = s.split(":")
    if len(parts) == 2:
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    if len(parts) == 4:
        ip, port, login, password = parts
        return {
            "server":   f"http://{ip}:{port}",
            "username": login,
            "password": password,
        }
    # 3 parts ambiguous — treat as ip:port:login (no pass)
    if len(parts) == 3:
        ip, port, login = parts
        return {"server": f"http://{ip}:{port}", "username": login, "password": ""}

    return {"server": f"http://{s}"}


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegistrationResult:
    success: bool = False
    email: str = ""
    password: str = ""
    error: str = ""
    needs_sms: bool = False
    logs: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Registrar
# ─────────────────────────────────────────────────────────────────────────────

class KleinanzeigenRegistrar:

    # Stealth script: mask webdriver flag
    _STEALTH_JS = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """

    def __init__(self, proxy_str: Optional[str] = None):
        self.proxy_config = parse_proxy(proxy_str) if proxy_str else None
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.logs: list[str] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "KleinanzeigenRegistrar":
        await self._start()
        return self

    async def __aexit__(self, *_):
        await self._stop()

    async def _start(self):
        self._pw = await async_playwright().start()
        launch_kwargs: dict = {
            "headless": HEADLESS,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=de-DE",
            ],
        }
        if self.proxy_config:
            launch_kwargs["proxy"] = self.proxy_config

        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        await self._context.add_init_script(self._STEALTH_JS)
        self.page = await self._context.new_page()

    async def _stop(self):
        try:
            if self.page:        await self.page.close()
            if self._context:   await self._context.close()
            if self._browser:   await self._browser.close()
            if self._pw:        await self._pw.stop()
        except Exception:
            pass

    # ── internal helpers ──────────────────────────────────────────────────────

    def _log(self, msg: str):
        self.logs.append(msg)

    async def _human_type(self, selector: str, text: str):
        await self.page.click(selector)
        for ch in text:
            await self.page.keyboard.type(ch, delay=random.randint(40, 120))

    async def _dismiss_cookie_banner(self):
        selectors = [
            "#gdpr-banner-accept",
            "button[data-testid='uc-accept-all-button']",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Accept all')",
            "button:has-text('Akzeptieren')",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        ]
        for sel in selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

    # ── registration flow ─────────────────────────────────────────────────────

    async def fill_registration_form(
        self, email: str, password: str
    ) -> bool:
        """
        Переходит на страницу регистрации и заполняет форму.
        Возвращает True если форма успешно отправлена.
        """
        self._log("Открываю страницу регистрации…")
        await self.page.goto(KA_BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(1, 2))
        await self._dismiss_cookie_banner()

        await self.page.goto(KA_REGISTER_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(1.5, 2.5))
        await self._dismiss_cookie_banner()

        self._log("Заполняю email…")
        email_selectors = [
            'input[name="email"]',
            'input[type="email"]',
            'input[id*="email"]',
            'input[placeholder*="E-Mail"]',
            'input[placeholder*="email" i]',
        ]
        email_filled = False
        for sel in email_selectors:
            try:
                if await self.page.locator(sel).first.is_visible(timeout=3000):
                    await self._human_type(sel, email)
                    email_filled = True
                    break
            except Exception:
                continue

        if not email_filled:
            self._log("ОШИБКА: Поле email не найдено")
            return False

        await asyncio.sleep(random.uniform(0.5, 1.0))

        self._log("Заполняю пароль…")
        pass_selectors = [
            'input[name="password"]',
            'input[type="password"]',
            'input[id*="password"]',
            'input[id*="passwort"]',
        ]
        pass_filled = False
        for sel in pass_selectors:
            try:
                if await self.page.locator(sel).first.is_visible(timeout=3000):
                    await self._human_type(sel, password)
                    pass_filled = True
                    break
            except Exception:
                continue

        if not pass_filled:
            self._log("ОШИБКА: Поле пароля не найдено")
            return False

        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Accept terms checkbox if present
        terms_selectors = [
            'input[name="termsAndConditions"]',
            'input[id*="terms"]',
            'input[id*="agb"]',
            'input[type="checkbox"]',
        ]
        for sel in terms_selectors:
            try:
                chk = self.page.locator(sel).first
                if await chk.is_visible(timeout=2000):
                    if not await chk.is_checked():
                        await chk.click()
                    break
            except Exception:
                continue

        await asyncio.sleep(random.uniform(0.5, 1.0))

        self._log("Отправляю форму…")
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Registrieren")',
            'button:has-text("Konto erstellen")',
            'button:has-text("Weiter")',
        ]
        submitted = False
        for sel in submit_selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            self._log("ОШИБКА: Кнопка отправки не найдена")
            return False

        await asyncio.sleep(3)
        self._log("Форма отправлена, ожидаю ответ сервера…")
        return True

    async def open_verification_link(self, link: str) -> bool:
        """Открывает ссылку верификации email."""
        self._log(f"Открываю ссылку верификации…")
        try:
            await self.page.goto(link, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            self._log("Ссылка верификации открыта.")
            return True
        except Exception as e:
            self._log(f"Ошибка открытия ссылки: {e}")
            return False

    async def enter_phone_number(self, phone: str) -> bool:
        """Вводит номер телефона на странице добавления номера."""
        self._log(f"Ввожу номер телефона: {phone}")
        phone_selectors = [
            'input[name="phone"]',
            'input[type="tel"]',
            'input[id*="phone"]',
            'input[id*="telefon"]',
            'input[placeholder*="Telefon"]',
            'input[placeholder*="Handynummer"]',
        ]
        for sel in phone_selectors:
            try:
                fld = self.page.locator(sel).first
                if await fld.is_visible(timeout=5000):
                    await fld.click()
                    await fld.fill("")
                    await self._human_type(sel, phone)
                    await asyncio.sleep(0.5)
                    # Submit
                    submit = [
                        'button[type="submit"]',
                        'button:has-text("SMS senden")',
                        'button:has-text("Weiter")',
                        'button:has-text("Bestätigen")',
                    ]
                    for ss in submit:
                        try:
                            sbtn = self.page.locator(ss).first
                            if await sbtn.is_visible(timeout=2000):
                                await sbtn.click()
                                self._log("Номер телефона отправлен.")
                                return True
                        except Exception:
                            continue
            except Exception:
                continue
        self._log("Поле телефона не найдено — возможно, шаг пропущен.")
        return False

    async def enter_sms_code(self, code: str) -> bool:
        """Вводит SMS-код подтверждения."""
        self._log(f"Ввожу SMS-код…")
        code_selectors = [
            'input[name="smsCode"]',
            'input[name="code"]',
            'input[id*="code"]',
            'input[id*="sms"]',
            'input[type="number"]',
            'input[inputmode="numeric"]',
        ]
        for sel in code_selectors:
            try:
                fld = self.page.locator(sel).first
                if await fld.is_visible(timeout=5000):
                    await fld.fill(code.strip())
                    await asyncio.sleep(0.5)
                    submit = [
                        'button[type="submit"]',
                        'button:has-text("Bestätigen")',
                        'button:has-text("Weiter")',
                        'button:has-text("Verifizieren")',
                    ]
                    for ss in submit:
                        try:
                            sbtn = self.page.locator(ss).first
                            if await sbtn.is_visible(timeout=2000):
                                await sbtn.click()
                                await asyncio.sleep(2)
                                self._log("SMS-код введён.")
                                return True
                        except Exception:
                            continue
            except Exception:
                continue
        self._log("Поле SMS-кода не найдено.")
        return False

    async def current_url(self) -> str:
        return self.page.url if self.page else ""
