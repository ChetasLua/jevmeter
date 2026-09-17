"""API key handling. The key is looked up in this order and never written inside a project folder:

1. the TYPESAFE_API_KEY environment variable
2. a .env file in the current directory (TYPESAFE_API_KEY=...)
3. the user config file: ~/.config/jevmeter/config.json (created by `jevmeter setup`, permissions 600)
"""
import json
import os
import stat

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "jevmeter")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def _from_dotenv(path=".env"):
    if not os.path.exists(path):
        return None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("TYPESAFE_API_KEY") and "=" in line:
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            return v or None
    return None


def get_key():
    k = os.environ.get("TYPESAFE_API_KEY") or _from_dotenv()
    if k:
        return k.strip()
    if os.path.exists(CONFIG_FILE):
        try:
            return (json.load(open(CONFIG_FILE)).get("typesafe_api_key") or "").strip() or None
        except Exception:
            return None
    return None


def save_key(key):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = {}
    if os.path.exists(CONFIG_FILE):
        try:
            data = json.load(open(CONFIG_FILE))
        except Exception:
            data = {}
    data["typesafe_api_key"] = key.strip()
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return CONFIG_FILE


def mask(key):
    return (key[:10] + "…" + key[-4:]) if key and len(key) > 16 else "…"


def check_key(key):
    """One tiny request to confirm the key works. Returns (ok, message)."""
    import requests
    from .score import API
    try:
        r = requests.post(API, headers={"Authorization": f"Bearer {key}"}, timeout=30,
                          json={"model": "jev-latest", "state": "hello", "questions": {"q": {"type": "noul", "instructions": "Is `state` a greeting?"}}})
    except Exception as e:
        return False, f"could not reach api.typesafe.ai ({e.__class__.__name__}); check your internet connection"
    if r.status_code in (401, 403):
        return False, "TypeSafe rejected this key; create a new one at https://console.typesafe.ai/keys"
    if r.status_code >= 400:
        return False, f"TypeSafe API error {r.status_code}: {r.text[:160]}"
    return True, f"key works ({r.json().get('model', 'jev')})"
