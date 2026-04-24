import json
import re
import time
import random
import logging
import threading
import base64
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

_browser_instance = None
_pw_instance = None
_browser_lock = threading.Lock()
_browser_launch_time = 0
_BROWSER_MAX_AGE = 300


def _get_browser():
    global _browser_instance, _pw_instance, _browser_launch_time
    with _browser_lock:
        now = time.time()
        if _browser_instance and (now - _browser_launch_time) < _BROWSER_MAX_AGE:
            try:
                _browser_instance.contexts
                return _browser_instance
            except Exception:
                _browser_instance = None

        if _browser_instance:
            try:
                _browser_instance.close()
            except Exception:
                pass
            _browser_instance = None

        if _pw_instance:
            try:
                _pw_instance.stop()
            except Exception:
                pass
            _pw_instance = None

        _pw_instance = sync_playwright().start()
        _browser_instance = _pw_instance.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--window-size=1920,1080',
            ]
        )
        _browser_launch_time = now
        logger.info("[HybridBT] Playwright browser launched (headless)")
        return _browser_instance


def _make_stealth_context(browser):
    viewports = [
        {"width": 1920, "height": 1080},
        {"width": 1366, "height": 768},
        {"width": 1536, "height": 864},
        {"width": 1440, "height": 900},
        {"width": 1280, "height": 720},
    ]
    locales = ['en-US', 'en-GB', 'en-CA', 'en-AU']
    timezones = ['America/New_York', 'America/Chicago', 'America/Los_Angeles', 'America/Denver', 'Europe/London']
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
    ]

    vp = random.choice(viewports)
    ctx = browser.new_context(
        viewport=vp,
        screen=vp,
        user_agent=random.choice(user_agents),
        locale=random.choice(locales),
        timezone_id=random.choice(timezones),
        color_scheme=random.choice(['light', 'dark']),
        has_touch=False,
        java_script_enabled=True,
        bypass_csp=False,
        ignore_https_errors=True,
    )

    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        window.chrome = {runtime: {}};
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : origQuery(params);
    """)

    return ctx


def _extract_cookies_for_requests(context):
    cookies = context.cookies()
    cookie_dict = {}
    for c in cookies:
        cookie_dict[c['name']] = c['value']
    return cookie_dict, cookies


def _transfer_cookies_to_session(session, cookie_dict, raw_cookies=None):
    for name, value in cookie_dict.items():
        session.cookies.set(name, value)
    if raw_cookies:
        for c in raw_cookies:
            domain = c.get('domain', '')
            path = c.get('path', '/')
            session.cookies.set(c['name'], c['value'], domain=domain, path=path)
    return session


def _extract_bt_token_from_html(html):
    patterns = [
        r'"braintreeClientToken"\s*:\s*"([^"]+)"',
        r'"braintree_client_token"\s*:\s*"([^"]+)"',
        r'braintreeClientToken\s*=\s*["\']([^"\']+)["\']',
        r'data-braintree-token\s*=\s*["\']([^"\']+)["\']',
        r'clientToken\s*[=:]\s*["\']([A-Za-z0-9+/=]{50,})["\']',
        r'client_token\s*[=:]\s*["\']([A-Za-z0-9+/=]{50,})["\']',
        r'BraintreeClient\.create\(\{[^}]*authorization\s*:\s*["\']([^"\']+)["\']',
        r'"authorization"\s*:\s*"([A-Za-z0-9+/=]{50,})"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def _extract_csrf_from_html(html):
    csrf_patterns = [
        r'<meta\s+name="csrf-token"\s+content="([^"]+)"',
        r'csrf-token["\s]+content="([^"]+)"',
        r'content="([^"]+)"[^>]*name="csrf-token"',
        r'<meta\s+content="([^"]+)"\s+name="csrf-token"',
        r'"csrf[_-]?token"\s*:\s*"([^"]+)"',
        r"'csrf[_-]?token'\s*:\s*'([^']+)'",
        r'name="authenticity_token"\s+value="([^"]+)"',
        r'value="([^"]+)"\s+name="authenticity_token"',
        r'"authenticity_token"\s*:\s*"([^"]+)"',
    ]
    for pat in csrf_patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def hybrid_bt_session(site_url, checkout_path="/checkout/onepage"):
    result = {
        "success": False,
        "cookies": {},
        "raw_cookies": [],
        "csrf_token": None,
        "bt_client_token": None,
        "auth_fingerprint": None,
        "page_html": None,
        "checkout_html": None,
        "error": None,
        "cf_bypassed": False,
        "fingerprint_signals": [],
    }

    context = None
    try:
        browser = _get_browser()
        context = _make_stealth_context(browser)
        page = context.new_page()

        logger.info(f"[HybridBT] Navigating to {site_url}")
        page.goto(site_url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(random.uniform(0.3, 0.8))

        page_html = page.content()

        if any(sig in page_html.lower() for sig in ['just a moment', 'cloudflare', 'cf-browser-verification', 'challenge-platform']):
            logger.info("[HybridBT] Cloudflare challenge detected, waiting...")
            result["cf_bypassed"] = True
            try:
                page.wait_for_function(
                    "() => !document.title.toLowerCase().includes('just a moment') && !document.querySelector('#challenge-running')",
                    timeout=20000
                )
                time.sleep(random.uniform(0.5, 1.0))
                page_html = page.content()
                logger.info("[HybridBT] Cloudflare challenge resolved")
                result["fingerprint_signals"].append("cf_challenge_bypassed")
            except PWTimeout:
                logger.warning("[HybridBT] Cloudflare challenge not resolved in time")
                result["error"] = "Cloudflare challenge not resolved"
                return result

        result["page_html"] = page_html
        result["fingerprint_signals"].append("canvas_webgl_hardware:active")

        csrf = _extract_csrf_from_html(page_html)
        if csrf:
            result["csrf_token"] = csrf
            logger.info(f"[HybridBT] CSRF token found ({len(csrf)} chars)")

        if not csrf:
            csrf_js = page.evaluate("""
                () => {
                    const meta = document.querySelector('meta[name="csrf-token"]');
                    if (meta) return meta.getAttribute('content');
                    const inp = document.querySelector('input[name="authenticity_token"]');
                    if (inp) return inp.value;
                    return null;
                }
            """)
            if csrf_js:
                result["csrf_token"] = csrf_js
                logger.info(f"[HybridBT] CSRF token via JS DOM ({len(csrf_js)} chars)")

        bt_token = _extract_bt_token_from_html(page_html)
        if bt_token:
            result["bt_client_token"] = bt_token
            logger.info("[HybridBT] BT client token found on homepage")

        if not bt_token:
            bt_token_js = page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        const text = s.textContent || s.innerText || '';
                        const m = text.match(/(?:braintreeClientToken|braintree_client_token|clientToken|client_token|authorization)['"\\s:=]+['"\\s]*([A-Za-z0-9+/=]{50,})/);
                        if (m) return m[1];
                    }
                    if (window.__NEXT_DATA__) {
                        const str = JSON.stringify(window.__NEXT_DATA__);
                        const m = str.match(/(?:braintreeClientToken|client_token|clientToken)['"\\s:]+['"\\s]*([A-Za-z0-9+/=]{50,})/);
                        if (m) return m[1];
                    }
                    return null;
                }
            """)
            if bt_token_js:
                bt_token = bt_token_js
                result["bt_client_token"] = bt_token
                logger.info("[HybridBT] BT client token via JS extraction")

        if not bt_token:
            logger.info("[HybridBT] No BT token on homepage, navigating to checkout...")
            checkout_url = f"{site_url.rstrip('/')}{checkout_path}"
            try:
                page.goto(checkout_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(random.uniform(0.3, 0.6))
                checkout_html = page.content()
                result["checkout_html"] = checkout_html

                bt_token = _extract_bt_token_from_html(checkout_html)
                if not bt_token:
                    bt_token = page.evaluate("""
                        () => {
                            const scripts = document.querySelectorAll('script');
                            for (const s of scripts) {
                                const text = s.textContent || s.innerText || '';
                                const m = text.match(/(?:braintreeClientToken|braintree_client_token|clientToken|client_token|authorization)['"\\s:=]+['"\\s]*([A-Za-z0-9+/=]{50,})/);
                                if (m) return m[1];
                            }
                            return null;
                        }
                    """)

                if bt_token:
                    result["bt_client_token"] = bt_token
                    logger.info("[HybridBT] BT client token found on checkout page")

                csrf2 = _extract_csrf_from_html(checkout_html)
                if csrf2 and not result["csrf_token"]:
                    result["csrf_token"] = csrf2
            except Exception as e:
                logger.warning(f"[HybridBT] Checkout navigation failed: {str(e)[:60]}")

        if result["bt_client_token"]:
            try:
                raw = result["bt_client_token"]
                if not raw.endswith('='):
                    raw += '=' * (4 - len(raw) % 4)
                decoded = base64.b64decode(raw)
                bt_json = json.loads(decoded)
                af = bt_json.get('authorizationFingerprint', '')
                if af:
                    result["auth_fingerprint"] = af
                    logger.info("[HybridBT] Auth fingerprint extracted from BT token")
            except Exception as e:
                logger.warning(f"[HybridBT] BT token decode failed: {str(e)[:60]}")

        cookie_dict, raw_cookies = _extract_cookies_for_requests(context)
        result["cookies"] = cookie_dict
        result["raw_cookies"] = raw_cookies
        result["fingerprint_signals"].append(f"cookies_captured:{len(cookie_dict)}")

        cf_cookies = [k for k in cookie_dict if k.startswith('cf_') or k.startswith('__cf') or k == 'cf_clearance']
        if cf_cookies:
            result["cf_bypassed"] = True
            result["fingerprint_signals"].append(f"cf_cookies:{len(cf_cookies)}")

        has_essentials = result["csrf_token"] or result["auth_fingerprint"]
        result["success"] = bool(has_essentials or result["cookies"])

        logger.info(
            f"[HybridBT] Session result: success={result['success']}, "
            f"csrf={'YES' if result['csrf_token'] else 'NO'}, "
            f"bt_token={'YES' if result['bt_client_token'] else 'NO'}, "
            f"auth_fp={'YES' if result['auth_fingerprint'] else 'NO'}, "
            f"cookies={len(cookie_dict)}, cf_bypass={result['cf_bypassed']}"
        )

    except Exception as e:
        logger.error(f"[HybridBT] Error: {str(e)[:100]}")
        result["error"] = str(e)[:100]
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass

    return result


def cleanup_browser():
    global _browser_instance, _pw_instance
    with _browser_lock:
        if _browser_instance:
            try:
                _browser_instance.close()
            except Exception:
                pass
            _browser_instance = None
        if _pw_instance:
            try:
                _pw_instance.stop()
            except Exception:
                pass
            _pw_instance = None
            logger.info("[HybridBT] Playwright browser and driver closed")
