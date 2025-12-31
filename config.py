# Contoh config.py — gunakan env vars pada production
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_TOKEN_DI_SINI")
CHANNEL_MENFESS = int(os.getenv("CHANNEL_MENFESS", "-100xxxxxxxx"))
GROUP_PUBLIK = int(os.getenv("GROUP_PUBLIK", "-100xxxxxxxx"))
CHANNEL_LOG = int(os.getenv("CHANNEL_LOG", "-100xxxxxxxx"))

# ADMIN IDS — contoh: "123456789,987654321"
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x]

# Maximum upload/download file size in MB
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))
