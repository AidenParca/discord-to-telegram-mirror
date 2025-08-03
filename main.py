# MEP.py
import requests
import os
import json
import re
import time
from datetime import datetime, timedelta
from io import BytesIO

# --- Configuration Loading ---
def load_config():
    """Loads configuration from config.json"""
    try:
        with open('config.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        log("FATAL: config.json not found. Please create it.", "ERROR")
        exit(1)
    except json.JSONDecodeError:
        log("FATAL: config.json is not valid JSON.", "ERROR")
        exit(1)

CONFIG = load_config()

# --- Constants from Config ---
DISCORD_BOT_TOKEN = CONFIG.get('DISCORD_BOT_TOKEN')
TELEGRAM_BOT_TOKEN = CONFIG.get('TELEGRAM_BOT_TOKEN')
CHANNEL_MAPPING = CONFIG.get('CHANNEL_MAPPING', {})
FILTER_WORDS = CONFIG.get('FILTER_WORDS', [])
MESSAGE_WINDOW_HOURS = CONFIG.get('MESSAGE_WINDOW_HOURS', 1)
TRACKING_FILE = 'upload_history.json'
REQUEST_DELAY = 3

# --- API Headers and URLs ---
DISCORD_HEADERS = {'Authorization': f'Bot {DISCORD_BOT_TOKEN}'}
TELEGRAM_API_URL = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}'

def log(message, level="INFO"):
    """Simple logging function."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")

def filter_content(content):
    """Removes specified words/phrases from content, case-insensitively."""
    if not content:
        return ""
    for word in FILTER_WORDS:
        content = re.sub(re.escape(word), '', content, flags=re.IGNORECASE)
    return content.strip()

def load_upload_history():
    """Loads the history of processed message IDs from a JSON file."""
    try:
        with open(TRACKING_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_upload_history(history):
    """Saves the history of processed message IDs to a JSON file."""
    with open(TRACKING_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def is_within_window(message_timestamp):
    """Checks if a message timestamp is within the processing window."""
    try:
        msg_time = datetime.fromisoformat(message_timestamp.replace('Z', '+00:00'))
    except ValueError:
        log(f"Could not parse timestamp: {message_timestamp}", "ERROR")
        return False
    return (datetime.now(msg_time.tzinfo) - msg_time) < timedelta(hours=MESSAGE_WINDOW_HOURS)

def get_discord_messages(channel_id):
    """Fetches the last 50 messages from a given Discord channel using the Bot API."""
    url = f'https://discord.com/api/v9/channels/{channel_id}/messages'
    params = {'limit': 50}
    try:
        response = requests.get(url, headers=DISCORD_HEADERS, params=params, timeout=30)
        response.raise_for_status()
        return sorted(response.json(), key=lambda x: x['timestamp'])
    except requests.exceptions.RequestException as e:
        log(f"API or Network Error fetching from Discord: {e}", "ERROR")
        return None

def clean_content_for_telegram(content):
    """Cleans Discord-specific formatting for a plain text Telegram message."""
    if not content:
        return ""
    # Remove all mentions, custom emojis, and formatting
    content = re.sub(r'<a?:\w+:\d+>', '', content)
    content = re.sub(r'<@!?\d+>|<@&\d+>|<#\d+>', '', content)
    content = re.sub(r'[*_~`|]', '', content)
    return filter_content(content)

def process_embed(embed):
    """Processes a Discord embed into a clean, readable text block."""
    parts = []
    if title := filter_content(embed.get('title', '')):
        parts.append(f"**{title}**") # Using markdown for Telegram
    if description := clean_content_for_telegram(embed.get('description', '')):
        parts.append(description)
    if 'fields' in embed:
        for field in embed['fields']:
            name = filter_content(field.get('name', ''))
            value = clean_content_for_telegram(field.get('value', ''))
            if name and value:
                parts.append(f"**{name}**\n{value}")
    return '\n\n'.join(parts)

def send_to_telegram(chat_id, thread_id, text_content=None, image_urls=None):
    """Sends content (text and/or images) to a specific Telegram thread."""
    history = load_upload_history()
    media = []
    
    # Prepare media group if multiple images exist
    if image_urls:
        for i, url in enumerate(image_urls):
            if url in history: continue
            media_item = {'type': 'photo', 'media': url}
            if i == 0 and text_content: # Add caption to the first image
                media_item['caption'] = text_content
                media_item['parse_mode'] = 'Markdown'
            media.append(media_item)

    try:
        if len(media) > 1:
            requests.post(f'{TELEGRAM_API_URL}/sendMediaGroup', json={'chat_id': chat_id, 'message_thread_id': thread_id, 'media': media})
            log(f"Sent media group with {len(media)} images.", "INFO")
        elif len(media) == 1:
            requests.post(f'{TELEGRAM_API_URL}/sendPhoto', data={'chat_id': chat_id, 'message_thread_id': thread_id, 'photo': media[0]['media'], 'caption': media[0].get('caption'), 'parse_mode': 'Markdown'})
            log("Sent single photo.", "INFO")
        elif text_content:
            requests.post(f'{TELEGRAM_API_URL}/sendMessage', json={'chat_id': chat_id, 'message_thread_id': thread_id, 'text': text_content, 'parse_mode': 'Markdown'})
            log("Sent text-only message.", "INFO")
        else:
            return False, [] # No content to send
        
        # Mark all sent items as processed
        sent_items = [url for url in image_urls]
        return True, sent_items

    except requests.exceptions.RequestException as e:
        log(f"Failed to send to Telegram: {e}", "ERROR")
        return False, []

def process_channel(discord_channel, tg_chat, tg_thread):
    """Main processing logic for a single channel mapping."""
    log(f"Processing channel {discord_channel}", "INFO")
    messages = get_discord_messages(discord_channel)
    if not messages:
        log(f"No messages found for channel {discord_channel}", "WARNING")
        return

    history = load_upload_history()
    processed_count = 0
    for message in messages:
        message_id = message['id']
        if message_id in history or not is_within_window(message['timestamp']):
            continue

        text_parts = []
        if content := clean_content_for_telegram(message.get('content')):
            text_parts.append(content)
        
        for embed in message.get('embeds', []):
            if embed_text := process_embed(embed):
                text_parts.append(embed_text)
        
        full_text = '\n\n'.join(text_parts)
        
        image_urls = [attach['url'] for attach in message.get('attachments', []) if 'image' in attach.get('content_type', '')]
        
        success, sent_items = send_to_telegram(tg_chat, tg_thread, full_text, image_urls)
        
        if success:
            history[message_id] = datetime.utcnow().isoformat()
            for item in sent_items:
                history[item] = datetime.utcnow().isoformat()
            processed_count += 1
        
        save_upload_history(history)
        time.sleep(REQUEST_DELAY)

    log(f"Processed {processed_count} new messages from channel {discord_channel}", "INFO")

def main():
    """Main execution function."""
    log("Starting Discord to Telegram mirror", "INFO")
    if not all([DISCORD_BOT_TOKEN, TELEGRAM_BOT_TOKEN]):
        log("FATAL: Discord or Telegram token not set in config.json.", "ERROR")
        return
        
    for discord_channel, mapping in CHANNEL_MAPPING.items():
        tg_chat, tg_thread = mapping
        process_channel(discord_channel, tg_chat, tg_thread)
    log("Mirror run finished.", "INFO")

if __name__ == '__main__':
    main()