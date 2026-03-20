import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ADS Browser local API.
LOCAL_API_BASE = "http://127.0.0.1:50325"
API_KEY = os.getenv("ADS_API_KEY", "0b164afcd0bf4a80370594b331f1aa0b0086cd551c0bdc24")
ADS_TIMEOUT_SECONDS = 90
ADS_HEADLESS = False
ADS_OPEN_TABS = 1
ADS_PROFILE_READY_TIMEOUT_SECONDS = 30
ADS_PROFILE_WARMUP_SECONDS = 0

# Calendly URLs.
CALENDLY_SIGNUP_URL = "https://calendly.com/signup"
CALENDLY_MEETING_TYPES_URL = "https://calendly.com/app/scheduling/meeting_types/user/me"
SIGNUP_LOAD_CHECK_TIMEOUT_SECONDS = 8
SIGNUP_LOAD_MAX_RELOADS = 4
SIGNUP_INITIAL_DELAY_SECONDS = 4
SIGNUP_WAIT_FOREVER_IF_NOT_READY = True
SIGNUP_POST_EMAIL_WAIT_SECONDS = 20
SIGNUP_PASSWORD_SWITCH_TIMEOUT_SECONDS = 35
KEEP_PROFILE_OPEN_AFTER_REGISTRATION = True

# AnyMessage email activation API.
# Docs reference: https://anymessage.shop/en/docs
ANYMESSAGE_API_BASE = "https://api.anymessage.shop"
ANYMESSAGE_TOKEN = "CmeCiBaS3fAgAXoGYTYS6l3x2k0Kowyc"
ANYMESSAGE_SITE = "calendly.com"
ANYMESSAGE_DOMAIN = "gmail"
ANYMESSAGE_POLL_SECONDS = 5
ANYMESSAGE_MAX_WAIT_SECONDS = 180

# 9proxy API. Rotation is used only on explicit errors.
NINEPROXY_ROTATE_ENABLED = False
NINEPROXY_ROTATE_URL = ""
NINEPROXY_ROTATE_METHOD = "GET"
NINEPROXY_TIMEOUT_SECONDS = 30
NINEPROXY_API_KEY = ""
NINEPROXY_PORT = ""

# Retry strategy for registration when captcha/proxy errors happen.
REGISTRATION_MAX_ATTEMPTS = 3
CAPTCHA_WAIT_SECONDS = 20
CAPTCHA_MODE = "manual"  # manual | auto_wait
CAPTCHA_MANUAL_TIMEOUT_SECONDS = 600

# Runtime speed multiplier for explicit UI pauses.
# 1.0 = original speed, 0.5 = ~2x faster, 0.25 = ~4x faster.
# 0.2 keeps actions fast but less race-prone on slow page states.
ACTION_SPEED_MULTIPLIER = 0.2

# Booking settings.
BOOKING_GUESTS_PER_EVENT = 10
BOOKING_NAME_PREFIX = "Alex"
SEND_BETWEEN_BOOKINGS_SECONDS = 2
SCHEDULE_CONFIRM_EXTRA_WAIT_SECONDS = 2

# Sender captcha recovery:
# recreate ADS profile with fixed SOCKS5 and resume sending.
SENDER_CAPTCHA_RECOVERY_ENABLED = True
SENDER_CAPTCHA_MAX_RECOVERIES = 5
SENDER_CAPTCHA_PROXY_TYPE = "socks5"
SENDER_CAPTCHA_PROXY_HOST = "127.0.0.1"
SENDER_CAPTCHA_PROXY_PORT = 60000
SENDER_CAPTCHA_PROXY_USER = ""
SENDER_CAPTCHA_PROXY_PASSWORD = ""
SENDER_CAPTCHA_PROFILE_NAME_PREFIX = "calendly-recovery"

# Files and directories.
EDIT_DIR_CANDIDATES = [BASE_DIR / "edit", BASE_DIR / "EDIT"]
EMAIL_DIR_CANDIDATES = [BASE_DIR / "email", BASE_DIR / "EMAIL"]
COOKIE_DIR_CANDIDATES = [BASE_DIR / "coockie", BASE_DIR / "cookie"]

SUBJECT_FILENAME = "subject.txt"
BODY_FILENAME = "body.txt"
EMAIL_FILENAME = "emails.txt"

PROFILE_DB_PATH = BASE_DIR / "profiles.db"
LOGS_DIR = BASE_DIR / "logs"
SENT_EMAILS_DIR = BASE_DIR / "sent-emails"
SENT_EMAILS_FILENAME = "sent_emails.txt"

# Legacy compatibility (old pCloud module).
DEFAULT_PCLOUD_URL = "https://my.pcloud.com/"
BATCH_SIZE = 20
BATCH_DELAY_SECONDS = 1.0
NAME_FILENAME = "NAME.txt"
TEXT_FILENAME = "text.txt"
