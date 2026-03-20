from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ADS Browser local API
LOCAL_API_BASE = "http://127.0.0.1:50325"
API_KEY = "0b164afcd0bf4a80370594b331f1aa0b0086cd551c0bdc24"
ADS_TIMEOUT_SECONDS = 60
ADS_HEADLESS = False
ADS_OPEN_TABS = 1

# Inflow automation defaults
DEFAULT_INFLOW_URL = "https://app.inflowinventory.com/purchase-orders"
INFLOW_SIGNUP_URL = "https://accounts.inflowinventory.com/signup"
SEND_DELAY_SECONDS = 0.2

# Auto re-registration and profile rotation
AUTO_REREGISTER_ON_LIMIT = True
AUTO_REREGISTER_TRY_LIMIT = 1
UK_PHONE_DIGITS = 11

# AnyMessage API
ANYMESSAGE_API_BASE = "https://api.anymessage.example"
ANYMESSAGE_API_TOKEN = ""
ANYMESSAGE_TIMEOUT_SECONDS = 60
ANYMESSAGE_SERVICE = "gmail"
ANYMESSAGE_PURCHASE_PATHS = (
    "/api/v1/mail/buy",
    "/api/v1/emails/buy",
    "/api/v1/order/create",
    "/buy",
)
ANYMESSAGE_MESSAGES_PATHS = (
    "/api/v1/mail/messages",
    "/api/v1/messages",
    "/api/v1/order/messages",
    "/messages",
)
ANYMESSAGE_POLL_INTERVAL_SECONDS = 6
ANYMESSAGE_CONFIRM_TIMEOUT_SECONDS = 240

# Files and directories
EDIT_DIR_CANDIDATES = [BASE_DIR / "edit", BASE_DIR / "EDIT"]
EMAIL_DIR_CANDIDATES = [BASE_DIR / "email", BASE_DIR / "EMAIL"]
SUBJECT_FILENAME = "SUBJECT.txt"
MESSAGE_FILENAME = "MESSAGE.txt"
EMAIL_FILENAMES = ("emails.txt", "email.txt")
SENT_EMAILS_DIR = BASE_DIR / "sent-emails"
SENT_EMAILS_TXT = "sent_emails.txt"
SENT_EMAILS_DB = "sent_emails.db"

PROFILE_DB_PATH = BASE_DIR / "profiles.db"
LOGS_DIR = BASE_DIR / "logs"
