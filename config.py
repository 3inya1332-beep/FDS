from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ADS Browser local API
LOCAL_API_BASE = "http://127.0.0.1:50325"
API_KEY = "PASTE_YOUR_ADS_API_KEY"
ADS_TIMEOUT_SECONDS = 60
ADS_HEADLESS = False
ADS_OPEN_TABS = 1

# Inflow automation defaults
DEFAULT_INFLOW_URL = "https://app.inflowinventory.com/purchase-orders"
SEND_DELAY_SECONDS = 1.0

# Files and directories
EDIT_DIR_CANDIDATES = [BASE_DIR / "edit", BASE_DIR / "EDIT"]
EMAIL_DIR_CANDIDATES = [BASE_DIR / "email", BASE_DIR / "EMAIL"]
SUBJECT_FILENAME = "SUBJECT.txt"
MESSAGE_FILENAME = "MESSAGE.txt"
EMAIL_FILENAMES = ("emails.txt", "email.txt")

PROFILE_DB_PATH = BASE_DIR / "profiles.db"
LOGS_DIR = BASE_DIR / "logs"
