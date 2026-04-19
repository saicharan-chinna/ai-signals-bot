"""
AI Signals Telegram Bot
Share any social media link or text → auto-saved to your Notion "AI Signals" database.
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s]+")

PLATFORM_MAP = {
    "twitter.com": "Twitter",
    "x.com": "Twitter",
    "reddit.com": "Reddit",
    "linkedin.com": "LinkedIn",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "github.com": "GitHub",
    "instagram.com": "Instagram",
    "substack.com": "Newsletter",
    "medium.com": "Blog",
    "arxiv.org": "Research",
    "huggingface.co": "HuggingFace",
}

TYPE_KEYWORDS = {
    "Research": ["paper", "arxiv", "research", "study", "findings", "published"],
    "Tool": ["tool", "library", "sdk", "api", "release", "launch", "app", "plugin"],
    "Model": ["model", "llm", "gpt", "claude", "gemini", "mistral", "weights"],
    "News": ["news", "announcement", "breaking", "update", "report"],
    "Idea": ["idea", "thread", "thoughts", "opinion", "take"],
}


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text)
    return match.group(0) if match else None


def detect_platform(url: str | None) -> str:
    if not url:
        return "Other"
    for domain, name in PLATFORM_MAP.items():
        if domain in url:
            return name
    return "Other"


def detect_type_heuristic(text: str) -> str:
    text_lower = text.lower()
    for type_name, keywords in TYPE_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return type_name
    return "News"


def make_title_heuristic(text: str, url: str | None) -> str:
    # Use first non-URL line as title, trimmed to 80 chars
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("http")]
    if lines:
        return lines[0][:80]
    if url:
        return url[:80]
    return "AI Finding"


def make_summary_heuristic(text: str) -> str:
    return text.strip()[:300]


# ── Claude-powered extraction (optional) ─────────────────────────────────────

async def extract_with_claude(text: str, url: str | None) -> dict:
    """Use Claude claude-haiku-4-5 to extract structured metadata from shared content."""
    prompt = f"""Extract structured metadata from this social media content and return JSON only.

Content:
{text}
URL: {url or "none"}

Return a JSON object with these exact keys:
- title: short descriptive title (max 80 chars)
- summary: 1-2 sentence summary of what this is about (max 200 chars)
- type: one of [Model, Tool, Research, Idea, News]
- relevance_score: integer 1-5 (5 = highly relevant AI finding, 1 = tangentially related)

Return only valid JSON, no markdown, no explanation."""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            content = resp.json()["content"][0]["text"].strip()
            # Strip markdown code blocks if present
            content = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.MULTILINE)
            return json.loads(content)
    except Exception as e:
        logger.warning(f"Claude extraction failed: {e}, falling back to heuristics")
        return {}


# ── Notion API ────────────────────────────────────────────────────────────────

async def create_notion_entry(title: str, url: str | None, source: str, type_: str,
                               summary: str, relevance: int) -> str | None:
    """Create a page in the AI Signals Notion database. Returns the page URL."""
    today = date.today().isoformat()

    properties = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Summary": {"rich_text": [{"text": {"content": summary[:2000]}}]},
        "Type": {"select": {"name": type_}},
        "Source": {"multi_select": [{"name": source}]},
        "Week": {"date": {"start": today}},
        "Action": {"select": {"name": "Explore"}},
        "Relevance Score": {"number": relevance},
    }

    if url:
        properties["Link"] = {"url": url}

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.notion.com/v1/pages",
                headers={
                    "Authorization": f"Bearer {NOTION_TOKEN}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            page = resp.json()
            page_id = page["id"].replace("-", "")
            return f"https://notion.so/{page_id}"
    except Exception as e:
        logger.error(f"Notion API error: {e}")
        if hasattr(e, "response") and e.response is not None:
            logger.error(f"Response: {e.response.text}")
        return None


# ── Bot handlers ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *AI Signals Bot* ready!\n\n"
        "Share any link, tweet, post, or text about AI and I'll save it to your Notion database.\n\n"
        "Just paste or forward anything here.",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # Gather text from message or forwarded content
    text = ""
    if msg.text:
        text = msg.text
    elif msg.caption:
        text = msg.caption
    elif msg.forward_origin:
        text = "[Forwarded message with no text]"

    if not text.strip():
        await msg.reply_text("⚠️ I couldn't find any text in that message. Try forwarding or pasting text/links.")
        return

    url = extract_url(text)
    source = detect_platform(url)

    # Status message
    status_msg = await msg.reply_text("⏳ Processing...")

    # Try Claude first, fall back to heuristics
    metadata = {}
    if ANTHROPIC_API_KEY:
        metadata = await extract_with_claude(text, url)

    title = metadata.get("title") or make_title_heuristic(text, url)
    summary = metadata.get("summary") or make_summary_heuristic(text)
    type_ = metadata.get("type") or detect_type_heuristic(text)
    relevance = metadata.get("relevance_score") or 3

    # Ensure type_ is valid
    valid_types = ["Model", "Tool", "Research", "Idea", "News"]
    if type_ not in valid_types:
        type_ = "News"

    # Create Notion entry
    notion_url = await create_notion_entry(title, url, source, type_, summary, relevance)

    if notion_url:
        await status_msg.edit_text(
            f"✅ *Saved to AI Signals!*\n\n"
            f"📌 *{title}*\n"
            f"🏷 {type_} · {source} · ⭐ {relevance}/5\n\n"
            f"[View in Notion]({notion_url})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    else:
        await status_msg.edit_text(
            "❌ Failed to save to Notion. Check your token and that the integration has access to the database."
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set in .env")
    if not NOTION_TOKEN:
        raise ValueError("NOTION_TOKEN not set in .env")
    if not NOTION_DATABASE_ID:
        raise ValueError("NOTION_DATABASE_ID not set in .env")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION | filters.FORWARDED, handle_message))

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
