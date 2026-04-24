import os
import json
import random
import logging
import time
import string
import copy
from faker import Faker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class _PollConflictFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "TerminatedByOtherGetUpdates" in msg:
            return False
        if "Terminated by other getupdates" in msg:
            return False
        if "Cause exception while getting updates" in msg:
            return False
        return True

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
for _ln in ['aiogram.dispatcher.dispatcher', 'aiogram.dispatcher', 'aiogram', '']:
    logging.getLogger(_ln).addFilter(_PollConflictFilter())
logger = logging.getLogger(__name__)

fake = Faker()

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip() or 'PASTE_YOUR_BOT_TOKEN_HERE'
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip() or 'PASTE_YOUR_CHAT_ID_HERE'
ADMIN_CODE = os.environ.get('ADMIN_CODE', '').strip() or 'PASTE_YOUR_ADMIN_ID_HERE'
TELEGRAM_ADMIN = os.environ.get('TELEGRAM_ADMIN', '').strip() or 'PASTE_YOUR_ADMIN_ID_HERE'
STRIPE_PUB_KEY = os.environ.get('STRIPE_PUB_KEY', '').strip()

_DEFAULT_GATE = {
    "site_url": "https://pipelineforchangefoundation.com",
    "donate_path": "/donate/",
    "campaign_id": "",
    "pub_key": "",
    "stripe_account": "",
    "donation_amount": "1.00",
    "random_amount": False,
    "random_amount_min": "1.00",
    "random_amount_max": "5.00",
    "hybrid_mode": False,
}

_DEFAULT_BRAINTREE_GATE = {
    "site_url": "https://huckberry.com",
    "add_to_cart_path": "/orders/populate",
    "checkout_path": "/checkout/onepage",
    "product_payload": "",
    "payment_method_id": "3",
    "hybrid_mode": False,
}

GATE_SETTINGS = {
    "stripe": copy.deepcopy(_DEFAULT_GATE),
    "braintree": copy.deepcopy(_DEFAULT_BRAINTREE_GATE),
}

MAX_CONFIGS = 5

_gate_configs = {
    1: {
        "name": "Stripe Gate",
        "gate_type": "stripe",
        "settings": copy.deepcopy(_DEFAULT_GATE),
        "enabled": True,
        "stats": {"checked": 0, "live": 0, "dead": 0, "errors": 0, "charged": 0},
    },
    2: {
        "name": "Braintree Gate",
        "gate_type": "braintree",
        "settings": copy.deepcopy(_DEFAULT_BRAINTREE_GATE),
        "enabled": True,
        "stats": {"checked": 0, "live": 0, "dead": 0, "errors": 0, "charged": 0},
    }
}
_active_config_id = 1
_parallel_enabled = True
_next_config_id = 3


def get_config(config_id):
    return _gate_configs.get(config_id)


def get_all_configs():
    return copy.deepcopy(_gate_configs)


def get_active_config_id():
    return _active_config_id


def set_active_config(config_id):
    global _active_config_id
    if config_id in _gate_configs:
        _active_config_id = config_id
        gt = _gate_configs[config_id].get("gate_type", "stripe")
        GATE_SETTINGS[gt] = _gate_configs[config_id]["settings"]
        GATE_ENABLED[gt] = _gate_configs[config_id]["enabled"]
        return True
    return False


def create_config(name="", from_url="", gate_type="stripe"):
    global _next_config_id
    if len(_gate_configs) >= MAX_CONFIGS:
        return None, "Max 5 configs reached"
    cid = _next_config_id
    _next_config_id += 1
    default_settings = copy.deepcopy(_DEFAULT_BRAINTREE_GATE) if gate_type == "braintree" else copy.deepcopy(_DEFAULT_GATE)
    _gate_configs[cid] = {
        "name": name or f"Config #{cid}",
        "gate_type": gate_type,
        "settings": default_settings,
        "enabled": False,
        "stats": {"checked": 0, "live": 0, "dead": 0, "errors": 0, "charged": 0},
    }
    return cid, "OK"


