from logging import root
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from slim_bot/ or parent tele_bot/
_here = Path(__file__).resolve().parent
load_dotenv(_here / ".env")
load_dotenv(_here.parent / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7887005250:AAGxQec_EDAFD7hGQP3PVnd9W4fj-kdFIS4")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-3.5-turbo"  # Cheaper model for better pricing control
MONGO_URI = os.getenv("MONGO_URI")

# gpt-3.5-turbo approximate API pricing (USD per 1M tokens)
PRICE_INPUT_PER_1M = 0.50
PRICE_OUTPUT_PER_1M = 1.50

# Comma-separated Telegram user IDs allowed to use /admin
ADMIN_TELEGRAM_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
    if x.strip().isdigit()
}

# Rate limiting
DAILY_MESSAGE_LIMIT = 40  # Maximum messages per user per day (free tier)


# /root/english_bot/elena_english_bot
# sudo nano /etc/systemd/system/english_bot.service
# sudo systemctl enable english_bot
# [Unit]
# Description=Telegram Bot
# After=network.target

# [Service]
# User=root
# Group=root
# WorkingDirectory=/root/english_bot/elena_english_bot

# ExecStart=/root/english_bot/elena_english_bot/.venv/bin/python /root/english_bot/elena_english_bot/friend_bot.py

# Restart=always
# RestartSec=5

# Environment=PYTHONUNBUFFERED=1

# [Install]
# WantedBy=multi-user.target