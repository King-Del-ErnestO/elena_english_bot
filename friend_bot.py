# friend_bot.py
# A slim, conversational English-practice bot.
# - No roles. Just a friendly "human-like" chat partner.
# - Gives gentle corrections after every 2 user messages.
# - Keeps the tone supportive and natural; asks short follow-ups.

import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from collections import defaultdict, deque
from typing import List, Dict, Deque, Tuple, Optional, Set
from config import TELEGRAM_TOKEN, OPENAI_API_KEY, OPENAI_MODEL, DAILY_MESSAGE_LIMIT, ADMIN_TELEGRAM_IDS
import sqlite3
import threading
import time
from datetime import datetime, timedelta
import os
import json
import re

# OpenAI SDK v1
try:
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    client = None  # Allows the file to import even if SDK isn't installed yet

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ---- Database setup ----
DB_PATH = "friend_bot.db"

def init_db():
    """Initialize the SQLite database and create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_reminder_at TIMESTAMP,
            last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_word_of_day_at TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS allowed_emails (
            email TEXT PRIMARY KEY COLLATE NOCASE,
            added_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS word_of_the_day (
            date TEXT PRIMARY KEY,
            word TEXT NOT NULL,
            meaning_ru TEXT NOT NULL,
            pronunciation TEXT NOT NULL,
            example_sentence TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_message_count (
            chat_id INTEGER,
            date TEXT,
            message_count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, date)
        )
    """)
    
    # Migrate existing databases: add missing columns if they don't exist
    for column_sql in (
        "ALTER TABLE users ADD COLUMN last_word_of_day_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN email TEXT",
    ):
        try:
            cursor.execute(column_sql)
        except sqlite3.OperationalError:
            pass  # Column already exists, ignore
    
    conn.commit()
    conn.close()

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()

def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(normalize_email(email)))

def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_TELEGRAM_IDS

def get_user(chat_id: int) -> Optional[tuple]:
    """Return user row or None: (chat_id, username, first_name, email, ...)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT chat_id, username, first_name, email, last_reminder_at, last_active_at, last_word_of_day_at FROM users WHERE chat_id = ?",
        (chat_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return row

def is_email_allowed(email: str) -> bool:
    """Check if email is on the admin allowlist."""
    email = normalize_email(email)
    if not email:
        return False
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM allowed_emails WHERE email = ? COLLATE NOCASE", (email,))
    found = cursor.fetchone() is not None
    conn.close()
    return found

def user_has_access(chat_id: int) -> bool:
    """True if user is registered with an allowlisted email, or is an admin."""
    if is_admin(chat_id):
        return True
    user = get_user(chat_id)
    if not user:
        return False
    email = user[3]
    return bool(email) and is_email_allowed(email)

def add_allowed_email(email: str, added_by: int = None) -> bool:
    """Add email to allowlist. Returns True if newly added, False if already present."""
    email = normalize_email(email)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO allowed_emails (email, added_by) VALUES (?, ?)",
            (email, added_by),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def remove_allowed_email(email: str) -> bool:
    """Remove email from allowlist. Returns True if removed."""
    email = normalize_email(email)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM allowed_emails WHERE email = ? COLLATE NOCASE", (email,))
    removed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return removed

def list_allowed_emails() -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT email FROM allowed_emails ORDER BY email COLLATE NOCASE")
    emails = [row[0] for row in cursor.fetchall()]
    conn.close()
    return emails

def save_user(
    chat_id: int,
    username: str = None,
    first_name: str = None,
    email: str = None,
    update_reminder: bool = True,
):
    """Save or update a user in the database.
    
    Args:
        chat_id: User's chat ID
        username: Optional username
        first_name: Optional first name
        email: Optional email (normalized when provided)
        update_reminder: If True, also update last_reminder_at to prevent immediate reminders
    """
    if email is not None:
        email = normalize_email(email)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if user exists
    cursor.execute("SELECT chat_id, email FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    exists = row is not None
    
    if exists:
        # Keep existing email unless a new one is provided
        email_to_set = email if email is not None else row[1]
        if update_reminder:
            cursor.execute("""
                UPDATE users 
                SET username = ?, first_name = ?, email = ?, last_active_at = CURRENT_TIMESTAMP,
                    last_reminder_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
            """, (username, first_name, email_to_set, chat_id))
        else:
            cursor.execute("""
                UPDATE users 
                SET username = ?, first_name = ?, email = ?, last_active_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
            """, (username, first_name, email_to_set, chat_id))
    else:
        # Insert new user - set last_reminder_at to current time so they don't get immediate reminder
        cursor.execute("""
            INSERT INTO users (chat_id, username, first_name, email, last_active_at, last_reminder_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (chat_id, username, first_name, email))
    
    conn.commit()
    conn.close()