def duplicate_config(source_id):
    global _next_config_id
    if len(_gate_configs) >= MAX_CONFIGS:
        return None, "Max 5 configs reached"
    src = _gate_configs.get(source_id)
    if not src:
        return None, "Config not found"
    cid = _next_config_id
    _next_config_id += 1
    _gate_configs[cid] = {
        "name": f"{src['name']} (copy)",
        "gate_type": src.get("gate_type", "stripe"),
        "settings": copy.deepcopy(src["settings"]),
        "enabled": False,
        "stats": {"checked": 0, "live": 0, "dead": 0, "errors": 0, "charged": 0},
    }
    return cid, "OK"


def delete_config(config_id):
    global _active_config_id
    if config_id not in _gate_configs:
        return False, "Config not found"
    if len(_gate_configs) <= 1:
        return False, "Cannot delete last config"
    del _gate_configs[config_id]
    if _active_config_id == config_id:
        _active_config_id = list(_gate_configs.keys())[0]
        gt = _gate_configs[_active_config_id].get("gate_type", "stripe")
        GATE_SETTINGS[gt] = _gate_configs[_active_config_id]["settings"]
        GATE_ENABLED[gt] = _gate_configs[_active_config_id]["enabled"]
    return True, "OK"


def enable_config(config_id):
    if config_id in _gate_configs:
        _gate_configs[config_id]["enabled"] = True
        if config_id == _active_config_id:
            gt = _gate_configs[config_id].get("gate_type", "stripe")
            GATE_ENABLED[gt] = True
        return True
    return False


def disable_config(config_id):
    if config_id in _gate_configs:
        _gate_configs[config_id]["enabled"] = False
        if config_id == _active_config_id:
            gt = _gate_configs[config_id].get("gate_type", "stripe")
            GATE_ENABLED[gt] = False
        return True
    return False


def set_config_setting(config_id, key, value):
    if config_id in _gate_configs:
        _gate_configs[config_id]["settings"][key] = value
        if config_id == _active_config_id:
            gt = _gate_configs[config_id].get("gate_type", "stripe")
            if gt not in GATE_SETTINGS:
                GATE_SETTINGS[gt] = {}
            GATE_SETTINGS[gt][key] = value
        return True
    return False


def set_config_name(config_id, name):
    if config_id in _gate_configs:
        _gate_configs[config_id]["name"] = name
        return True
    return False


def get_config_stats(config_id):
    if config_id in _gate_configs:
        return _gate_configs[config_id]["stats"].copy()
    return {}


def update_config_stats(config_id, key, increment=1):
    if config_id in _gate_configs and key in _gate_configs[config_id]["stats"]:
        _gate_configs[config_id]["stats"][key] += increment


def get_enabled_configs():
    return {cid: cfg for cid, cfg in _gate_configs.items() if cfg["enabled"]}


def is_parallel_enabled():
    return _parallel_enabled


def set_parallel_enabled(enabled):
    global _parallel_enabled
    _parallel_enabled = enabled


def config_count():
    return len(_gate_configs)


_redeem_keys = {}

def generate_redeem_key(days):
    if days < 1 or days > 30:
        return None, "Days must be 1-30"
    key = f"H@0-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"
    expiry = int(time.time()) + (days * 86400)
    _redeem_keys[key] = {
        "days": days,
        "created": int(time.time()),
        "expiry": expiry,
        "used_by": None,
        "used_at": None,
    }
    return key, "OK"


def redeem_key(key, user_id):
    if key not in _redeem_keys:
        return False, "Invalid key"
    kd = _redeem_keys[key]
    if kd["used_by"] is not None:
        return False, "Key already used"
    if int(time.time()) > kd["expiry"]:
        return False, "Key expired"
    kd["used_by"] = user_id
    kd["used_at"] = int(time.time())
    return True, kd["days"]


def get_all_redeem_keys():
    return copy.deepcopy(_redeem_keys)


def revoke_redeem_key(key):
    if key in _redeem_keys:
        del _redeem_keys[key]
        return True
    return False


def is_user_redeemed(user_id):
    now = int(time.time())
    for kd in _redeem_keys.values():
        if kd["used_by"] == user_id:
            if now <= kd["expiry"]:
                remaining = kd["expiry"] - now
                return True, remaining
    return False, 0


