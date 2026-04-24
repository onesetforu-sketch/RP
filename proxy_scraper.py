import asyncio
import logging
import os
import time
import random
import re
from aiohttp import ClientSession, ClientTimeout, TCPConnector

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=https&timeout=5000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXY_LIST/master/http.txt",
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt",
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/https_proxies.txt",
    "https://raw.githubusercontent.com/ErcinDedewormo/proxy-list/main/proxy-list/data.txt",
    "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Finder/master/all/proxy-list.txt",
    "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt",
    "https://www.proxy-list.download/api/v1?type=http",
    "https://www.proxy-list.download/api/v1?type=https",
    "https://api.openproxylist.xyz/http.txt",
    "https://proxyspace.pro/http.txt",
    "https://proxyspace.pro/https.txt",
]

PROXY_FILE = os.path.join(BASE_DIR, "proxies.txt")
SCRUBBED_FILE = os.path.join(BASE_DIR, "proxies_live.txt")

TEST_URLS = [
    "https://httpbin.org/ip",
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://api.my-ip.io/v2/ip.txt",
    "https://ipinfo.io/ip",
    "https://api64.ipify.org?format=json",
]

TARGET_LIVE = 80
REFILL_THRESHOLD = 15
MAX_WORKERS = 500
SCRUB_BATCH_SIZE = 1000
TEST_TIMEOUT = 3
MAX_LATENCY_MS = 3000

_scrubbed_proxies = []
_proxy_latencies = {}
_scrub_lock = asyncio.Lock() if hasattr(asyncio, 'Lock') else None
_scrub_stats = {
    'total_scraped': 0,
    'total_live': 0,
    'last_scrub': 0,
    'scrub_cycles': 0,
    'dead_removed': 0,
    'avg_latency': 0,
    'fastest_proxy': '',
    'sources_ok': 0,
    'sources_fail': 0,
}


def _get_scrub_lock():
    global _scrub_lock
    if _scrub_lock is None:
        _scrub_lock = asyncio.Lock()
    return _scrub_lock


def get_scrub_stats():
    return _scrub_stats.copy()


def get_scrubbed_proxies():
    return list(_scrubbed_proxies)


def get_live_count():
    return len(_scrubbed_proxies)


def get_proxy_latency(proxy):
    return _proxy_latencies.get(proxy, 0)


def remove_dead_proxy(proxy):
    global _scrubbed_proxies
    clean = proxy.replace('http://', '').replace('https://', '')
    if clean in _scrubbed_proxies:
        _scrubbed_proxies.remove(clean)
        _proxy_latencies.pop(clean, None)
        _scrub_stats['total_live'] = len(_scrubbed_proxies)
        _scrub_stats['dead_removed'] = _scrub_stats.get('dead_removed', 0) + 1
        if _scrubbed_proxies:
            with open(SCRUBBED_FILE, 'w') as f:
                for p in _scrubbed_proxies:
                    f.write(p + '\n')
        return True
    return False


def _parse_proxy_line(line):
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    line = re.sub(r'^https?://', '', line)
    parts = line.split(':')
    if len(parts) == 2:
        ip, port = parts
        if ip.replace('.', '').isdigit() and port.isdigit() and 1 <= int(port) <= 65535:
            octets = ip.split('.')
            if len(octets) == 4 and all(0 <= int(o) <= 255 for o in octets if o.isdigit()):
                return f"{ip}:{port}"
    elif len(parts) == 4:
        ip, port = parts[0], parts[1]
        if ip.replace('.', '').isdigit() and port.isdigit():
            return line
    return None


async def scrape_proxies():
    all_proxies = set()
    timeout = ClientTimeout(total=10)
    connector = TCPConnector(limit=100, ssl=False)
    sources_ok = 0
    sources_fail = 0

    async with ClientSession(timeout=timeout, connector=connector) as session:
        tasks = []
        for url in PROXY_SOURCES:
            tasks.append(_fetch_source(session, url))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, set) and len(result) > 0:
                all_proxies.update(result)
                sources_ok += 1
            else:
                sources_fail += 1

    _scrub_stats['sources_ok'] = sources_ok
    _scrub_stats['sources_fail'] = sources_fail

    if all_proxies:
        with open(PROXY_FILE, 'w') as f:
            for proxy in sorted(all_proxies):
                f.write(proxy + '\n')
        _scrub_stats['total_scraped'] = len(all_proxies)
        logger.info(f"Scraped {len(all_proxies)} raw proxies from {sources_ok}/{len(PROXY_SOURCES)} sources ({sources_fail} failed)")

    return list(all_proxies)


