"""
AI Signals Telegram Bot
Share any link, reel, short, or text → fetches content + transcripts → saves structured entry to Notion.
"""

import os
import re
import json
import xml.etree.ElementTree as ET
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
    "tiktok.com": "TikTok",
    "substack.com": "Newsletter",
    "medium.com": "Blog",
    "arxiv.org": "Research",
    "huggingface.co": "HuggingFace",
}

SHORT_VIDEO_DOMAINS = ["youtube.com/shorts", "youtu.be", "instagram.com/reel", "instagram.com/p/", "tiktok.com"]


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


def is_short_video(url: str | None) -> bool:
    if not url:
        return False
    return any(d in url for d in SHORT_VIDEO_DOMAINS)


def extract_youtube_id(url: str) -> str | None:
    patterns = [
        r"youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


# ── HTML meta-tag parser ──────────────────────────────────────────────────────

class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.og_title = ""
        self.og_description = ""
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


# ── YouTube transcript fetcher ────────────────────────────────────────────────

async def fetch_youtube_transcript(video_id: str, client: httpx.AsyncClient) -> str:
    """Fetch auto-generated captions for a YouTube video. Returns plain text."""
    try:
        # Fetch the watch page to find the caption track URL
        resp = await client.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=12,
        )
        html = resp.text

        # Extract caption track base URL from ytInitialPlayerResponse
        cap_match = re.search(r'"captionTracks":\s*\[.*?"baseUrl":\s*"([^"]+)"', html)
        if not cap_match:
            # Try timedtext API directly
            api_url = f"https://www.youtube.com/api/timedtext?v={video_id}&lang=en&fmt=json3"
            r2 = await client.get(api_url, timeout=8)
            if r2.status_code == 200:
                data = r2.json()
                events = data.get("events", [])
                texts = []
                for e in events:
                    for seg in e.get("segs", []):
                        t = seg.get("utf8", "").strip()
                        if t and t != "\n":
                            texts.append(t)
                return " ".join(texts)[:2000]
            return ""

        cap_url = cap_match.group(1).replace("\\u0026", "&")
        cap_resp = await client.get(cap_url + "&fmt=json3", timeout=8)
        if cap_resp.status_code == 200:
            try:
                data = cap_resp.json()
                events = data.get("events", [])
                texts = []
                for e in events:
                    for seg in e.get("segs", []):
                        t = seg.get("utf8", "").strip()
                        if t and t != "\n":
                            texts.append(t)
                return " ".join(texts)[:2000]
            except Exception:
                pass

        # Try XML format as last resort
        cap_resp_xml = await client.get(cap_url, timeout=8)
        if cap_resp_xml.status_code == 200:
            try:
                root = ET.fromstring(cap_resp_xml.text)
                texts = [elem.text or "" for elem in root.iter("text")]
                return " ".join(t.strip() for t in texts if t.strip())[:2000]
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"YouTube transcript fetch failed: {e}")
    return ""


# ── Platform-specific fetchers ────────────────────────────────────────────────

