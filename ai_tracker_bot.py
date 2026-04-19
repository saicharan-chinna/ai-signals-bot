"""
AI Signals Telegram Bot
Share any social media link or text -> auto-saved to your Notion "AI Signals" database.
"""

import os
import re
import json
import logging
import httpx
from datetime import date
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s]+")

PLATFORM_MAP = {
    "twitter.com": "Twitter", "x.com": "Twitter", "reddit.com": "Reddit",
    "linkedin.com": "LinkedIn", "youtube.com": "YouTube", "youtu.be": "YouTube",
    "github.com": "GitHub", "instagram.com": "Instagram", "substack.com": "Newsletter",
    "medium.com": "Blog", "arxiv.org": "Research", "huggingface.co": "HuggingFace",
}

TYPE_KEYWORDS = {
    "Research": ["paper", "arxiv", "research", "study", "findings", "published"],
    "Tool": ["tool", "library", "sdk", "api", "release", "launch", "app", "plugin"],
    "Model": ["model", "llm", "gpt", "claude", "gemini", "mistral", "weights"],
    "News": ["news", "announcement", "breaking", "update", "report"],
    "Idea": ["idea", "thread", "thoughts", "opinion", "take"],
}

def extract_url(text):
    match = URL_RE.search(text)
    return match.group(0) if match else None

def detect_platform(url):
    if not url: return "Other"
    for domain, name in PLATFORM_MAP.items():
        if domain in url: return name
    return "Other"

def detect_type_heuristic(text):
    text_lower = text.lower()
    for type_name, keywords in TYPE_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords): return type_name
    return "News"

def make_title_heuristic(text, url):
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("http")]
    if lines: return lines[0][:80]
    if url: return url[:80]
    return "AI Finding"

def make_summary_heuristic(text):
    return text.strip()[:300]

async def extract_with_claude(text, url):
    prompt = f"""Extract structured metadata from this social media content and return JSON only.
Content: {text}
URL: {url or "none"}
Return JSON with keys: title (max 80 chars), summary (1-2 sentences, max 200 chars), type (one of: Model/Tool/Research/Idea/News), relevance_score (1-5 int).
Return only valid JSON."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]})
            resp.raise_for_status()
            content = resp.json()["content"][0]["text"].strip()
            content = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.MULTILINE)
            return json.loads(content)
    except Exception as e:
        logger.warning(f"Claude extraction failed: {e}")
        return {}

async def create_notion_entry(title, url, source, type_, summary, relevance):
    today = date.today().isoformat()
    properties = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Summary": {"rich_text": [{"text": {"content": summary[:2000]}}]},
        "Type": {"select": {"name": type_}},
        "Source": {"select": {"name": source}},
        "Week": {"date": {"start": today}},
        "Action": {"select": {"name": "Explore"}},
        "Relevance Score": {"number": relevance},
    }
    if url: properties["Link"] = {"url": url}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post("https://api.notion.com/v1/pages",
                headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
                json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties})
            resp.raise_for_status()
            page_id = resp.json()["id"].replace("-", "")
            return f"https://notion.so/{page_id}"
    except Exception as e:
        logger.error(f"Notion API error: {e}")
        return None

async def start(update, context):
    await update.message.reply_text("*AI Signals Bot* ready!\n\nShare any link, tweet, post, or text about AI and I'll save it to your Notion database.\n\nJust paste or forward anything here.", parse_mode="Markdown")

async def handle_message(update, context):
    msg = update.message
    text = msg.text or msg.caption or ("[Forwarded]" if msg.forward_origin else "")
    if not text.strip():
        await msg.reply_text("Could not find any text. Try forwarding or pasting text/links.")
        return
    url = extract_url(text)
    source = detect_platform(url)
    status_msg = await msg.reply_text("Processing...")
    metadata = await extract_with_claude(text, url) if ANTHROPIC_API_KEY else {}
    title = metadata.get("title") or make_title_heuristic(text, url)
    summary = metadata.get("summary") or make_summary_heuristic(text)
    type_ = metadata.get("type") or detect_type_heuristic(text)
    relevance = metadata.get("relevance_score") or 3
    if type_ not in ["Model", "Tool", "Research", "Idea", "News"]: type_ = "News"
    notion_url = await create_notion_entry(title, url, source, type_, summary, relevance)
    if notion_url:
        await status_msg.edit_text(f"Saved to AI Signals!\n\n{title}\n{type_} - {source} - {relevance}/5\n\nView: {notion_url}")
    else:
        await status_msg.edit_text("Failed to save to Notion. Check your token and integration access.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION | filters.FORWARDED, handle_message))
    logger.info("Bot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