def get_all_users():
    """Get all users from the database who have access (allowlisted email)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.chat_id, u.username, u.first_name, u.last_reminder_at, u.last_active_at, u.last_word_of_day_at
        FROM users u
        INNER JOIN allowed_emails a ON lower(u.email) = lower(a.email)
        WHERE u.email IS NOT NULL AND u.email != ''
    """)
    users = cursor.fetchall()
    conn.close()
    return users

def update_last_reminder(chat_id: int):
    """Update the last reminder timestamp for a user."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users SET last_reminder_at = CURRENT_TIMESTAMP WHERE chat_id = ?
    """, (chat_id,))
    conn.commit()
    conn.close()

def get_used_words():
    """Get all words that have been used as word of the day."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT word FROM word_of_the_day ORDER BY date DESC")
    words = [row[0].lower() for row in cursor.fetchall()]
    conn.close()
    return words

def get_today_word():
    """Get today's word of the day, generating it if it doesn't exist."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if word for today exists
    cursor.execute("SELECT word, meaning_ru, pronunciation, example_sentence FROM word_of_the_day WHERE date = ?", (today,))
    result = cursor.fetchone()
    
    if result:
        conn.close()
        return {
            "word": result[0],
            "meaning_ru": result[1],
            "pronunciation": result[2],
            "example_sentence": result[3]
        }
    
    # Get all previously used words to avoid duplicates
    used_words = get_used_words()
    excluded_words_text = ", ".join(used_words) if used_words else "none"
    
    # Generate new word for today
    if client is None:
        # Fallback word if OpenAI is not available
        # Try to pick a word that hasn't been used
        fallback_words = [
            {"word": "hello", "meaning_ru": "привет, здравствуй", "pronunciation": "/həˈloʊ/", "example_sentence": "Hello! How are you today?"},
            {"word": "practice", "meaning_ru": "практика, практиковать", "pronunciation": "/ˈpræktɪs/", "example_sentence": "I practice English every day."},
            {"word": "friend", "meaning_ru": "друг, подруга", "pronunciation": "/frend/", "example_sentence": "She is my best friend."},
            {"word": "learn", "meaning_ru": "учить, изучать", "pronunciation": "/lɜːrn/", "example_sentence": "I want to learn English."},
            {"word": "help", "meaning_ru": "помощь, помогать", "pronunciation": "/help/", "example_sentence": "Can you help me, please?"}
        ]
        word_data = None
        for fallback in fallback_words:
            if fallback["word"].lower() not in used_words:
                word_data = fallback
                break
        if word_data is None:
            word_data = fallback_words[0]  # Use first one if all are used
    else:
        max_retries = 3
        word_data = None
        
        for attempt in range(max_retries):
            try:
                user_prompt = f"Generate a word of the day for today."
                if excluded_words_text and excluded_words_text != "none":
                    user_prompt += f"\n\nEXCLUDED WORDS (do NOT use these): {excluded_words_text}"
                
                resp = client.chat.completions.create(
                    model=OPENAI_MODEL,
                    temperature=0.7,
                    max_tokens=200,
                    messages=[
                        {"role": "system", "content": WORD_OF_DAY_PROMPT},
                        {"role": "user", "content": user_prompt}
                    ],
                    response_format={"type": "json_object"}
                )
                candidate_word = json.loads(resp.choices[0].message.content.strip())
                
                # Check if the word was already used
                if candidate_word["word"].lower() not in used_words:
                    word_data = candidate_word
                    break
                else:
                    print(f"Word '{candidate_word['word']}' was already used, retrying... (attempt {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        # Update excluded words for next attempt
                        used_words.append(candidate_word["word"].lower())
                        excluded_words_text = ", ".join(used_words)
                    
            except Exception as e:
                print(f"Error generating word of the day: {e}")
                if attempt == max_retries - 1:
                    # Final fallback
                    fallback_words = [
                        {"word": "hello", "meaning_ru": "привет, здравствуй", "pronunciation": "/həˈloʊ/", "example_sentence": "Hello! How are you today?"},
                        {"word": "practice", "meaning_ru": "практика, практиковать", "pronunciation": "/ˈpræktɪs/", "example_sentence": "I practice English every day."},
                        {"word": "friend", "meaning_ru": "друг, подруга", "pronunciation": "/frend/", "example_sentence": "She is my best friend."}
                    ]
                    for fallback in fallback_words:
                        if fallback["word"].lower() not in used_words:
                            word_data = fallback
                            break
                    if word_data is None:
                        word_data = fallback_words[0]
        
        if word_data is None:
            # Ultimate fallback
            word_data = {
                "word": "practice",
                "meaning_ru": "практика, практиковать",
                "pronunciation": "/ˈpræktɪs/",
                "example_sentence": "I practice English every day."
            }
    
    # Double-check for duplicates before saving
    if word_data["word"].lower() in used_words:
        print(f"WARNING: Generated word '{word_data['word']}' was already used! This should not happen.")
        # Try to find an alternative
        alternative_words = ["wonderful", "amazing", "interesting", "important", "different", "together", "always", "sometimes", "usually", "often"]
        for alt_word in alternative_words:
            if alt_word.lower() not in used_words:
                # Generate a simple entry for this alternative
                word_data = {
                    "word": alt_word,
                    "meaning_ru": "замечательный" if alt_word == "wonderful" else "удивительный" if alt_word == "amazing" else "интересный" if alt_word == "interesting" else "важный" if alt_word == "important" else "разный" if alt_word == "different" else "вместе" if alt_word == "together" else "всегда" if alt_word == "always" else "иногда" if alt_word == "sometimes" else "обычно" if alt_word == "usually" else "часто",
                    "pronunciation": "/ˈwʌndərfəl/" if alt_word == "wonderful" else "/əˈmeɪzɪŋ/" if alt_word == "amazing" else "/ˈɪntrəstɪŋ/" if alt_word == "interesting" else "/ɪmˈpɔːrtənt/" if alt_word == "important" else "/ˈdɪfərənt/" if alt_word == "different" else "/təˈɡeðər/" if alt_word == "together" else "/ˈɔːlweɪz/" if alt_word == "always" else "/ˈsʌmtaɪmz/" if alt_word == "sometimes" else "/ˈjuːʒuəli/" if alt_word == "usually" else "/ˈɔːfən/",
                    "example_sentence": f"I think it's {alt_word}." if alt_word in ["wonderful", "amazing", "interesting", "important", "different"] else f"Let's go {alt_word}." if alt_word == "together" else f"I {alt_word} go to school." if alt_word in ["always", "sometimes", "usually", "often"] else f"This is {alt_word}."
                }
                break
    
    # Save to database
    cursor.execute("""
        INSERT OR REPLACE INTO word_of_the_day (date, word, meaning_ru, pronunciation, example_sentence)
        VALUES (?, ?, ?, ?, ?)
    """, (today, word_data["word"], word_data["meaning_ru"], word_data["pronunciation"], word_data["example_sentence"]))
    conn.commit()
    conn.close()
    
    return word_data

def format_word_of_day(word_data: dict) -> str:
    """Format the word of the day message."""
    return f"""<b>📚 Слово дня</b>

<b>{word_data['word']}</b> {word_data['pronunciation']}

<b>Значение:</b> {word_data['meaning_ru']}

<b>Пример:</b> {word_data['example_sentence']}

Попробуй использовать это слово в разговоре! 😊"""

def should_send_word_of_day(last_word_at: str) -> bool:
    """Check if we should send word of the day (once per day)."""
    if last_word_at is None:
        return True
    try:
        last_word = datetime.strptime(last_word_at, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        # Send if last one was more than 23 hours ago
        return (now - last_word) >= timedelta(hours=23)
    except Exception as e:
        print(f"Error parsing word of day timestamp: {e}")
        return True

def update_last_word_of_day(chat_id: int):
    """Update the last word of the day timestamp for a user."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users SET last_word_of_day_at = CURRENT_TIMESTAMP WHERE chat_id = ?
    """, (chat_id,))
    conn.commit()
    conn.close()

def get_daily_message_count(chat_id: int) -> int:
    """Get today's message count for a user."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT message_count FROM daily_message_count 
        WHERE chat_id = ? AND date = ?
    """, (chat_id, today))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def increment_daily_message_count(chat_id: int) -> int:
    """Increment and return today's message count for a user."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO daily_message_count (chat_id, date, message_count)
        VALUES (?, ?, 1)
        ON CONFLICT(chat_id, date) DO UPDATE SET message_count = message_count + 1
    """, (chat_id, today))
    cursor.execute("""
        SELECT message_count FROM daily_message_count 
        WHERE chat_id = ? AND date = ?
    """, (chat_id, today))
    count = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return count