def cleanup_expired_keys():
    now = int(time.time())
    expired = [k for k, v in _redeem_keys.items() if now > v["expiry"]]
    for k in expired:
        del _redeem_keys[k]
    return len(expired)


_ADMINS_FILE = os.path.join(BASE_DIR, 'admins.json')
_extra_admins = {}


def _load_admins():
    global _extra_admins
    try:
        if os.path.exists(_ADMINS_FILE):
            with open(_ADMINS_FILE, 'r') as f:
                _extra_admins = json.load(f)
    except Exception:
        _extra_admins = {}


def _save_admins():
    try:
        with open(_ADMINS_FILE, 'w') as f:
            json.dump(_extra_admins, f)
    except Exception:
        pass


_load_admins()


def add_admin(identifier, label=""):
    uid = str(identifier).strip().lstrip('@')
    if uid in _extra_admins:
        return False, "Already admin"
    _extra_admins[uid] = {"label": label, "added": int(time.time())}
    _save_admins()
    return True, "Added"


def remove_admin(identifier):
    uid = str(identifier).strip().lstrip('@')
    if uid in _extra_admins:
        del _extra_admins[uid]
        _save_admins()
        return True
    return False


def get_all_admins():
    return copy.deepcopy(_extra_admins)


def is_extra_admin(user_id, username=None):
    if str(user_id) in _extra_admins:
        return True
    if username and username.lstrip('@') in _extra_admins:
        return True
    return False


_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
]


def get_random_ua():
    return random.choice(_USER_AGENTS)


def get_gate_setting(gate_name, key, default=""):
    return GATE_SETTINGS.get(gate_name, {}).get(key, default)


def set_gate_setting(gate_name, key, value):
    if gate_name not in GATE_SETTINGS:
        GATE_SETTINGS[gate_name] = {}
    GATE_SETTINGS[gate_name][key] = value
    if _active_config_id in _gate_configs:
        _gate_configs[_active_config_id]["settings"][key] = value


def get_all_gate_settings(gate_name):
    return GATE_SETTINGS.get(gate_name, {}).copy()


GATE_ENABLED = {"stripe": True, "braintree": True}


def get_config_gate_type(config_id):
    if config_id in _gate_configs:
        return _gate_configs[config_id].get("gate_type", "stripe")
    return "stripe"


def set_config_gate_type(config_id, gate_type):
    if config_id in _gate_configs and gate_type in ("stripe", "braintree"):
        old_type = _gate_configs[config_id].get("gate_type", "stripe")
        _gate_configs[config_id]["gate_type"] = gate_type
        if old_type != gate_type:
            default_settings = copy.deepcopy(_DEFAULT_BRAINTREE_GATE) if gate_type == "braintree" else copy.deepcopy(_DEFAULT_GATE)
            _gate_configs[config_id]["settings"] = default_settings
        return True
    return False


def is_gate_enabled(gate_name):
    return GATE_ENABLED.get(gate_name, False)


def set_gate_enabled(gate_name, enabled):
    GATE_ENABLED[gate_name] = enabled
    if _active_config_id in _gate_configs:
        _gate_configs[_active_config_id]["enabled"] = enabled


NOTIFY_SETTINGS = {
    "live": True,
    "decline": False,
    "errors": False,
    "proxy_file": False,
}

_custom_chat_id = None

def get_notify(key):
    return NOTIFY_SETTINGS.get(key, False)

def set_notify(key, enabled):
    NOTIFY_SETTINGS[key] = enabled

def get_all_notify():
    return NOTIFY_SETTINGS.copy()

def set_custom_chat_id(chat_id):
    global _custom_chat_id
    _custom_chat_id = chat_id

def get_custom_chat_id():
    return _custom_chat_id


_proxy_pool = []
_proxy_blacklist = set()
_proxy_stats = {
    'total_loaded': 0,
    'blacklisted': 0,
    'rotations': 0,
}
_proxy_enabled = True
_custom_proxies = []


