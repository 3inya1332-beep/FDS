from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ADS Browser local API
# For AdsPower Local API, "http://local.adspower.net:50325" is the canonical host.
# If your setup uses 127.0.0.1, you can keep "http://127.0.0.1:50325".
LOCAL_API_BASE = "http://local.adspower.net:50325"
ADS_FALLBACK_BASE_URLS = [
    "http://127.0.0.1:50325",
    "http://localhost:50325",
    "http://local.adspower.net:50325",
]
API_KEY = "PASTE_YOUR_ADS_API_KEY"
ADS_TIMEOUT_SECONDS = 60
ADS_HEADLESS = False
ADS_OPEN_TABS = 1
ADS_REQUEST_PAUSE_SECONDS = 0.8
ADS_MAX_RETRIES_PER_URL = 2

# pCloud automation defaults
DEFAULT_PCLOUD_URL = "https://my.pcloud.com/"
BATCH_SIZE = 20
BATCH_DELAY_SECONDS = 0.6

# 9Proxy rotation (same running script, no manual restart)
# If you use a single 9Proxy API URL, keep one item in the list.
# If you have multiple URLs/keys, put all of them in this list (round-robin).
PROXY_ROTATION_ENABLED = True
PROXY_ROTATION_URLS = [
    "http://127.0.0.1:10101/api/proxy?t=2&num=1&country=GB",
]
PROXY_ROTATION_TIMEOUT_SECONDS = 12
PROXY_ROTATION_WAIT_SECONDS = 0.8

# Files and directories
EDIT_DIR_CANDIDATES = [BASE_DIR / "edit", BASE_DIR / "EDIT"]
EMAIL_DIR_CANDIDATES = [BASE_DIR / "email", BASE_DIR / "EMAIL"]
NAME_FILENAME = "NAME.txt"
TEXT_FILENAME = "text.txt"
EMAIL_FILENAME = "emails.txt"

PROFILE_DB_PATH = BASE_DIR / "profiles.db"
LOGS_DIR = BASE_DIR / "logs"
EMAIL_HISTORY_DIR = BASE_DIR / "email_history"
SENT_EMAILS_FILE = EMAIL_HISTORY_DIR / "sent_emails.txt"
UNSENT_EMAILS_FILE = EMAIL_HISTORY_DIR / "unsent_emails.txt"
INBOX_TEST_COUNTER_FILE = EMAIL_HISTORY_DIR / "inbox_test_counter.txt"
PROXY_SETTINGS_FILE = EMAIL_HISTORY_DIR / "proxy_settings.json"
