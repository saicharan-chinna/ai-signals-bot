"""
AI Signals Telegram Bot
Share any social media link or text → fetches real content → auto-saved to Notion "AI Signals" database.
"""

import os
import re
import json
import logging
import httpx
from datetime import date
from html.parser import HTMLParser
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

# ── URL helpers ───────────────────────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s\)>\"]+")

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


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text)
    return match.group(0).rstrip(".,;)") if match else None


def detect_platform(url: str | None) -> str:
    if not url:
        return "Other"
    for domain, name in PLATFORM_MAP.items():
        if domain in url:
            return name
    return "Other"


# ── HTML meta-tag parser ──────────────────────────────────────────────────────

class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.og_title = ""
        self.og_description = ""
        self.og_type = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attrs.get("name", "").lower()
            prop = attrs.get("property", "").lower()
            content = attrs.get("content", "")
            if prop == "og:title":
                self.og_title = content
            elif prop == "og:description":
                self.og_description = content
            elif prop == "og:type":
                self.og_type = content
            elif name in ("description", "twitter:description"):
                self.description = content
            elif name == "twitter:title" and not self.og_title:
                self.og_title = content

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def best_title(self) -> str:
        return (self.og_title or self.title or "").strip()

    def best_description(self) -> str:
        return (self.og_description or self.description or "").strip()


# ── Platform-specific fetchers ────────────────────────────────────────────────

async def fetch_github(url: str, client: httpx.AsyncClient) -> dict:
    """Fetch GitHub repo/file info via GitHub API."""
    # Match owner/repo from URL
    m = re.search(r"github\.com/([^/]+)/([^/\?#]+)", url)
    if not m:
        return {}
    owner, repo = m.group(1), m.group(2).rstrip(".git")
    try:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            title = data.get("full_name", f"{owner}/{repo}")
            desc = data.get("description") or ""
            topics = ", ".join(data.get("topics", []))
            lang = data.get("language") or ""
            stars = data.get("stargazers_count", 0)
            summary_parts = [desc]
            if lang:
                summary_parts.append(f"Language: {lang}")
            if topics:
                summary_parts.append(f"Topics: {topics}")
            summary_parts.append(f"⭐ {stars} stars")
            return {
                "fetched_title": title,
                "fetched_body": " | ".join(p for p in summary_parts if p),
            }
    except Exception as e:
        logger.warning(f"GitHub API fetch failed: {e}")
    return {}


async def fetch_arxiv(url: str, client: httpx.AsyncClient) -> dict:
    """Fetch arXiv paper title and abstract."""
    m = re.search(r"arxiv\.org/(abs|pdf)/([0-9]+\.[0-9]+)", url)
    if not m:
        return {}
    arxiv_id = m.group(2)
    try:
        resp = await client.get(
            f"https://export.arxiv.org/abs/{arxiv_id}",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        )
        if resp.status_code == 200:
            html = resp.text
            # Extract title
            title_m = re.search(r'<h1 class="title mathjax"[^>]*>.*?<span[^>]*>Title:</span>\s*(.*?)</h1>', html, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
            # Extract abstract
            abs_m = re.search(r'<blockquote class="abstract mathjax"[^>]*>.*?<span[^>]*>Abstract:</span>\s*(.*?)</blockquote>', html, re.DOTALL)
            abstract = re.sub(r"<[^>]+>", " ", abs_m.group(1)).strip() if abs_m else ""
            abstract = re.sub(r"\s+", " ", abstract)
            return {
                "fetched_title": title,
                "fetched_body": abstract[:600],
            }
    except Exception as e:
        logger.warning(f"arXiv fetch failed: {e}")
    return {}


async def fetch_youtube(url: str, client: httpx.AsyncClient) -> dict:
    """Fetch YouTube video title and description via oEmbed."""
    try:
        resp = await client.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "fetched_title": data.get("title", ""),
                "fetched_body": f"By {data.get('author_name', '')} on YouTube",
            }
    except Exception as e:
        logger.warning(f"YouTube oEmbed fetch failed: {e}")
    return {}


async def fetch_generic(url: str, client: httpx.AsyncClient) -> dict:
    """Fetch any URL and extract og:title, og:description from HTML."""
    try:
        resp = await client.get(
            url,
            timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; AI-Signals-Bot/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        )
        if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
            parser = MetaParser()
            parser.feed(resp.text[:50000])  # Parse first 50k chars
            title = parser.best_title()
            body = parser.best_description()
            # Also grab first meaningful paragraph as fallback body
            if not body:
                text_content = re.sub(r"<[^>]+>", " ", resp.text[:30000])
                text_content = re.sub(r"\s+", " ", text_content).strip()
                body = text_content[:500]
            return {
                "fetched_title": title,
                "fetched_body": body,
            }
    except Exception as e:
        logger.warning(f"Generic URL fetch failed for {url}: {e}")
    return {}