async def _fetch_source(session, url):
    proxies = set()
    try:
        headers = {
            'User-Agent': random.choice([
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
            ])
        }
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                text = await resp.text()
                for line in text.splitlines():
                    parsed = _parse_proxy_line(line)
                    if parsed:
                        proxies.add(parsed)
                logger.info(f"Scraped {len(proxies)} from {url[:55]}")
    except Exception as e:
        logger.debug(f"Source failed: {url[:40]} - {e}")
    return proxies


async def _test_proxy_with_session(session, proxy, timeout_sec=None):
    if timeout_sec is None:
        timeout_sec = TEST_TIMEOUT
    proxy_url = f"http://{proxy}" if not proxy.startswith('http') else proxy
    test_url = random.choice(TEST_URLS)
    try:
        start = time.time()
        async with session.get(test_url, proxy=proxy_url, timeout=ClientTimeout(total=timeout_sec), allow_redirects=False) as resp:
            if resp.status == 200:
                await resp.read()
                latency = round((time.time() - start) * 1000)
                if latency <= MAX_LATENCY_MS:
                    return proxy, True, latency
    except Exception:
        pass
    return proxy, False, 0


async def _scrub_batch_fast(proxies, target_needed, existing_live_set=None):
    if existing_live_set is None:
        existing_live_set = set()

    live = []
    dead_count = 0
    total_tested = 0

    connector = TCPConnector(limit=0, ssl=False, enable_cleanup_closed=True, force_close=True, ttl_dns_cache=60)
    timeout = ClientTimeout(total=TEST_TIMEOUT * 2, connect=TEST_TIMEOUT)

    async with ClientSession(connector=connector, timeout=timeout) as session:
        for batch_start in range(0, len(proxies), SCRUB_BATCH_SIZE):
            if len(live) >= target_needed:
                break

            batch = proxies[batch_start:batch_start + SCRUB_BATCH_SIZE]
            sem = asyncio.Semaphore(MAX_WORKERS)

            async def _limited_test(p):
                async with sem:
                    return await _test_proxy_with_session(session, p)

            results = await asyncio.gather(*[_limited_test(p) for p in batch], return_exceptions=True)

            for r in results:
                if isinstance(r, tuple):
                    proxy, is_live, latency = r
                    if is_live and proxy not in existing_live_set:
                        live.append((proxy, latency))
                        existing_live_set.add(proxy)
                    elif not is_live:
                        dead_count += 1
                else:
                    dead_count += 1

            total_tested += len(batch)
            live_pct = f"{(len(live) / max(1, total_tested) * 100):.1f}%"
            logger.info(f"Scrub: {total_tested}/{len(proxies)} tested | {len(live)} live ({live_pct}) | {dead_count} dead | need {max(0, target_needed - len(live))} more")

            if len(live) >= target_needed:
                logger.info(f"Target reached ({len(live)}/{target_needed}) - stopping early")
                break

    return live, dead_count


async def _verify_existing_fast(proxies):
    if not proxies:
        return [], 0

    still_live = []
    removed = 0

    connector = TCPConnector(limit=0, ssl=False, enable_cleanup_closed=True, force_close=True, ttl_dns_cache=60)
    timeout = ClientTimeout(total=TEST_TIMEOUT + 1, connect=TEST_TIMEOUT)
    sem = asyncio.Semaphore(MAX_WORKERS)

    async with ClientSession(connector=connector, timeout=timeout) as session:
        async def _test(p):
            async with sem:
                return await _test_proxy_with_session(session, p, timeout_sec=TEST_TIMEOUT)

        results = await asyncio.gather(*[_test(p) for p in proxies], return_exceptions=True)
        for r in results:
            if isinstance(r, tuple):
                proxy, is_live, latency = r
                if is_live:
                    still_live.append((proxy, latency))
                else:
                    removed += 1
            else:
                removed += 1

    logger.info(f"Verified existing: {len(still_live)} alive, {removed} dead (tested {len(proxies)})")
    return still_live, removed