def check_rate_limit(chat_id: int) -> bool:
    """Check if user has reached daily message limit. Returns True if within limit."""
    count = get_daily_message_count(chat_id)
    return count < DAILY_MESSAGE_LIMIT

# Initialize database on startup
init_db()

# ---- Per-user state ----
# count how many user turns we've seen, keep last two utterances to correct,
# and store a short rolling context for natural conversation.
user_turn_count: Dict[int, int] = defaultdict(int)
recent_user_msgs: Dict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=2))
recent_dialogue: Dict[int, Deque[Tuple[str, str]]] = defaultdict(lambda: deque(maxlen=10))
# structure for recent_dialogue: deque of (speaker, text) with speaker in {"user","bot"}

# Users who must submit an email before chatting
awaiting_email: Set[int] = set()
# Admins waiting to type an email for add/remove
admin_pending_action: Dict[int, str] = {}  # chat_id -> "add" | "remove"

ASK_EMAIL_MSG = (
    "Привет! 👋 Чтобы пользоваться ботом, введи свой email.\n\n"
    "Пример: <code>name@example.com</code>"
)
NO_ACCESS_MSG = (
    "Этот email не найден в списке доступа.\n\n"
    "Напиши администратору, чтобы получить доступ к боту.\n"
    "После добавления твоего email отправь его сюда ещё раз."
)