def load_proxies():
    global _proxy_pool
    live_file = os.path.join(BASE_DIR, "proxies_live.txt")
    raw_file = os.path.join(BASE_DIR, "proxies.txt")

    chosen_file = live_file if os.path.exists(live_file) and os.path.getsize(live_file) > 0 else raw_file

    try:
        if os.path.exists(chosen_file):
            with open(chosen_file, 'r') as f:
                all_proxies = [line.strip() for line in f if line.strip()]
            _proxy_pool = [p for p in all_proxies if p not in _proxy_blacklist]
            _proxy_stats['total_loaded'] = len(_proxy_pool)
            source = "scrubbed" if chosen_file == live_file else "raw"
            logger.info(f"Loaded {len(_proxy_pool)} {source} proxies (blacklisted: {len(_proxy_blacklist)})")
    except Exception as e:
        logger.error(f"Error loading proxies: {e}")
        _proxy_pool = []


def blacklist_proxy(proxy):
    clean = proxy.replace('http://', '').replace('https://', '')
    _proxy_blacklist.add(clean)
    _proxy_stats['blacklisted'] = len(_proxy_blacklist)
    if clean in _proxy_pool:
        _proxy_pool.remove(clean)


def clear_blacklist():
    global _proxy_blacklist
    _proxy_blacklist = set()
    _proxy_stats['blacklisted'] = 0


def get_proxy_dict():
    global _proxy_pool
    if not _proxy_enabled:
        return None
    if _custom_proxies:
        proxy = random.choice(_custom_proxies)
        if not proxy.startswith('http'):
            proxy = f"http://{proxy}"
        _proxy_stats['rotations'] += 1
        return {"http": proxy, "https": proxy}
    if not _proxy_pool:
        load_proxies()
    if not _proxy_pool:
        return None

    proxy = random.choice(_proxy_pool)
    if not proxy.startswith('http'):
        proxy = f"http://{proxy}"
    _proxy_stats['rotations'] += 1
    return {"http": proxy, "https": proxy}


def get_proxy_stats():
    return _proxy_stats.copy()


def get_pool_size():
    if _custom_proxies:
        return len(_custom_proxies)
    return len(_proxy_pool)


def is_proxy_enabled():
    return _proxy_enabled


def set_proxy_enabled(enabled):
    global _proxy_enabled
    _proxy_enabled = enabled


def _normalize_proxy(proxy):
    clean = proxy.strip()
    parts = clean.split(':')
    if len(parts) == 4:
        ip, port, user, passwd = parts
        return f"{user}:{passwd}@{ip}:{port}"
    return clean


def add_custom_proxy(proxy):
    global _custom_proxies
    normalized = _normalize_proxy(proxy)
    if normalized and normalized not in _custom_proxies:
        _custom_proxies.append(normalized)
        return True
    return False


def remove_custom_proxy(proxy):
    global _custom_proxies
    normalized = _normalize_proxy(proxy)
    if normalized in _custom_proxies:
        _custom_proxies.remove(normalized)
        return True
    clean = proxy.strip()
    if clean in _custom_proxies:
        _custom_proxies.remove(clean)
        return True
    return False


def get_custom_proxies():
    return _custom_proxies.copy()


def clear_custom_proxies():
    global _custom_proxies
    _custom_proxies = []


def has_custom_proxies():
    return len(_custom_proxies) > 0


_user_data_dir = os.path.join(BASE_DIR, "user_data")
os.makedirs(_user_data_dir, exist_ok=True)

_user_limits_file = os.path.join(_user_data_dir, "user_limits.json")
_user_limits_defaults = {
    "max_cards_per_user": 0,
    "max_concurrent_users": 10,
}

def _load_user_limits():
    try:
        if os.path.exists(_user_limits_file):
            with open(_user_limits_file, 'r') as f:
                saved = json.load(f)
                merged = _user_limits_defaults.copy()
                merged.update(saved)
                return merged
    except Exception:
        pass
    return _user_limits_defaults.copy()

def _save_user_limits():
    try:
        with open(_user_limits_file, 'w') as f:
            json.dump(_user_limits, f)
    except Exception:
        pass

_user_limits = _load_user_limits()