async def full_scrape_and_scrub():
    global _scrubbed_proxies, _proxy_latencies

    lock = _get_scrub_lock()
    if lock.locked():
        logger.info("Scrub already in progress, skipping")
        return _scrubbed_proxies

    async with lock:
        t_start = time.time()

        raw = await scrape_proxies()
        if not raw:
            logger.warning("No proxies scraped - skipping scrub")
            return _scrubbed_proxies

        random.shuffle(raw)

        already_live = list(_scrubbed_proxies)
        verified_live, verify_removed = await _verify_existing_fast(already_live)
        already_live_set = set(p for p, _ in verified_live)

        all_live = list(verified_live)

        untested = [p for p in raw if p not in already_live_set]
        random.shuffle(untested)

        needed = max(0, TARGET_LIVE - len(all_live))

        logger.info(f"Target: {TARGET_LIVE} | Verified: {len(all_live)} | Need: {needed} | Pool: {len(untested)} | Workers: {MAX_WORKERS}")

        if needed > 0 and untested:
            new_live, new_dead = await _scrub_batch_fast(untested, needed, already_live_set)
            all_live.extend(new_live)

        if len(all_live) < TARGET_LIVE:
            logger.warning(f"Exhausted pool, found {len(all_live)}/{TARGET_LIVE} live")

        all_live.sort(key=lambda x: x[1])
        _scrubbed_proxies = [p for p, _ in all_live]
        _proxy_latencies = {p: lat for p, lat in all_live}

        if _scrubbed_proxies:
            with open(SCRUBBED_FILE, 'w') as f:
                for proxy in _scrubbed_proxies:
                    f.write(proxy + '\n')

        _scrub_stats['total_live'] = len(_scrubbed_proxies)
        _scrub_stats['dead_removed'] = verify_removed
        _scrub_stats['last_scrub'] = int(time.time())
        _scrub_stats['scrub_cycles'] += 1

        if all_live:
            latencies = [lat for _, lat in all_live if lat > 0]
            if latencies:
                _scrub_stats['avg_latency'] = round(sum(latencies) / len(latencies))
                fastest = min(all_live, key=lambda x: x[1] if x[1] > 0 else 99999)
                _scrub_stats['fastest_proxy'] = fastest[0]
            else:
                _scrub_stats['avg_latency'] = 0

        remaining = [p for p in raw if p not in already_live_set]
        all_proxies = list(_scrubbed_proxies) + remaining
        with open(PROXY_FILE, 'w') as f:
            for p in all_proxies:
                f.write(p + '\n')

        elapsed = time.time() - t_start
        logger.info(
            f"Scrub done in {elapsed:.1f}s: {len(_scrubbed_proxies)} LIVE "
            f"(target={TARGET_LIVE}, cycle #{_scrub_stats['scrub_cycles']})"
        )
        if _scrub_stats['avg_latency'] > 0:
            logger.info(f"Avg latency: {_scrub_stats['avg_latency']}ms | Fastest: {_scrub_stats.get('fastest_proxy', 'N/A')}")

        return _scrubbed_proxies


async def auto_scrub_loop(interval=300, send_stats_callback=None):
    while True:
        try:
            await full_scrape_and_scrub()

            if send_stats_callback and _scrub_stats['scrub_cycles'] > 0:
                try:
                    await send_stats_callback(_scrub_stats)
                except Exception as e:
                    logger.debug(f"Stats callback error: {e}")

        except Exception as e:
            logger.error(f"Auto-scrub error: {e}")
        await asyncio.sleep(interval)


async def proxy_pool_monitor(check_interval=30, refill_callback=None):
    while True:
        try:
            live_count = len(_scrubbed_proxies)
            if live_count < REFILL_THRESHOLD:
                logger.warning(f"Live proxies LOW: {live_count}/{TARGET_LIVE} (threshold={REFILL_THRESHOLD}) - triggering auto-refill...")
                await full_scrape_and_scrub()
                new_count = len(_scrubbed_proxies)
                logger.info(f"Auto-refill complete: {live_count} -> {new_count} live proxies")
                if refill_callback:
                    try:
                        await refill_callback(new_count)
                    except Exception as e:
                        logger.debug(f"Refill callback error: {e}")
        except Exception as e:
            logger.error(f"Proxy pool monitor error: {e}")
        await asyncio.sleep(check_interval)