def _main_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("Start"))
    kb.add(KeyboardButton("👋 Hi"), KeyboardButton("How are you?"))
    kb.add(KeyboardButton("Let's talk about food"), KeyboardButton("Tell me a fun fact"))
    kb.add(KeyboardButton("📚 Word of the Day"))
    return kb

def _admin_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("➕ Add email"), KeyboardButton("➖ Remove email"))
    kb.add(KeyboardButton("📋 List emails"), KeyboardButton("✅ Done"))
    return kb

def _welcome_message(chat_id: int):
    bot.send_message(
        chat_id,
        "Привет! Я твой напарник для практики английского. Давай просто поболтаем как друз7ья 😊 \n"
        "Расскажи мне что-нибудь о своём дне или выбери один из вариантов ниже.\n"
        "Давай поговорим на английском. Расскажи мне о своём дне на английском.",
        reply_markup=_main_keyboard(),
    )

def _request_email(chat_id: int):
    awaiting_email.add(chat_id)
    bot.send_message(chat_id, ASK_EMAIL_MSG, reply_markup=ReplyKeyboardRemove())

def _try_register_email(message) -> bool:
    """Process an email submission. Returns True if the message was handled as email flow."""
    chat_id = message.chat.id
    text = (message.text or "").strip()
    username = message.from_user.username if message.from_user else None
    first_name = message.from_user.first_name if message.from_user else None

    if not is_valid_email(text):
        bot.send_message(
            chat_id,
            "Это не похоже на email. Отправь адрес в формате <code>name@example.com</code>",
        )
        return True

    email = normalize_email(text)
    if not is_email_allowed(email):
        bot.send_message(chat_id, NO_ACCESS_MSG)
        # Keep awaiting so they can retry after admin adds them
        awaiting_email.add(chat_id)
        return True

    save_user(chat_id, username, first_name, email=email, update_reminder=True)
    awaiting_email.discard(chat_id)
    bot.send_message(chat_id, f"Отлично! Email <code>{email}</code> подтверждён. Доступ открыт ✅")
    _welcome_message(chat_id)
    return True