def get_user_limit(key):
    return _user_limits.get(key, 0)


def set_user_limit(key, value):
    if key in _user_limits:
        _user_limits[key] = int(value)
        _save_user_limits()
        return True
    return False


def get_all_user_limits():
    return _user_limits.copy()


def _user_file(user_id, file_type):
    return os.path.join(_user_data_dir, f"{user_id}_{file_type}.txt")


def track_user_card(user_id, card_str, result_type):
    uid = str(user_id)
    if result_type == "live":
        fpath = _user_file(uid, "lives")
    elif result_type == "error":
        fpath = _user_file(uid, "errors")
    else:
        return
    try:
        with open(fpath, 'a') as f:
            f.write(card_str.strip() + '\n')
    except Exception:
        pass


def get_user_cards(user_id, file_type="lives"):
    fpath = _user_file(str(user_id), file_type)
    if not os.path.exists(fpath):
        return []
    try:
        with open(fpath, 'r') as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


def get_user_card_file(user_id, file_type="lives"):
    fpath = _user_file(str(user_id), file_type)
    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fpath
    return None


def clear_user_cards(user_id, file_type="lives"):
    fpath = _user_file(str(user_id), file_type)
    try:
        if os.path.exists(fpath):
            os.remove(fpath)
            return True
    except Exception:
        pass
    return False


def get_user_check_count(user_id):
    fpath = _user_file(str(user_id), "check_count")
    try:
        if os.path.exists(fpath):
            with open(fpath, 'r') as f:
                data = json.load(f)
                if data.get("date") == time.strftime("%Y-%m-%d"):
                    return data.get("count", 0)
    except Exception:
        pass
    return 0


def increment_user_check_count(user_id):
    fpath = _user_file(str(user_id), "check_count")
    today = time.strftime("%Y-%m-%d")
    count = 0
    try:
        if os.path.exists(fpath):
            with open(fpath, 'r') as f:
                data = json.load(f)
                if data.get("date") == today:
                    count = data.get("count", 0)
    except Exception:
        pass
    count += 1
    try:
        with open(fpath, 'w') as f:
            json.dump({"date": today, "count": count}, f)
    except Exception:
        pass
    return count


def check_user_card_limit(user_id):
    limit = _user_limits["max_cards_per_user"]
    if limit <= 0:
        return True, 0
    current = get_user_check_count(user_id)
    if current >= limit:
        return False, limit
    return True, limit - current


def export_config_data(config_id):
    cfg = _gate_configs.get(config_id)
    if not cfg:
        return None
    return {
        "h0_config": True,
        "version": "6.0",
        "name": cfg["name"],
        "gate_type": cfg.get("gate_type", "stripe"),
        "enabled": cfg["enabled"],
        "settings": copy.deepcopy(cfg["settings"]),
    }


def import_config_data(data, target_config_id=None):
    if not isinstance(data, dict) or not data.get("h0_config"):
        return False, "Invalid config file format"

    gate_type = data.get("gate_type", "stripe")
    if gate_type not in ("stripe", "braintree"):
        return False, f"Unknown gate type: {gate_type}"

    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        return False, "Invalid settings in config file"

    name = data.get("name", "Imported Config")
    enabled = data.get("enabled", False)

    if target_config_id is not None:
        cfg = _gate_configs.get(target_config_id)
        if not cfg:
            return False, f"Config #{target_config_id} not found"
        cfg["name"] = name
        cfg["gate_type"] = gate_type
        cfg["settings"] = copy.deepcopy(settings)
        cfg["enabled"] = enabled
        if target_config_id == _active_config_id:
            GATE_SETTINGS[gate_type] = cfg["settings"]
            GATE_ENABLED[gate_type] = cfg["enabled"]
        return True, f"Applied to Config #{target_config_id}"
    else:
        cid, msg = create_config(name=name, gate_type=gate_type)
        if cid is None:
            return False, msg
        _gate_configs[cid]["settings"] = copy.deepcopy(settings)
        _gate_configs[cid]["enabled"] = enabled
        return True, f"Created Config #{cid} ({name})"


load_proxies()