async def fetch_url_content(url: str) -> dict:
    """Route URL to the appropriate fetcher and return fetched content."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        if "github.com" in url:
            result = await fetch_github(url, client)
        elif "arxiv.org" in url:
            result = await fetch_arxiv(url, client)
        elif "youtube.com" in url or "youtu.be" in url:
            result = await fetch_youtube(url, client)
        else:
            result = await fetch_generic(url, client)

        # Fall back to generic fetch if platform-specific returned nothing
        if not result.get("fetched_title") and "github.com" not in url:
            result = await fetch_generic(url, client)

        return result


# ── Claude-powered extraction ─────────────────────────────────────────────────

async def extract_with_claude(user_text: str, url: str | None, fetched: dict) -> dict:
    """Use Claude Haiku to extract structured metadata from enriched content."""
    fetched_title = fetched.get("fetched_title", "")
    fetched_body = fetched.get("fetched_body", "")

    context_parts = []
    if user_text and url not in user_text.strip():
        context_parts.append(f"User message: {user_text.strip()}")
    if fetched_title:
        context_parts.append(f"Page/content title: {fetched_title}")
    if fetched_body:
        context_parts.append(f"Content description/body:\n{fetched_body[:800]}")
    if url:
        context_parts.append(f"URL: {url}")

    context = "\n\n".join(context_parts) or user_text

    prompt = f"""You are an AI research curator. Analyze the content below and extract structured metadata. Return JSON only.

{context}

Return a JSON object with EXACTLY these keys:
- title: Meaningful short title (max 80 chars). NOT the URL. Should describe what this is (e.g. "Claude 3.5 Haiku — Anthropic's fastest model", "Attention Is All You Need — transformer paper", "AutoGPT — autonomous GPT-4 agent framework")
- summary: 2-3 sentence summary describing what this is, why it matters, and key insights (max 300 chars)
- type: one of [Model, Tool, Research, Idea, News] — pick the most fitting
- relevance_score: integer 1-5 for AI relevance (5=core AI/ML content, 4=AI-adjacent tool, 3=general tech, 2=loosely related, 1=tangential)

Rules:
- title must never be a raw URL
- If it's a GitHub repo, title = "RepoName — one-line description"
- If it's a paper, title = "Paper Title (venue/year if known)"
- If it's a YouTube video, title = the video title
- summary should be informative and specific, not generic

Return only valid JSON, no markdown, no explanation."""

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            raw = resp.json()["content"][0]["text"].strip()
            raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"Claude extraction failed: {e}")
        return {}


def build_metadata_from_fetched(user_text: str, url: str | None, fetched: dict) -> dict:
    """Heuristic fallback using fetched page content."""
    title = fetched.get("fetched_title") or ""
    body = fetched.get("fetched_body") or ""

    # Clean up title
    if title:
        # Remove site name suffixes like " · GitHub", " - YouTube", " | Medium"
        title = re.split(r"\s*[·\-\|]\s*(GitHub|YouTube|Medium|arXiv|Twitter|Reddit|LinkedIn|HuggingFace)$", title)[0].strip()
        title = title[:80]

    # Fall back to first non-URL line of user text
    if not title:
        lines = [l.strip() for l in user_text.splitlines() if l.strip() and not l.strip().startswith("http")]
        title = lines[0][:80] if lines else (url[:80] if url else "AI Finding")

    summary = body[:300] if body else user_text.strip()[:300]

    # Type detection from title + body + user text
    combined = (title + " " + summary + " " + user_text).lower()
    type_ = "News"
    type_keywords = {
        "Research": ["paper", "arxiv", "research", "study", "findings", "published", "abstract", "journal"],
        "Model": ["model", "llm", "gpt", "claude", "gemini", "mistral", "weights", "checkpoint", "fine-tun"],
        "Tool": ["tool", "library", "sdk", "api", "release", "launch", "app", "plugin", "framework", "repo", "package"],
        "Idea": ["idea", "thread", "thoughts", "opinion", "take", "essay", "blog", "perspective"],
    }
    for t, kws in type_keywords.items():
        if any(kw in combined for kw in kws):
            type_ = t
            break

    return {"title": title, "summary": summary, "type": type_, "relevance_score": 3}


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
        "Share any link, tweet, post, paper, or repo about AI — I'll fetch the content, "
        "extract a meaningful title and summary, and save it to your Notion database.\n\n"
        "Works with: GitHub repos, arXiv papers, YouTube videos, Twitter/X, Reddit, "
        "Medium, Substack, HuggingFace, and any URL.\n\n"
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

    status_msg = await msg.reply_text("🔍 Fetching content...")

    # Step 1: fetch real content from the URL
    fetched = {}
    if url:
        await status_msg.edit_text("🔍 Fetching content from URL...")
        fetched = await fetch_url_content(url)
        logger.info(f"Fetched for {url}: title={fetched.get('fetched_title', '')[:60]}")

    await status_msg.edit_text("🧠 Analyzing...")

    # Step 2: extract metadata — Claude if API key set, else heuristics
    metadata = {}
    if ANTHROPIC_API_KEY:
        metadata = await extract_with_claude(text, url, fetched)

    if not metadata or not metadata.get("title"):
        metadata = build_metadata_from_fetched(text, url, fetched)

    title = metadata.get("title") or "AI Finding"
    summary = metadata.get("summary") or ""
    type_ = metadata.get("type") or "News"
    relevance = metadata.get("relevance_score") or 3

    # Ensure type_ is valid
    valid_types = ["Model", "Tool", "Research", "Idea", "News"]
    if type_ not in valid_types:
        type_ = "News"

    # Clamp relevance to 1–5
    try:
        relevance = max(1, min(5, int(relevance)))
    except (TypeError, ValueError):
        relevance = 3

    # Step 3: save to Notion
    await status_msg.edit_text("💾 Saving to Notion...")
    notion_url = await create_notion_entry(title, url, source, type_, summary, relevance)

    if notion_url:
        await status_msg.edit_text(
            f"✅ *Saved to AI Signals!*\n\n"
            f"📌 *{title}*\n"
            f"📝 {summary[:150]}{'…' if len(summary) > 150 else ''}\n\n"
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