def ensure_access(message) -> bool:
    """
    Gate chat access behind email allowlist.
    Returns True if the user may proceed with normal chat.
    """
    chat_id = message.chat.id

    if is_admin(chat_id):
        awaiting_email.discard(chat_id)
        return True

    if user_has_access(chat_id):
        awaiting_email.discard(chat_id)
        return True

    # Already prompted — treat next non-command message as an email
    if chat_id in awaiting_email and message.text and not str(message.text).startswith("/"):
        _try_register_email(message)
        return False

    user = get_user(chat_id)
    if user and user[3] and not is_email_allowed(user[3]):
        bot.send_message(chat_id, NO_ACCESS_MSG)
        awaiting_email.add(chat_id)
        return False

    # First contact or missing email
    _request_email(chat_id)
    return False

SYSTEM_PROMPT = """You are a warm, human-like conversation partner helping the user practice everyday English.
Your style is friendly, curious, natural, and concise—like a supportive friend.
Your job is to help the user practice easy English (A2 level). 
Speak like a kind friend, with short and simple sentences.. Use contractions. Avoid lists unless asked.
In each reply:
1) Briefly respond to what they said, adding a human touch (empathy, small talk).
2) Ask ONE short follow-up question to keep the chat going.
3) DO NOT correct their English in this main reply (corrections are done in another message).

General guidelines:
- Keep replies to 1–3 short sentences plus one question.
- Use everyday vocabulary. No teacherly tone, no role-play or titles.
- If they ask for help, give a short, direct answer (still friendly).
- If they share feelings, acknowledge them kindly.
- Never lecture. Keep it light and conversational.
- Even if the user says something in Russian, answer in English.
"""

CORRECTION_PROMPT = """You help learners improve English gently.
You will receive the last two user messages. Provide light corrections only.
Rules:
- Be supportive and brief.
- Show up to 3 corrections total across both messages. If messages are already fine, say so.
- For each correction: show ❌ original, ✅ fixed, and a VERY short explanation in Russian (max 8 words).
- Keep vocabulary at A2–B1 level.
- No long explanations, no grammar jargon.
- End with one encouraging sentence in Russian.
- IMPORTANT: All corrections, explanations, and the final encouraging sentence must be in Russian.
- The original and fixed examples should still show the English text, but all explanations should be in Russian.
Return in simple, readable bullet points.
"""

def _chat_response(context: List[Dict[str, str]]) -> str:
    """Call OpenAI for a friendly chat response."""
    if client is None:
        return "Hi! (OpenAI SDK not installed). Tell me about your day 😊"
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.7,
        max_tokens=180,
        messages=context
    )
    return resp.choices[0].message.content.strip()

