"""
registrar.py — Автоматическая регистрация аккаунта на kleinanzeigen.de
через Playwright (Chromium).

Флоу:
  1. kleinanzeigen.de/m-benutzer-anmeldung.html
     → редирект на login.kleinanzeigen.de (OAuth)
  2. Принять куки
  3. Заполнить email → Weiter → заполнить пароль → Submit
  4. Дождаться письма верификации (IMAP)
  5. Перейти по ссылке верификации
  6. (Опционально) ввести номер телефона → SMS-код
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import string
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from config import HEADLESS, KA_REGISTER_URL, KA_BASE_URL

logger = logging.getLogger(__name__)

# Папка для скриншотов при ошибках
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)


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
    """
    Регистрация аккаунта на kleinanzeigen.de.

    Фактический Flow сайта (2025/2026):
      kleinanzeigen.de/m-benutzer-anmeldung.html
        -> redirect -> login.kleinanzeigen.de (OAuth, Auth0-style)
      На странице login.kleinanzeigen.de:
        - Поле email + кнопка "Weiter"
        - Затем поле пароля
      Если аккаунт не найден -> предложение создать
      После регистрации -> письмо верификации -> ссылка подтверждения
    """

    _STEALTH_JS = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
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
        self._log("Запуск Playwright…")
        self._pw = await async_playwright().start()

        launch_kwargs: dict = {
            "headless": HEADLESS,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--lang=de-DE,de",
                "--window-size=1366,768",
            ],
            "timeout": 30000,
        }
        if self.proxy_config:
            launch_kwargs["proxy"] = self.proxy_config
            self._log(f"Прокси: {self.proxy_config.get('server')}")
        else:
            self._log("Прокси: не используется (прямое подключение)")

        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._log("Браузер запущен")

        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="de-DE",
            timezone_id="Europe/Berlin",
            java_script_enabled=True,
            accept_downloads=False,
            ignore_https_errors=False,
        )
        await self._context.add_init_script(self._STEALTH_JS)
        self.page = await self._context.new_page()

        # Логирование всех запросов / ответов
        self.page.on("request",  lambda r: logger.debug(f"[REQ] {r.method} {r.url[:120]}"))
        self.page.on("response", lambda r: logger.debug(f"[RES] {r.status} {r.url[:120]}"))
        self.page.on("console",  lambda m: logger.debug(f"[JS]  {m.type}: {m.text[:200]}"))
        self.page.on("pageerror", lambda e: self._log(f"[PageError] {e}"))

        self._log("Контекст браузера создан, страница открыта")

    async def _stop(self):
        try:
            if self.page:      await self.page.close()
            if self._context:  await self._context.close()
            if self._browser:  await self._browser.close()
            if self._pw:       await self._pw.stop()
        except Exception:
            pass

    # ── internal helpers ──────────────────────────────────────────────────────

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        logger.info(entry)

    async def _snapshot(self, label: str):
        """Сохранить скриншот и залогировать текущий URL / заголовок страницы."""
        url   = self.page.url if self.page else "?"
        title = await self.page.title() if self.page else "?"
        self._log(f"[{label}] URL: {url}")
        self._log(f"[{label}] Title: {title}")
        try:
            fname = SCREENSHOT_DIR / f"{datetime.now().strftime('%H%M%S')}_{label}.png"
            await self.page.screenshot(path=str(fname), full_page=True)
            self._log(f"[{label}] Скриншот: {fname}")
        except Exception as e:
            self._log(f"[{label}] Скриншот не удался: {e}")

    async def _human_type(self, locator, text: str):
        """Имитация ручного набора: случайные задержки между символами."""
        await locator.click()
        await asyncio.sleep(random.uniform(0.1, 0.3))
        for ch in text:
            await locator.press_sequentially(ch, delay=random.randint(50, 130))

    async def _goto_with_retry(self, url: str, retries: int = 3, timeout: int = 30000):
        """Переход по URL с 3 попытками и логированием."""
        for attempt in range(1, retries + 1):
            self._log(f"Перехожу: {url} (попытка {attempt}/{retries})")
            try:
                resp = await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                status = resp.status if resp else "?"
                self._log(f"HTTP статус: {status}")
                await self._snapshot("goto")
                return resp
            except PlaywrightTimeout:
                self._log(f"Таймаут при переходе на {url} (попытка {attempt})")
                await self._snapshot(f"timeout_{attempt}")
                if attempt == retries:
                    raise
                await asyncio.sleep(3)
            except Exception as e:
                self._log(f"Ошибка перехода на {url}: {e} (попытка {attempt})")
                await self._snapshot(f"error_{attempt}")
                if attempt == retries:
                    raise
                await asyncio.sleep(3)

    async def _dismiss_cookie_banner(self):
        """Закрыть баннер cookie если он есть."""
        selectors = [
            # Kleinanzeigen / Sourcepoint CMP
            "button[title='Alle akzeptieren']",
            "button[data-testid='uc-accept-all-button']",
            "button.sp_choice_type_11",   # Sourcepoint "Accept All"
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Mit Werbung und Tracking nutzen')",
            "button:has-text('Accept all')",
            "button:has-text('Akzeptieren')",
            # Generic
            "#gdpr-banner-accept",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        ]
        self._log("Проверяю баннер cookies…")
        for sel in selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    self._log(f"Баннер cookies найден: {sel!r} → кликаю")
                    await btn.click()
                    await asyncio.sleep(1.5)
                    self._log("Баннер cookies закрыт")
                    return
            except Exception:
                continue
        self._log("Баннер cookies не обнаружен (или уже закрыт)")

    async def _dump_visible_inputs(self):
        """Логирует все видимые input/button на странице для отладки."""
        try:
            inputs = await self.page.evaluate("""() => {
                const els = document.querySelectorAll('input, button, a[href*="register"], a[href*="anmeld"]');
                return Array.from(els).slice(0, 20).map(e => ({
                    tag:  e.tagName,
                    type: e.type || '',
                    name: e.name || '',
                    id:   e.id   || '',
                    placeholder: e.placeholder || '',
                    text: (e.innerText || e.value || '').slice(0, 50),
                    visible: e.offsetParent !== null,
                    href: e.href || '',
                }));
            }""")
            self._log("Видимые элементы на странице:")
            for el in inputs:
                if el.get("visible"):
                    self._log(f"  <{el['tag']}> type={el['type']!r} name={el['name']!r} "
                              f"id={el['id']!r} placeholder={el['placeholder']!r} "
                              f"text={el['text']!r} href={el['href']!r}")
        except Exception as e:
            self._log(f"dump_visible_inputs error: {e}")

    # ── registration flow ─────────────────────────────────────────────────────

    async def fill_registration_form(self, email: str, password: str) -> bool:
        """
        Основной метод регистрации.
        Returns True если форма успешно отправлена (не обязательно завершена).
        """
        self._log("=" * 60)
        self._log(f"СТАРТ РЕГИСТРАЦИИ | email={email}")
        self._log("=" * 60)

        # ── Шаг 1: Открываем страницу регистрации ─────────────────────────────
        self._log("Шаг 1: Перехожу на страницу входа/регистрации…")
        try:
            await self._goto_with_retry(KA_REGISTER_URL, retries=3, timeout=30000)
        except Exception as e:
            self._log(f"КРИТИЧНО: Не удалось открыть {KA_REGISTER_URL}: {e}")
            return False

        await asyncio.sleep(random.uniform(1.5, 2.5))
        await self._dismiss_cookie_banner()
        await self._dump_visible_inputs()

        # ── Шаг 2: Если редирект на login.kleinanzeigen.de — ищем ссылку регистрации
        current = self.page.url
        self._log(f"Шаг 2: Текущий URL после редиректа: {current}")

        if "login.kleinanzeigen.de" in current or "m-einloggen" in current:
            self._log("Обнаружен OAuth-экран входа. Ищу ссылку 'Erstelle ein Konto'…")
            reg_link_selectors = [
                "a:has-text('Erstelle ein Konto')",
                "a:has-text('Registrieren')",
                "a:has-text('Konto erstellen')",
                "a[href*='register']",
                "a[href*='anmeldung']",
                "a[href*='signup']",
            ]
            found_reg_link = False
            for sel in reg_link_selectors:
                try:
                    lnk = self.page.locator(sel).first
                    if await lnk.is_visible(timeout=3000):
                        href = await lnk.get_attribute("href") or ""
                        self._log(f"Ссылка регистрации найдена: {sel!r} → {href}")
                        await lnk.click()
                        await asyncio.sleep(2)
                        await self._snapshot("after_reg_link_click")
                        found_reg_link = True
                        break
                except Exception:
                    continue

            if not found_reg_link:
                self._log("Ссылка 'Erstelle ein Konto' не найдена — пробую ввести email напрямую")

        await self._dismiss_cookie_banner()
        await asyncio.sleep(random.uniform(1, 1.5))
        await self._dump_visible_inputs()

        # ── Шаг 3: Ввод email ────────────────────────────────────────────────
        self._log(f"Шаг 3: Ввожу email: {email}")
        email_selectors = [
            'input[name="username"]',      # Auth0 / OAuth стандарт
            'input[name="email"]',
            'input[type="email"]',
            'input[id="username"]',
            'input[id="email"]',
            'input[id*="email" i]',
            'input[placeholder*="E-Mail" i]',
            'input[placeholder*="email" i]',
            'input[autocomplete="email"]',
        ]
        email_filled = False
        for sel in email_selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    self._log(f"Поле email найдено: {sel!r}")
                    await loc.clear()
                    await self._human_type(loc, email)
                    filled_val = await loc.input_value()
                    self._log(f"Email введён, значение в поле: {filled_val!r}")
                    email_filled = True
                    break
            except Exception as e:
                self._log(f"Селектор email {sel!r} не сработал: {e}")

        if not email_filled:
            self._log("ОШИБКА: Поле email не найдено ни по одному селектору")
            await self._snapshot("email_not_found")
            return False

        await asyncio.sleep(random.uniform(0.5, 1.0))

        # ── Шаг 4: Нажать "Weiter" если email и пароль на разных экранах ─────
        weiter_selectors = [
            'button[type="submit"]:has-text("Weiter")',
            'button:has-text("Weiter")',
            'button:has-text("Fortfahren")',
            'button:has-text("Nächster Schritt")',
            'button[name="action"][value="default"]',
        ]
        weiter_clicked = False
        for sel in weiter_selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    self._log(f"Кнопка 'Weiter' найдена: {sel!r} → кликаю")
                    await btn.click()
                    await asyncio.sleep(2)
                    await self._snapshot("after_weiter")
                    weiter_clicked = True
                    break
            except Exception:
                continue

        if not weiter_clicked:
            self._log("Кнопка 'Weiter' не найдена — оба поля, вероятно, на одной странице")

        await self._dump_visible_inputs()
        await self._dismiss_cookie_banner()

        # ── Шаг 5: Ввод пароля ───────────────────────────────────────────────
        self._log("Шаг 5: Ввожу пароль…")
        pass_selectors = [
            'input[name="password"]',
            'input[type="password"]',
            'input[id="password"]',
            'input[id*="password" i]',
            'input[id*="passwort" i]',
            'input[autocomplete="new-password"]',
            'input[autocomplete="current-password"]',
        ]
        pass_filled = False
        for sel in pass_selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.is_visible(timeout=4000):
                    self._log(f"Поле пароля найдено: {sel!r}")
                    await loc.clear()
                    await self._human_type(loc, password)
                    self._log("Пароль введён")
                    pass_filled = True
                    break
            except Exception as e:
                self._log(f"Селектор пароля {sel!r} не сработал: {e}")

        if not pass_filled:
            self._log("ОШИБКА: Поле пароля не найдено")
            await self._snapshot("pass_not_found")
            return False

        await asyncio.sleep(random.uniform(0.5, 1.0))

        # ── Шаг 6: Чекбокс условий использования ─────────────────────────────
        terms_selectors = [
            'input[name="termsAndConditions"]',
            'input[name="terms"]',
            'input[id*="terms" i]',
            'input[id*="agb" i]',
        ]
        for sel in terms_selectors:
            try:
                chk = self.page.locator(sel).first
                if await chk.is_visible(timeout=2000):
                    checked = await chk.is_checked()
                    self._log(f"Чекбокс условий {sel!r}: checked={checked}")
                    if not checked:
                        await chk.click()
                        self._log("Чекбокс установлен")
                    break
            except Exception:
                continue

        await asyncio.sleep(random.uniform(0.5, 1.0))

        # ── Шаг 7: Отправка формы регистрации ────────────────────────────────
        self._log("Шаг 7: Отправляю форму…")
        await self._snapshot("before_submit")

        submit_selectors = [
            'button[type="submit"]:has-text("Konto erstellen")',
            'button[type="submit"]:has-text("Registrieren")',
            'button[type="submit"]:has-text("Anmelden")',
            'button[type="submit"]:has-text("Weiter")',
            'button[type="submit"]',
            'input[type="submit"]',
        ]
        submitted = False
        for sel in submit_selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    txt = await btn.text_content() or ""
                    self._log(f"Кнопка подтверждения: {sel!r} текст={txt.strip()!r} → кликаю")
                    await btn.click()
                    submitted = True
                    break
            except Exception as e:
                self._log(f"Submit {sel!r} не сработал: {e}")

        if not submitted:
            self._log("ОШИБКА: Кнопка отправки не найдена")
            await self._snapshot("submit_not_found")
            return False

        self._log("Форма отправлена — жду ответа сервера (5 сек)…")
        await asyncio.sleep(5)
        await self._snapshot("after_submit")

        # Проверяем нет ли ошибок прямо на странице
        await self._check_page_errors()

        url_after = self.page.url
        self._log(f"URL после отправки: {url_after}")
        self._log("Форма регистрации успешно отправлена")
        return True

    async def _check_page_errors(self):
        """Логирует сообщения об ошибках с текущей страницы."""
        error_selectors = [
            ".error-message", ".alert-error", "[class*='error']",
            "[class*='Error']", "[role='alert']", ".notification--error",
        ]
        for sel in error_selectors:
            try:
                els = self.page.locator(sel)
                count = await els.count()
                if count:
                    for i in range(min(count, 5)):
                        txt = await els.nth(i).text_content() or ""
                        if txt.strip():
                            self._log(f"[Ошибка на странице] {txt.strip()[:200]}")
            except Exception:
                continue

    async def open_verification_link(self, link: str) -> bool:
        """Открывает ссылку верификации email."""
        self._log(f"Шаг: Открываю ссылку верификации: {link[:80]}…")
        try:
            await self._goto_with_retry(link, retries=2, timeout=30000)
            await asyncio.sleep(2)
            await self._dismiss_cookie_banner()
            await self._snapshot("verification_link")
            self._log("Ссылка верификации открыта успешно")
            return True
        except Exception as e:
            self._log(f"Ошибка открытия ссылки верификации: {e}")
            await self._snapshot("verification_error")
            return False

    async def enter_phone_number(self, phone: str) -> bool:
        """Вводит номер телефона на странице добавления номера."""
        self._log(f"Шаг: Ввожу номер телефона: {phone}")
        await self._snapshot("before_phone")
        await self._dump_visible_inputs()

        phone_selectors = [
            'input[name="phone"]',
            'input[type="tel"]',
            'input[id*="phone" i]',
            'input[id*="telefon" i]',
            'input[placeholder*="Telefon" i]',
            'input[placeholder*="Handynummer" i]',
            'input[placeholder*="+49" i]',
        ]
        for sel in phone_selectors:
            try:
                fld = self.page.locator(sel).first
                if await fld.is_visible(timeout=5000):
                    self._log(f"Поле телефона найдено: {sel!r}")
                    await fld.clear()
                    await self._human_type(fld, phone)
                    self._log(f"Телефон введён: {phone}")
                    await asyncio.sleep(0.5)

                    submit_sel = [
                        'button:has-text("SMS senden")',
                        'button:has-text("Code senden")',
                        'button:has-text("Weiter")',
                        'button:has-text("Bestätigen")',
                        'button[type="submit"]',
                    ]
                    for ss in submit_sel:
                        try:
                            sbtn = self.page.locator(ss).first
                            if await sbtn.is_visible(timeout=2000):
                                txt = await sbtn.text_content() or ""
                                self._log(f"Кликаю кнопку отправки номера: {txt.strip()!r}")
                                await sbtn.click()
                                await asyncio.sleep(3)
                                await self._snapshot("after_phone_submit")
                                self._log("Номер телефона отправлен, ожидаю SMS")
                                return True
                        except Exception:
                            continue
            except Exception as e:
                self._log(f"Поле телефона {sel!r}: {e}")
                continue

        self._log("Поле телефона не найдено — шаг с телефоном пропущен или не требуется")
        return False

    async def enter_sms_code(self, code: str) -> bool:
        """Вводит SMS-код подтверждения."""
        self._log(f"Шаг: Ввожу SMS-код: {code}")
        await self._snapshot("before_sms")
        await self._dump_visible_inputs()

        code_selectors = [
            'input[name="smsCode"]',
            'input[name="code"]',
            'input[id*="code" i]',
            'input[id*="sms" i]',
            'input[type="number"]',
            'input[inputmode="numeric"]',
            'input[autocomplete="one-time-code"]',
        ]
        for sel in code_selectors:
            try:
                fld = self.page.locator(sel).first
                if await fld.is_visible(timeout=5000):
                    self._log(f"Поле SMS-кода найдено: {sel!r}")
                    await fld.clear()
                    await fld.fill(code.strip())
                    await asyncio.sleep(0.5)

                    submit_sel = [
                        'button:has-text("Bestätigen")',
                        'button:has-text("Weiter")',
                        'button:has-text("Verifizieren")',
                        'button[type="submit"]',
                    ]
                    for ss in submit_sel:
                        try:
                            sbtn = self.page.locator(ss).first
                            if await sbtn.is_visible(timeout=2000):
                                txt = await sbtn.text_content() or ""
                                self._log(f"Кликаю кнопку подтверждения SMS: {txt.strip()!r}")
                                await sbtn.click()
                                await asyncio.sleep(3)
                                await self._snapshot("after_sms")
                                self._log("SMS-код введён и подтверждён")
                                return True
                        except Exception:
                            continue
            except Exception as e:
                self._log(f"Поле SMS {sel!r}: {e}")
                continue

        self._log("Поле SMS-кода не найдено")
        await self._snapshot("sms_not_found")
        return False

    async def current_url(self) -> str:
        return self.page.url if self.page else ""
