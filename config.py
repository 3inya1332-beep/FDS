from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

# ADS Browser / AdsPower Local API
ADS_API_BASE = "http://127.0.0.1:50325"
ADS_API_KEY = "PUT_YOUR_ADS_API_KEY_HERE"
ADS_API_TIMEOUT = 60
ADS_HEADLESS = 0
ADS_MINIMIZED_LAUNCH_ARGS = [
    "--start-minimized",
    "--window-position=-2400,-2400",
    "--window-size=1280,900",
]

# pCloud
DEFAULT_START_URL = "https://my.pcloud.com/"
DEFAULT_LOCATION_ID = 2
PCLOUD_API_HOSTS = {
    1: "api.pcloud.com",
    2: "eapi.pcloud.com",
}
DEFAULT_PARENT_FOLDER_ID = 0
DEFAULT_SHARE_PERMISSION = 3  # edit = create + modify
DEFAULT_BATCH_SIZE = 20
MAX_WORKERS = 5
REQUEST_TIMEOUT = 60
REQUEST_DELAY_SECONDS = 0.0
OPEN_START_URL_AFTER_CONNECT = True
RETRY_AUTH_AFTER_MANUAL_LOGIN = True
CLOSE_PROFILE_AFTER_RUN = False

# Local project files
PROFILE_DB_PATH = BASE_DIR / "profiles.sqlite3"
FOLDER_NAME_FILE = BASE_DIR / "edit" / "NAME.txt"
MESSAGE_FILE = BASE_DIR / "edit" / "text.txt"
EMAILS_FILE = BASE_DIR / "email" / "emails.txt"
LOG_FILE = BASE_DIR / "logs" / "app.log"