def _correction_response(last_two_user_msgs: List[str]) -> str:
    """Call OpenAI to produce gentle corrections for the last two messages."""
    if client is None:
        return "Всё отлично! Продолжай в том же духе—у тебя получается! 🌟"
    msgs = [
        {"role": "system", "content": CORRECTION_PROMPT},
        {"role": "user", "content": "Last two user messages:\n1) " + (last_two_user_msgs[0] if len(last_two_user_msgs) > 0 else "") +
                                     "\n2) " + (last_two_user_msgs[1] if len(last_two_user_msgs) > 1 else "")}
    ]
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.3,
        max_tokens=220,
        messages=msgs
    )
    return resp.choices[0].message.content.strip()

def _build_context(chat_id: int, user_msg: str) -> List[Dict[str, str]]:
    """Build a compact chat context for the model (system + a few recent turns + current user)."""
    ctx = [{"role": "system", "content": SYSTEM_PROMPT}]
    # Add brief dialogue memory
    for speaker, text in list(recent_dialogue[chat_id]):
        role = "assistant" if speaker == "bot" else "user"
        ctx.append({"role": role, "content": text})
    # Current user message
    ctx.append({"role": "user", "content": user_msg})
    return ctx


@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id = message.chat.id

    if not ensure_access(message):
        return

    username = message.from_user.username if message.from_user else None
    first_name = message.from_user.first_name if message.from_user else None
    save_user(chat_id, username, first_name, update_reminder=True)

    user_turn_count[chat_id] = 0
    recent_user_msgs[chat_id].clear()
    recent_dialogue[chat_id].clear()
    _welcome_message(chat_id)