async def fetch_github(url: str, client: httpx.AsyncClient) -> dict:
    """Fetch GitHub repo info via GitHub API."""
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
            parts = [desc]
            if lang:
                parts.append(f"Language: {lang}")
            if topics:
                parts.append(f"Topics: {topics}")
            parts.append(f"⭐ {stars} stars")
            return {
                "fetched_title": title,
                "fetched_body": " | ".join(p for p in parts if p),
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
            title_m = re.search(r'<h1 class="title mathjax"[^>]*>.*?<span[^>]*>Title:</span>\s*(.*?)</h1>', html, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
            abs_m = re.search(r'<blockquote class="abstract mathjax"[^>]*>.*?<span[^>]*>Abstract:</span>\s*(.*?)</blockquote>', html, re.DOTALL)
            abstract = re.sub(r"<[^>]+>", " ", abs_m.group(1)).strip() if abs_m else ""
            abstract = re.sub(r"\s+", " ", abstract)
            return {"fetched_title": title, "fetched_body": abstract[:800]}
    except Exception as e:
        logger.warning(f"arXiv fetch failed: {e}")
    return {}


async def fetch_youtube(url: str, client: httpx.AsyncClient) -> dict:
    """Fetch YouTube video/short title via oEmbed + transcript via captions."""
    result = {}
    try:
        resp = await client.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            result["fetched_title"] = data.get("title", "")
            result["fetched_body"] = f"By {data.get('author_name', '')} on YouTube"
    except Exception as e:
        logger.warning(f"YouTube oEmbed failed: {e}")

    # Try to get transcript for all YouTube content
    vid_id = extract_youtube_id(url)
    if vid_id:
        transcript = await fetch_youtube_transcript(vid_id, client)
        if transcript:
            result["transcript"] = transcript
            if not result.get("fetched_body"):
                result["fetched_body"] = transcript[:300]

    return result


async def fetch_instagram(url: str, client: httpx.AsyncClient) -> dict:
    """Fetch Instagram reel/post via meta tags."""
    try:
        resp = await client.get(
            url,
            timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )
        if resp.status_code == 200:
            parser = MetaParser()
            parser.feed(resp.text[:50000])
            title = parser.best_title()
            body = parser.best_description()
            # Try to extract caption from JSON-LD or page script
            caption_m = re.search(r'"caption"\s*:\s*\{\s*"edges"\s*:\s*\[\s*\{\s*"node"\s*:\s*\{\s*"text"\s*:\s*"([^"]{10,})"', resp.text)
            if caption_m:
                body = caption_m.group(1).replace("\\n", " ").replace("\\", "")[:600]
            return {"fetched_title": title or "Instagram Reel", "fetched_body": body}
    except Exception as e:
        logger.warning(f"Instagram fetch failed: {e}")
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
            parser.feed(resp.text[:50000])
            title = parser.best_title()
            body = parser.best_description()
            if not body:
                text_content = re.sub(r"<[^>]+>", " ", resp.text[:30000])
                body = re.sub(r"\s+", " ", text_content).strip()[:500]
            return {"fetched_title": title, "fetched_body": body}
    except Exception as e:
        logger.warning(f"Generic URL fetch failed for {url}: {e}")
    return {}


async def fetch_url_content(url: str) -> dict:
    """Route URL to appropriate fetcher. Returns fetched content dict."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        if "github.com" in url:
            result = await fetch_github(url, client)
        elif "arxiv.org" in url:
            result = await fetch_arxiv(url, client)
        elif "youtube.com" in url or "youtu.be" in url:
            result = await fetch_youtube(url, client)
        elif "instagram.com" in url:
            result = await fetch_instagram(url, client)
        else:
            result = await fetch_generic(url, client)

        # Fall back to generic if platform-specific got nothing
        if not result.get("fetched_title") and "github.com" not in url and "instagram.com" not in url:
            generic = await fetch_generic(url, client)
            if generic.get("fetched_title"):
                result.update(generic)

        return result


# ── Claude-powered extraction + actionable brief ──────────────────────────────

async def extract_with_claude(user_text: str, url: str | None, fetched: dict, is_video: bool) -> dict:
    """Use Claude Haiku to extract metadata AND generate an actionable brief."""
    fetched_title = fetched.get("fetched_title", "")
    fetched_body = fetched.get("fetched_body", "")
    transcript = fetched.get("transcript", "")

    context_parts = []
    if user_text and url not in user_text.strip():
        context_parts.append(f"User note: {user_text.strip()}")
    if fetched_title:
        context_parts.append(f"Content title: {fetched_title}")
    if fetched_body:
        context_parts.append(f"Description:\n{fetched_body[:600]}")
    if transcript:
        context_parts.append(f"Audio transcript (auto-captions):\n{transcript[:1200]}")
    if url:
        context_parts.append(f"URL: {url}")

    context = "\n\n".join(context_parts) or user_text

    video_note = ""
    if is_video:
        video_note = "\nThis is a video/reel/short — use the transcript heavily to understand the actual content."

    prompt = f"""You are an AI research curator. Analyze this content and return structured JSON only.
{video_note}

{context}

Return a JSON object with EXACTLY these keys:
- title: Meaningful title (max 80 chars). NOT a raw URL. Examples: "Claude 3.5 Haiku — Anthropic's fastest model", "Attention Is All You Need — transformer paper", "Why RAG beats fine-tuning for most use cases"
- summary: 2-3 sentence summary of what this is, why it matters, and key insights (max 300 chars)
- type: one of [Model, Tool, Research, Idea, News]
- relevance_score: integer 1-5 for AI relevance (5=core AI/ML, 4=AI-adjacent tool, 3=general tech, 2=loosely related, 1=tangential)
- actionable_brief: 1-2 sentence concrete next action for the reader. Examples: "Try integrating this into your RAG pipeline — especially the reranking step.", "Read Section 3 on context windows — directly applicable to your current prompt work.", "Share this with the team; the benchmark comparisons on page 2 are decision-relevant."

Rules:
- title must never be a raw URL
- GitHub repos: "RepoName — one-line description"
- Papers: "Paper Title (venue/year if known)"
- Videos/reels/shorts: use the actual video title or The main topic from transcript
- actionable_brief must be specific and useful, not generic ("explore this" is NOT acceptable)
- If transcript available, ground the actionable_brief in actual content from it

Return only valid JSON, no markdown fences, no explanation."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 500,
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
    """Heuristic fallback using fetched content."""
    title = fetched.get("fetched_title") or ""
    body = fetched.get("fetched_body") or ""
    transcript = fetched.get("transcript") or ""

    if title:
        title = re.split(r"\s*[·\-\|]\s*(GitHub|YouTube|Medium|arXiv|Twitter|Reddit|LinkedIn|HuggingFace|Instagram|TikTok)$", title)[0].strip()
        title = title[:80]

    if not title:
        lines = [l.strip() for l in user_text.splitlines() if l.strip() and not l.strip().startswith("http")]
        title = lines[0][:80] if lines else (url[:80] if url else "AI Finding")

    summary = (body or transcript or user_text.strip())[:300]

    combined = (title + " " + summary + " " + user_text).lower()
    type_ = "News"
    type_keywords = {
        "Research": ["paper", "arxiv", "research", "study", "findings", "published", "abstract", "journal"],
        "Model": ["model", "llm", "gpt", "claude", "gemini", "mistral", "weights", "checkpoint", "fine-tun"],
        "Tool": ["tool", "library", "sdk", "api", "release", "launch", "app", "plugin", "framework", "repo"],
        "Idea": ["idea", "thread", "thoughts", "opinion", "take", "essay", "blog", "perspective"],
    }
    for t, kws in type_keywords.items():
        if any(kw in combined for kw in kws):
            type_ = t
            break

    actionable_brief = "Review this content and assess relevance to your current work."
    return {"title": title, "summary": summary, "type": type_, "relevance_score": 3, "actionable_brief": actionable_brief}


# ── Notion API ─────────────────────────────────────────────────
��──────────────

async def create_notion_entry(title: str, url: str | None, source: str, type_: str,
                               summary: str, relevance: int, actionable_brief: str,
                               transcript_snippet: str = "") -> str | None:
    """Create a Notion page with properties + rich body content."""
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

    # Build page body with actionable brief + transcript snippet
    children = []

    if actionable_brief:
        children.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": f"⚡ {actionable_brief}"}}],
                "icon": {"emoji": "⚡"},
                "color": "yellow_background"
            }
        })

    if transcript_snippet:
        children.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": "📝 Transcript / Captions"}}],
                "children": [{
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": transcript_snippet[:1800]}}]
                    }
                }]
            }
        })

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }
    if children:
        payload["children"] = children

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
        "Share any link about AI and I'll:\n"
        "• Fetch the real content (title, description, GitHub stars, arXiv abstract)\n"
        "• For YouTube videos/Shorts — extract audio captions/transcript\n"
        "• For Instagram Reels — parse caption and context\n"
        "• Generate a meaningful title, summary, and *actionable brief*\n"
        "• Save everything to your Notion AI Signals database\n\n"
        "Works with: GitHub, arXiv, YouTube, YouTube Shorts, Instagram Reels, "
        "Reddit, Medium, Substack, HuggingFace, and any URL.\n\n"
        "Just paste or forward anything here.",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    text = ""
    if msg.text:
        text = msg.text
    elif msg.caption:
        text = msg.caption
    elif msg.forward_origin:
        text = "[Forwarded message with no text]"

    if not text.strip():
        await msg.reply_text("⚠️ I couldn't find any text. Try forwarding or pasting a link.")
        return

    url = extract_url(text)
    source = detect_platform(url)
    video = is_short_video(url)

    status_msg = await msg.reply_text("🔍 Fetching content...")

    # Step 1: fetch content
    fetched = {}
    if url:
        label = "🎬 Fetching video + transcript..." if video else "🔍 Fetching content from URL..."
        await status_msg.edit_text(label)
        fetched = await fetch_url_content(url)
        has_transcript = bool(fetched.get("transcript"))
        logger.info(f"Fetched for {url}: title={fetched.get('fetched_title','')[:60]} transcript={'yes' if has_transcript else 'no'}")

    await status_msg.edit_text("🧠 Analyzing...")

    # Step 2: extract metadata + actionable brief
    metadata = {}
    if ANTHROPIC_API_KEY:
        metadata = await extract_with_claude(text, url, fetched, video)

    if not metadata or not metadata.get("title"):
        metadata = build_metadata_from_fetched(text, url, fetched)

    title = metadata.get("title") or "AI Finding"
    summary = metadata.get("summary") or ""
    type_ = metadata.get("type") or "News"
    relevance = metadata.get("relevance_score") or 3
    actionable_brief = metadata.get("actionable_brief") or ""

    valid_types = ["Model", "Tool", "Research", "Idea", "News"]
    if type_ not in valid_types:
        type_ = "News"
    try:
        relevance = max(1, min(5, int(relevance)))
    except (TypeError, ValueError):
        relevance = 3

    transcript_snippet = fetched.get("transcript", "")

    # Step 3: save to Notion
    await status_msg.edit_text("💾 Saving to Notion...")
    notion_url = await create_notion_entry(
        title, url, source, type_, summary, relevance, actionable_brief, transcript_snippet
    )

    if notion_url:
        brief_line = f"\n⚡ _{actionable_brief}_" if actionable_brief else ""
        transcript_note = " · 📝 transcript saved" if transcript_snippet else ""
        await status_msg.edit_text(
            f"✅ *Saved to AI Signals!*\n\n"
            f"📌 *{title}*\n"
            f"📝 {summary[:150]}{'…' if len(summary) > 150 else ''}"
            f"{brief_line}\n\n"
            f"🏷 {type_} · {source} · ⭐ {relevance}/5{transcript_note}\n\n"
            f"[View in Notion]({notion_url})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    else:
        await status_msg.edit_text(
            "❌ Failed to save to Notion. Check your token and integration access."
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