@bot.message_handler(commands=["admin"])
def handle_admin(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        bot.send_message(chat_id, "Эта команда доступна только администраторам.")
        return

    awaiting_email.discard(chat_id)
    admin_pending_action.pop(chat_id, None)
    emails = list_allowed_emails()
    count = len(emails)
    bot.send_message(
        chat_id,
        f"<b>Admin panel</b>\n\n"
        f"Allowed emails: <b>{count}</b>\n\n"
        "Выбери действие или отправь:\n"
        "• <code>/addemail name@example.com</code>\n"
        "• <code>/removeemail name@example.com</code>\n"
        "• <code>/listemails</code>",
        reply_markup=_admin_keyboard(),
    )


@bot.message_handler(commands=["addemail"])
def handle_addemail_cmd(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        bot.send_message(chat_id, "Эта команда доступна только администраторам.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        admin_pending_action[chat_id] = "add"
        bot.send_message(chat_id, "Отправь email, который нужно добавить:")
        return

    _admin_add_email(chat_id, parts[1])


@bot.message_handler(commands=["removeemail"])
def handle_removeemail_cmd(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        bot.send_message(chat_id, "Эта команда доступна только администраторам.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        admin_pending_action[chat_id] = "remove"
        bot.send_message(chat_id, "Отправь email, который нужно удалить:")
        return

    _admin_remove_email(chat_id, parts[1])


@bot.message_handler(commands=["listemails"])
def handle_listemails_cmd(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        bot.send_message(chat_id, "Эта команда доступна только администраторам.")
        return
    _admin_list_emails(chat_id)


def _admin_add_email(chat_id: int, raw_email: str):
    email = normalize_email(raw_email)
    if not is_valid_email(email):
        bot.send_message(chat_id, "Некорректный email. Пример: <code>name@example.com</code>")
        return
    if add_allowed_email(email, added_by=chat_id):
        bot.send_message(chat_id, f"Добавлено: <code>{email}</code> ✅", reply_markup=_admin_keyboard())
    else:
        bot.send_message(chat_id, f"Уже в списке: <code>{email}</code>", reply_markup=_admin_keyboard())
    admin_pending_action.pop(chat_id, None)


def _admin_remove_email(chat_id: int, raw_email: str):
    email = normalize_email(raw_email)
    if remove_allowed_email(email):
        bot.send_message(chat_id, f"Удалено: <code>{email}</code> ✅", reply_markup=_admin_keyboard())
    else:
        bot.send_message(chat_id, f"Не найдено: <code>{email}</code>", reply_markup=_admin_keyboard())
    admin_pending_action.pop(chat_id, None)


def _admin_list_emails(chat_id: int):
    emails = list_allowed_emails()
    if not emails:
        bot.send_message(chat_id, "Список пуст. Добавь email через ➕ Add email.", reply_markup=_admin_keyboard())
        return
    lines = "\n".join(f"• <code>{e}</code>" for e in emails)
    bot.send_message(chat_id, f"<b>Allowed emails ({len(emails)})</b>\n\n{lines}", reply_markup=_admin_keyboard())


@bot.message_handler(func=lambda m: True)
def handle_chat(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()

    # Admin panel button / pending email actions
    if is_admin(chat_id):
        if text == "➕ Add email":
            admin_pending_action[chat_id] = "add"
            bot.send_message(chat_id, "Отправь email, который нужно добавить:")
            return
        if text == "➖ Remove email":
            admin_pending_action[chat_id] = "remove"
            bot.send_message(chat_id, "Отправь email, который нужно удалить:")
            return
        if text == "📋 List emails":
            _admin_list_emails(chat_id)
            return
        if text == "✅ Done":
            admin_pending_action.pop(chat_id, None)
            bot.send_message(chat_id, "Admin mode closed.", reply_markup=_main_keyboard())
            return
        if chat_id in admin_pending_action:
            action = admin_pending_action[chat_id]
            if action == "add":
                _admin_add_email(chat_id, text)
            elif action == "remove":
                _admin_remove_email(chat_id, text)
            return

    if text == "Start":
        handle_start(message)
        return

    if not ensure_access(message):
        return

    username = message.from_user.username if message.from_user else None
    first_name = message.from_user.first_name if message.from_user else None
    
    # Save/update user in database (update reminder time since they're actively using the bot)
    # This ensures existing users who message again won't get an immediate reminder
    save_user(chat_id, username, first_name, update_reminder=True)

    # Handle "Word of the Day" button (doesn't count toward rate limit)
    if text == "📚 Word of the Day":
        try:
            word_data = get_today_word()
            word_message = format_word_of_day(word_data)
            bot.send_message(chat_id, word_message)
        except Exception as e:
            print(f"Error sending word of day to {chat_id}: {e}")
            bot.send_message(chat_id, "Извини, не могу получить слово дня прямо сейчас. Попробуй позже! 😊")
        return

    # Check rate limit before processing chat messages
    if not check_rate_limit(chat_id):
        # User has reached daily limit
        bot.send_message(
            chat_id,
            "Отлично потренировались сегодня! 😊\n\nДавай продолжим завтра. Хорошего дня!"
        )
        return
    
    # Increment message count
    increment_daily_message_count(chat_id)

    # Update state
    user_turn_count[chat_id] += 1
    recent_user_msgs[chat_id].append(text)

    # Build and send friendly reply
    context = _build_context(chat_id, text)
    reply = _chat_response(context)
    bot.send_message(chat_id, reply)
    recent_dialogue[chat_id].append(("user", text))
    recent_dialogue[chat_id].append(("bot", reply))

    # Every 2 interactions → send gentle corrections
    if user_turn_count[chat_id] % 3 == 0:
        last_two = list(recent_user_msgs[chat_id])
        correction = _correction_response(last_two)
        # Prefix to make it feel like a separate aside
        bot.send_message(chat_id, f"<b>Быстрые советы 🎯</b>\n{correction}")

# ---- Daily reminder scheduler ----
REMINDER_MESSAGES = [
    "Привет! 👋 Готов попрактиковать английский сегодня? Расскажи, что у тебя на уме!",
    "Доброе утро! ☀️ Давай поболтаем на английском. Как дела?",
    "Привет! 😊 Хочешь немного попрактиковаться? Я здесь, чтобы поболтать!",
    "Привет, друг! 🌟 Время для практики английского. Что нового?",
    "Привет! 👋 Давай поговорим на английском сегодня. Как проходит твой день?"
]

WORD_OF_DAY_PROMPT = """Generate a word of the day for English learners at A2 (beginner) level.
The word should be useful for everyday conversation, not too difficult, but still educational.

IMPORTANT: The word MUST be different from any previously used words. Check the excluded words list carefully.

Return ONLY a JSON object with this exact structure (no markdown, no extra text):
{
    "word": "the English word",
    "meaning_ru": "translation and meaning in Russian",
    "pronunciation": "phonetic pronunciation (like /wɜːrd/ or simple guide like 'wurd')",
    "example_sentence": "a simple example sentence using this word (A2 level English)"
}

Example:
{
    "word": "beautiful",
    "meaning_ru": "красивый, прекрасный",
    "pronunciation": "/ˈbjuːtɪfəl/",
    "example_sentence": "The sunset is very beautiful today."
}

Make sure the word is appropriate for A2 level beginners and is NOT in the excluded words list."""

def should_send_reminder(last_reminder_at: str, last_active_at: str = None) -> bool:
    """Check if we should send a reminder (once per day).
    
    Args:
        last_reminder_at: Timestamp of last reminder sent
        last_active_at: Timestamp of last user activity (optional, for extra safety)
    """
    # If user was active very recently (within last hour), don't send reminder
    if last_active_at:
        try:
            last_active = datetime.strptime(last_active_at, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            if (now - last_active) < timedelta(hours=1):
                return False  # Too recent, skip reminder
        except:
            pass
    
    if last_reminder_at is None:
        return True
    try:
        # SQLite timestamps are in format: YYYY-MM-DD HH:MM:SS
        last_reminder = datetime.strptime(last_reminder_at, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        # Send reminder if last one was more than 23 hours ago
        return (now - last_reminder) >= timedelta(hours=23)
    except Exception as e:
        # If parsing fails, send reminder anyway
        print(f"Error parsing reminder timestamp: {e}")
        return True

def send_daily_reminders():
    """Send reminder messages to all users once per day."""
    import random
    while True:
        try:
            users = get_all_users()
            reminder_msg = random.choice(REMINDER_MESSAGES)
            
            for chat_id, username, first_name, last_reminder_at, last_active_at, last_word_of_day_at in users:
                if should_send_reminder(last_reminder_at, last_active_at):
                    try:
                        bot.send_message(chat_id, reminder_msg)
                        update_last_reminder(chat_id)
                        print(f"Sent reminder to user {chat_id}")
                    except Exception as e:
                        print(f"Failed to send reminder to {chat_id}: {e}")
            
            # Check every hour
            time.sleep(3600)  # 3600 seconds = 1 hour
        except Exception as e:
            print(f"Error in reminder scheduler: {e}")
            time.sleep(3600)

def send_word_of_day():
    """Send word of the day to all users once per day."""
    while True:
        try:
            # Get today's word (will generate if needed)
            word_data = get_today_word()
            word_message = format_word_of_day(word_data)
            
            users = get_all_users()
            
            for chat_id, username, first_name, last_reminder_at, last_active_at, last_word_of_day_at in users:
                if should_send_word_of_day(last_word_of_day_at):
                    try:
                        bot.send_message(chat_id, word_message)
                        update_last_word_of_day(chat_id)
                        print(f"Sent word of the day to user {chat_id}")
                    except Exception as e:
                        print(f"Failed to send word of day to {chat_id}: {e}")
            
            # Check every hour
            time.sleep(3600)  # 3600 seconds = 1 hour
        except Exception as e:
            print(f"Error in word of day scheduler: {e}")
            time.sleep(3600)

def start_reminder_scheduler():
    """Start the reminder scheduler in a background thread."""
    reminder_thread = threading.Thread(target=send_daily_reminders, daemon=True)
    reminder_thread.start()
    print("Daily reminder scheduler started")

def start_word_of_day_scheduler():
    """Start the word of the day scheduler in a background thread."""
    word_thread = threading.Thread(target=send_word_of_day, daemon=True)
    word_thread.start()
    print("Word of the day scheduler started")

if __name__ == "__main__":
    print("Friend bot running...")
    start_reminder_scheduler()
    start_word_of_day_scheduler()
    bot.infinity_polling()
