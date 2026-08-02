import os
import re
import json
import uuid
import logging
import threading
import asyncio
import requests
from datetime import datetime
from collections import OrderedDict

from dotenv import load_dotenv
from flask import Flask, render_template, abort, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GAS_URL = os.getenv("GAS_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:10000")

ADMIN_IDS = os.getenv("ADMIN_IDS", "").split(",")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS if x.strip().isdigit()]

RESULTS_PER_PAGE = 5
SESSIONS_FILE = "user_sessions.json"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

USER_SESSIONS = {}

# ═══════════════════════════════════════════════
# Preview Storage (in-memory with expiry)
# ═══════════════════════════════════════════════
PREVIEW_STORE = OrderedDict()
MAX_PREVIEWS = 200

def store_preview(preview_data):
    """Store preview data and return unique ID"""
    preview_id = uuid.uuid4().hex[:12]
    
    # Clean old previews if too many
    while len(PREVIEW_STORE) >= MAX_PREVIEWS:
        PREVIEW_STORE.popitem(last=False)
    
    PREVIEW_STORE[preview_id] = {
        "data": preview_data,
        "created": datetime.now().isoformat()
    }
    return preview_id

def get_preview(preview_id):
    """Get preview data by ID"""
    entry = PREVIEW_STORE.get(preview_id)
    if entry:
        return entry["data"]
    return None

# ═══════════════════════════════════════════════
# Flask App (HTML Preview Server)
# ═══════════════════════════════════════════════
flask_app = Flask(__name__, template_folder="templates", static_folder="static")

@flask_app.route("/")
def index():
    return "🇰🇭 Cambodia Law Bot v17.5 - Running"

@flask_app.route("/health")
def health():
    return {"status": "ok", "version": "17.5", "previews": len(PREVIEW_STORE)}

@flask_app.route("/preview/<preview_id>")
def preview_page(preview_id):
    data = get_preview(preview_id)
    if not data:
        abort(404)
    return render_template("preview.html", **data)

# ═══════════════════════════════════════════════
# Callback Data Registry
# ═══════════════════════════════════════════════
CALLBACK_REGISTRY = {}
CALLBACK_COUNTER = 0

def register_callback_data(doc, article):
    global CALLBACK_COUNTER
    CALLBACK_COUNTER += 1
    short_id = f"cb{CALLBACK_COUNTER}"
    CALLBACK_REGISTRY[short_id] = {"doc": doc, "article": article}
    if len(CALLBACK_REGISTRY) > 500:
        keys = list(CALLBACK_REGISTRY.keys())[:100]
        for k in keys:
            del CALLBACK_REGISTRY[k]
    return short_id

def get_callback_data(short_id):
    return CALLBACK_REGISTRY.get(short_id, {})

# ═══════════════════════════════════════════════
# Session Persistence
# ═══════════════════════════════════════════════
def load_sessions():
    global USER_SESSIONS
    try:
        if os.path.exists(SESSIONS_FILE):
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                USER_SESSIONS = {int(k): v for k, v in data.items()}
            logger.info(f"✅ Loaded {len(USER_SESSIONS)} sessions")
    except Exception as e:
        logger.error(f"❌ Load sessions error: {e}")
        USER_SESSIONS = {}

def save_sessions():
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            data = {str(k): v for k, v in USER_SESSIONS.items()}
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ Save sessions error: {e}")

def update_session(user_id, session_data):
    USER_SESSIONS[user_id] = session_data
    save_sessions()

# ═══════════════════════════════════════════════
# Category Detection
# ═══════════════════════════════════════════════
def get_law_category(doc_name):
    if not doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "📄", "label": "ច្បាប់"}
    if "នីតិវិធីព្រហ្មទណ្ឌ" in doc_name:
        return {"category": "criminal", "emoji": "🔴", "icon": "👮", "label": "នីតិវិធីព្រហ្មទណ្ឌ"}
    if "ព្រហ្មទណ្ឌ" in doc_name or "ព្រហ្មទណ្ឍ" in doc_name:
        return {"category": "criminal", "emoji": "🔴", "icon": "⚖️", "label": "ព្រហ្មទណ្ឌ"}
    if "នីតិវិធីរដ្ឋប្បវេណី" in doc_name:
        return {"category": "civil", "emoji": "🔵", "icon": "⚖️", "label": "នីតិវិធីរដ្ឋប្បវេណី"}
    if "រដ្ឋប្បវេណី" in doc_name:
        return {"category": "civil", "emoji": "🔵", "icon": "📜", "label": "រដ្ឋប្បវេណី"}
    if "ការងារ" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "💼", "label": "ការងារ"}
    if "គ្រួសារ" in doc_name or "អាពាហ៍" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "👨‍👩‍👧", "label": "គ្រួសារ"}
    if "ចរាចរណ៍" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "🚗", "label": "ចរាចរណ៍"}
    if "ពាណិជ្ជកម្ម" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "💰", "label": "ពាណិជ្ជកម្ម"}
    if "ដីធ្លី" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "🏞️", "label": "ដីធ្លី"}
    if "បរិស្ថាន" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "🌳", "label": "បរិស្ថាន"}
    if "អនីតិជន" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "👶", "label": "អនីតិជន"}
    return {"category": "other", "emoji": "🟢", "icon": "📄", "label": doc_name}

def group_results_by_document(results):
    groups = {}
    for r in results:
        doc = r.get("document", "Unknown")
        if doc not in groups:
            groups[doc] = []
        groups[doc].append(r)
    return groups

# ═══════════════════════════════════════════════
# Document Priority Sort
# ═══════════════════════════════════════════════
def get_doc_priority(doc_name):
    if not doc_name:
        return 999
    if "នីតិវិធីព្រហ្មទណ្ឌ" in doc_name:
        return 2
    if "ព្រហ្មទណ្ឌ" in doc_name or "ព្រហ្មទណ្ឍ" in doc_name:
        return 1
    if "នីតិវិធីរដ្ឋប្បវេណី" in doc_name:
        return 4
    if "រដ្ឋប្បវេណី" in doc_name:
        return 3
    if doc_name.startswith("ក្រម"):
        return 5
    if "អនីតិជន" in doc_name:
        return 10
    if "ការងារ" in doc_name:
        return 11
    if "គ្រួសារ" in doc_name or "អាពាហ៍" in doc_name:
        return 12
    if "ដីធ្លី" in doc_name:
        return 13
    if "ចរាចរណ៍" in doc_name:
        return 14
    if "ពាណិជ្ជកម្ម" in doc_name:
        return 15
    if "បរិស្ថាន" in doc_name:
        return 16
    if doc_name.startswith("ច្បាប់"):
        return 50
    return 100

def sort_documents_by_priority(docs):
    def sort_key(doc):
        if isinstance(doc, str):
            name = doc
        elif isinstance(doc, dict):
            name = doc.get("name", "")
        else:
            name = str(doc)
        return (get_doc_priority(name), name)
    return sorted(docs, key=sort_key)

# ═══════════════════════════════════════════════
# API Calls
# ═══════════════════════════════════════════════
def call_gas(payload, timeout=90):
    try:
        logger.info(f"→ GAS: mode={payload.get('mode')}")
        response = requests.post(GAS_URL, json=payload, timeout=timeout, allow_redirects=True)
        logger.info(f"← Status: {response.status_code}")
        if response.status_code != 200:
            return {"success": False, "error": f"HTTP {response.status_code}"}
        return response.json()
    except Exception as e:
        logger.error(f"GAS Error: {e}")
        return {"success": False, "error": str(e)}

def search_law(query, user_id="anonymous", use_ai=True):
    return call_gas({
        "mode": "search", "query": query,
        "user_id": str(user_id), "use_ai": use_ai
    })

def find_article(article_num, doc_name=None):
    payload = {"mode": "article", "article": article_num}
    if doc_name:
        payload["doc"] = doc_name
    return call_gas(payload)

def list_docs():
    return call_gas({"mode": "list"})

def record_feedback(user_id, query, document, article, action="click"):
    try:
        return call_gas({
            "mode": "feedback", "user_id": str(user_id), "query": query,
            "document": document, "article": article, "action": action
        }, timeout=15)
    except Exception as e:
        logger.error(f"Feedback error: {e}")
        return {"success": False}

def get_suggestions(partial_query):
    try:
        return call_gas({"mode": "suggestions", "query": partial_query}, timeout=15)
    except:
        return {"success": False}

def get_popular_articles(limit=10):
    try:
        return call_gas({"mode": "popular", "limit": limit}, timeout=15)
    except:
        return {"success": False}

def get_analytics(days=7):
    try:
        return call_gas({"mode": "analytics", "days": days}, timeout=30)
    except:
        return {"success": False}

# ═══════════════════════════════════════════════
# Sort & Paginate
# ═══════════════════════════════════════════════
def khmer_to_arabic_num(s):
    m = {"០":"0","១":"1","២":"2","៣":"3","៤":"4","៥":"5","៦":"6","៧":"7","៨":"8","៩":"9"}
    return "".join(m.get(c, c) for c in str(s))

def sort_results_by_article(results):
    def key_fn(r):
        doc = r.get("document", "")
        article = r.get("article", "")
        if not article:
            return (get_doc_priority(doc), doc, 999999)
        arabic = khmer_to_arabic_num(article)
        match = re.search(r'\d+', arabic)
        return (get_doc_priority(doc), doc, int(match.group()) if match else 999999)
    return sorted(results, key=key_fn)

def paginate_results(results, page=0, per_page=RESULTS_PER_PAGE):
    total = len(results)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = page * per_page
    end = start + per_page
    return {
        "results": results[start:end], "total": total,
        "total_pages": total_pages, "current_page": page + 1,
        "has_next": (page + 1) < total_pages, "has_prev": page > 0,
        "start_idx": start + 1, "end_idx": min(end, total)
    }

# ═══════════════════════════════════════════════
# Text Helpers
# ═══════════════════════════════════════════════
def escape_html(text):
    if not text:
        return ""
    text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_content(content):
    if not content:
        return ""
    content = content.replace("**", "").replace("__", "").replace("##", "")
    content = re.sub(r'\n{3,}', '\n\n', content)
    content = re.sub(r'^\s+', '', content, flags=re.MULTILINE)
    content = re.sub(r'[ \t]{2,}', ' ', content)
    return content.strip()

def _split_title_and_body(content, article_num):
    if not content:
        return ("", "")
    lines = content.split("\n")
    title = ""
    body_start_idx = 0
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        match = re.match(r'^មាត្រា\s*[០-៩\d]+\s*[.។\-–—]+\s*(.+)$', line)
        if match:
            title = match.group(1).strip()
            title = re.sub(r'[។\.\-–—]+$', '', title).strip()
            body_start_idx = idx + 1
            break
        else:
            title = line
            title = re.sub(r'^មាត្រា\s*[០-៩\d]+\s*[.។\-–—]\s*', '', title).strip()
            body_start_idx = idx + 1
            break
    body = "\n".join(lines[body_start_idx:]).strip()
    body = re.sub(r'\n{2,}', '\n', body)
    return (title, body)

def _split_into_paragraphs(text, max_sentences_per_para=3):
    if not text:
        return []
    sentences = re.split(r'(?<=។)\s*', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return [text]
    paragraphs = []
    current = []
    for sent in sentences:
        current.append(sent)
        total_len = sum(len(s) for s in current)
        if len(current) >= max_sentences_per_para or total_len > 350:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs

def format_body_paragraphs(body, indent="  "):
    if not body:
        return ""
    lines = body.split("\n")
    formatted_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        is_subheader = bool(re.match(
            r'^(ជំពូកទី|ផ្នែកទី|មាត្រា|វិភាគទី|ផ្នែក|ទី\s*[០-៩\d])', line
        ))
        if is_subheader:
            formatted_lines.append(f"<b>▸ {escape_html(line)}</b>")
        else:
            paragraphs = _split_into_paragraphs(line)
            for para in paragraphs:
                if para.strip():
                    formatted_lines.append(f"{indent}{escape_html(para.strip())}")
    return "\n".join(formatted_lines)

def highlight_keywords_html(text, keywords):
    if not keywords or not text:
        return text
    for kw in keywords:
        if not kw or len(kw) < 2:
            continue
        escaped_kw = escape_html(kw)
        pattern = re.compile(re.escape(escaped_kw), re.IGNORECASE)
        text = pattern.sub(f"<b><u>{escaped_kw}</u></b>", text)
    return text

def make_progress_bar(current, total, width=15):
    if total == 0:
        return ""
    filled = int((current / total) * width)
    bar = "▓" * filled + "░" * (width - filled)
    percent = int((current / total) * 100)
    return f"{bar} {percent}%"

# ═══════════════════════════════════════════════
# ⭐ v17.5: Build Preview Data for HTML
# ═══════════════════════════════════════════════
def build_preview_data(session):
    """Convert session results into HTML template data"""
    results = session.get("all_results", session.get("results", []))
    query = session.get("query", "")
    keywords = session.get("keywords", [])
    
    groups = group_results_by_document(results)
    sorted_doc_names = sort_documents_by_priority(list(groups.keys()))
    
    grouped_results = OrderedDict()
    doc_category = {}
    doc_emoji = {}
    
    for doc_name in sorted_doc_names:
        cat = get_law_category(doc_name)
        doc_category[doc_name] = cat["category"]
        doc_emoji[doc_name] = f"{cat['emoji']} {cat['icon']}"
        
        processed_articles = []
        for r in groups[doc_name]:
            article_num = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, body = _split_title_and_body(content, article_num)
            
            # ⭐ NEW: Better body formatting
            body_html = ""
            if body:
                body_html = format_body_html(body, keywords)
            
            processed_articles.append({
                "article": article_num,
                "title": title,
                "body": body_html
            })
        
        grouped_results[doc_name] = processed_articles
    
    now = datetime.now()
    
    return {
        "title": f"ស្វែងរក: {query}",
        "query": query,
        "total_articles": len(results),
        "total_docs": len(groups),
        "date": now.strftime("%d/%m/%Y"),
        "datetime_full": now.strftime("%d/%m/%Y %H:%M"),
        "grouped_results": grouped_results,
        "doc_category": doc_category,
        "doc_emoji": doc_emoji
    }


def format_body_html(body, keywords=None):
    """
    ⭐ NEW: Format legal text body with beautiful HTML
    Detects: numbered lists (១. ២. ៣.), sub-lists (ក. ខ. គ.), regular paragraphs
    """
    if not body:
        return ""
    
    # Clean up
    body = re.sub(r'\n{3,}', '\n\n', body)
    lines = body.split('\n')
    
    html_parts = []
    current_para = []
    
    for line in lines:
        line = line.strip()
        if not line:
            if current_para:
                html_parts.append(_process_paragraph(' '.join(current_para), keywords))
                current_para = []
            continue
        
        # Check for numbered points: ១- ២- ១. ២. 1. 2.
        num_match = re.match(r'^([០-៩\d]+)[\.\-–—។]\s*(.+)$', line)
        # Check for sub-points: ក- ក. ខ. គ.
        sub_match = re.match(r'^([ក-អ])[\.\-–—។]\s*(.+)$', line)
        
        if num_match:
            # Flush current paragraph
            if current_para:
                html_parts.append(_process_paragraph(' '.join(current_para), keywords))
                current_para = []
            
            num_label = num_match.group(1)
            text = num_match.group(2)
            processed_text = _highlight_text(text, keywords)
            html_parts.append(
                f'<p class="numbered">'
                f'<span class="num-label">{num_label}</span>'
                f'{processed_text}'
                f'</p>'
            )
        elif sub_match:
            if current_para:
                html_parts.append(_process_paragraph(' '.join(current_para), keywords))
                current_para = []
            
            sub_label = sub_match.group(1)
            text = sub_match.group(2)
            processed_text = _highlight_text(text, keywords)
            html_parts.append(
                f'<p class="sub-numbered">'
                f'<span class="sub-label">{sub_label}</span>'
                f'{processed_text}'
                f'</p>'
            )
        else:
            current_para.append(line)
    
    # Flush remaining
    if current_para:
        html_parts.append(_process_paragraph(' '.join(current_para), keywords))
    
    return ''.join(html_parts)


def _process_paragraph(text, keywords=None):
    """Process a regular paragraph"""
    if not text.strip():
        return ""
    text = _highlight_text(text, keywords)
    return f'<p>{text}</p>'


def _highlight_text(text, keywords=None):
    """Highlight keywords in text"""
    if not text:
        return ""
    
    # Escape HTML first
    text = escape_html(text)
    
    if not keywords:
        return text
    
    # Sort keywords by length (longest first) to avoid nested highlights
    sorted_keywords = sorted(keywords, key=len, reverse=True)
    
    for kw in sorted_keywords:
        if not kw or len(kw) < 2:
            continue
        escaped_kw = escape_html(kw)
        pattern = re.compile(re.escape(escaped_kw), re.IGNORECASE)
        text = pattern.sub(
            f'<span class="highlight">{escaped_kw}</span>',
            text
        )
    
    return text

# ═══════════════════════════════════════════════
# Format Modes (Telegram)
# ═══════════════════════════════════════════════
def format_preview_mode(data, session, pagination_info=None):
    results = data.get("results", [])
    if not results:
        return "🔍 រកមិនឃើញលទ្ធផល"

    all_results = session.get("all_results", results)
    all_groups = group_results_by_document(all_results)
    total_docs = len(all_groups)
    total_articles = len(all_results)
    page_groups = group_results_by_document(results)

    query = session.get("query", "")
    msg = f"🔍 <b>ស្វែងរក:</b> <code>{escape_html(query)}</code>\n"
    msg += f"📊 <b>{total_articles}</b> មាត្រា | <b>{total_docs}</b> ច្បាប់"
    if session.get("ai_reranked"):
        msg += " | 🤖 <i>AI</i>"
    msg += "\n"

    if pagination_info and pagination_info["total_pages"] > 1:
        bar = make_progress_bar(pagination_info["current_page"], pagination_info["total_pages"])
        msg += f"{bar}\n"

    msg += "━━━━━━━━━━━━━━━━━━━━\n"

    selected_docs = session.get("selected_docs", [])
    if selected_docs and selected_docs != ["all"]:
        msg += f"\n📂 <b>ស្វែងរកក្នុង:</b> {len(selected_docs)} ច្បាប់\n"

    filter_active = session.get("filter", "all")
    if filter_active == "all" and total_docs > 1:
        msg += "\n📚 <b>ច្បាប់ទាំងអស់:</b>\n"
        sorted_doc_names = sort_documents_by_priority(list(all_groups.keys()))
        for doc_name in sorted_doc_names:
            doc_results = all_groups[doc_name]
            cat = get_law_category(doc_name)
            in_page = "👁" if doc_name in page_groups else ""
            msg += f"  {cat['emoji']} {escape_html(doc_name)} ({len(doc_results)}) {in_page}\n"

    sorted_page_docs = sort_documents_by_priority(list(page_groups.keys()))
    for doc_name in sorted_page_docs:
        doc_results = page_groups[doc_name]
        cat = get_law_category(doc_name)
        msg += f"\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b>\n"
        for r in doc_results:
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, _ = _split_title_and_body(content, article)
            
            ai_badge = ""
            if r.get("ai_rank") and r["ai_rank"] <= 3:
                ai_badge = f" 🤖#{r['ai_rank']}"
            
            fb_boost = r.get("feedback_boost", 1.0)
            popular_badge = " 🔥" if fb_boost >= 1.3 else ""
            
            if title:
                if len(title) > 40:
                    title = title[:37] + "..."
                msg += f"  ├ 📌 <b>មាត្រា {escape_html(str(article))}</b>{ai_badge}{popular_badge} - {escape_html(title)}\n"
            else:
                msg += f"  ├ 📌 <b>មាត្រា {escape_html(str(article))}</b>{ai_badge}{popular_badge}\n"

    msg += "\n👆 <i>ចុចប៊ូតុងខាងក្រោមដើម្បីមើលពេញ</i>"
    return msg

def format_detailed_mode(data, session, pagination_info=None):
    results = data.get("results", [])
    keywords = session.get("keywords", [])
    if not results:
        return "🔍 រកមិនឃើញលទ្ធផល"

    all_results = session.get("all_results", results)
    all_groups = group_results_by_document(all_results)
    total_docs = len(all_groups)
    total_articles = len(all_results)

    msg = f"🔍 <b>ស្វែងរក:</b> <code>{escape_html(session.get('query', ''))}</code>\n"
    msg += f"📊 <b>{total_articles}</b> មាត្រា | <b>{total_docs}</b> ច្បាប់"
    if session.get("ai_reranked"):
        msg += " | 🤖"
    if pagination_info:
        msg += f" | 📄 <b>{pagination_info['current_page']}/{pagination_info['total_pages']}</b>"
    msg += "\n━━━━━━━━━━━━━━━━━━━━"

    selected_docs = session.get("selected_docs", [])
    if selected_docs and selected_docs != ["all"]:
        msg += f"\n📂 <b>ស្វែងរកក្នុង:</b> {len(selected_docs)} ច្បាប់"

    page_groups = group_results_by_document(results)
    sorted_page_docs = sort_documents_by_priority(list(page_groups.keys()))

    for doc_idx, doc_name in enumerate(sorted_page_docs):
        doc_results = page_groups[doc_name]
        cat = get_law_category(doc_name)
        total_in_doc = len(all_groups.get(doc_name, []))
        current_in_page = len(doc_results)

        if total_in_doc > current_in_page:
            msg += f"\n\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b> ({current_in_page}/{total_in_doc})"
        else:
            msg += f"\n\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b> ({total_in_doc})"

        for r_idx, r in enumerate(doc_results):
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, body = _split_title_and_body(content, article)

            ai_badge = ""
            if r.get("ai_rank") and r["ai_rank"] <= 3:
                ai_badge = f" 🤖#{r['ai_rank']}"
            
            fb_boost = r.get("feedback_boost", 1.0)
            popular_badge = " 🔥" if fb_boost >= 1.3 else ""

            if article and title:
                msg += f"\n\n📌 <b>មាត្រា {escape_html(str(article))}{ai_badge}{popular_badge} - {escape_html(title)}</b>"
            elif article:
                msg += f"\n\n📌 <b>មាត្រា {escape_html(str(article))}{ai_badge}{popular_badge}</b>"

            if body:
                formatted_body = format_body_paragraphs(body, indent="    ")
                if keywords:
                    formatted_body = highlight_keywords_html(formatted_body, keywords)
                msg += f"\n{formatted_body}"

            if r_idx < len(doc_results) - 1:
                msg += "\n▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️"

        if doc_idx < len(sorted_page_docs) - 1:
            msg += "\n━━━━━━━━━━━━━━━━━━━━"

    return msg

# ═══════════════════════════════════════════════
# Document Selection Keyboard
# ═══════════════════════════════════════════════
def build_doc_selection_keyboard(available_docs, selected_docs=None, search_type="search"):
    if selected_docs is None:
        selected_docs = []
    buttons = []

    # ⭐ Header - Better styling
    if len(selected_docs) == len(available_docs) and len(available_docs) > 0:
        buttons.append([
            InlineKeyboardButton(
                "☑️  ដកជម្រើសទាំងអស់",
                callback_data="docsel:none"
            )
        ])
    else:
        buttons.append([
            InlineKeyboardButton(
                "📋  ជ្រើសរើសទាំងអស់",
                callback_data="docsel:all"
            )
        ])

    # Separator (visual break)
    buttons.append([
        InlineKeyboardButton(
            "━━━━━━━━━━━━━━",
            callback_data="docsel:separator"
        )
    ])

    # ⭐ Documents with BETTER numbers + emojis
    khmer_numbers = ["១", "២", "៣", "៤", "៥", "៦", "៧", "៨", "៩", "១០",
                     "១១", "១២", "១៣", "១៤", "១៥", "១៦", "១៧", "១៨", "១៩", "២០"]
    
    for idx, doc_name in enumerate(available_docs):
        is_selected = doc_name in selected_docs
        check = "✅" if is_selected else "⬜"
        num = khmer_numbers[idx] if idx < len(khmer_numbers) else str(idx + 1)
        
        # ⭐ Get category emoji
        cat = get_law_category(doc_name)
        cat_emoji = cat["emoji"]
        
        # ⭐ Shorten long names for better display
        display_name = doc_name
        if len(display_name) > 30:
            display_name = display_name[:27] + "..."
        
        # ⭐ Format: [✅] ១ • 🔴 ក្រមព្រហ្មទណ្ឌ២០០៩
        button_text = f"{check}  {num}  {cat_emoji}  {display_name}"
        
        buttons.append([
            InlineKeyboardButton(
                button_text,
                callback_data=f"docsel:toggle:{idx}"
            )
        ])

    # Separator
    buttons.append([
        InlineKeyboardButton(
            "━━━━━━━━━━━━━━",
            callback_data="docsel:separator"
        )
    ])

    # ⭐ Actions with better labels
    action_row = []
    if selected_docs:
        action_row.append(
            InlineKeyboardButton(
                f"🔍  ស្វែងរក  ({len(selected_docs)})",
                callback_data="docsel:confirm"
            )
        )
    else:
        action_row.append(
            InlineKeyboardButton(
                "⚠️  សូមជ្រើសរើសច្បាប់",
                callback_data="docsel:warn"
            )
        )
    
    buttons.append(action_row)
    
    # ⭐ Secondary actions
    buttons.append([
        InlineKeyboardButton(
            "📚  ស្វែងរកទាំងអស់",
            callback_data="docsel:skip"
        )
    ])
    
    buttons.append([
        InlineKeyboardButton(
            "❌  បោះបង់",
            callback_data="docsel:cancel"
        )
    ])

    return InlineKeyboardMarkup(buttons)

# ═══════════════════════════════════════════════
# ⭐ v17.5: Navigation Keyboard (+ Preview button)
# ═══════════════════════════════════════════════
def build_navigation_keyboard(session):
    pagination = paginate_results(session["results"], session["page"])
    buttons = []

    # Row 1: Navigation
    nav_row = []
    if pagination["has_prev"]:
        nav_row.append(InlineKeyboardButton("⬅️ ថយ", callback_data="nav:prev"))
    nav_row.append(InlineKeyboardButton(
        f"📄 {pagination['current_page']}/{pagination['total_pages']}",
        callback_data="nav:info"
    ))
    if pagination["has_next"]:
        nav_row.append(InlineKeyboardButton("បន្ត ➡️", callback_data="nav:next"))
    buttons.append(nav_row)

    # Row 2: View Mode + Preview
    mode = session.get("view_mode", "preview")
    view_row = []
    if mode == "preview":
        view_row.append(InlineKeyboardButton("👁 មើលពេញ", callback_data="mode:detailed"))
    else:
        view_row.append(InlineKeyboardButton("📋 មើលសង្ខេប", callback_data="mode:preview"))
    
    # ⭐ NEW: HTML Preview / PDF button
    view_row.append(InlineKeyboardButton("🖨️ Preview/PDF", callback_data="action:preview_pdf"))
    buttons.append(view_row)

    # Filter
    results = session.get("all_results", session["results"])
    categories = set()
    for r in results:
        cat = get_law_category(r.get("document", ""))
        categories.add(cat["category"])

    if len(categories) > 1:
        filter_row = [InlineKeyboardButton("🔍 ទាំងអស់", callback_data="filter:all")]
        for cat in categories:
            if cat == "criminal":
                filter_row.append(InlineKeyboardButton("🔴 ព្រហ្មទណ្ឌ", callback_data="filter:criminal"))
            elif cat == "civil":
                filter_row.append(InlineKeyboardButton("🔵 រដ្ឋប្បវេណី", callback_data="filter:civil"))
            else:
                filter_row.append(InlineKeyboardButton("🟢 ផ្សេងៗ", callback_data="filter:other"))
        buttons.append(filter_row)

    # Actions
    buttons.append([
        InlineKeyboardButton("📂 ប្តូរច្បាប់", callback_data="action:reselect_docs"),
        InlineKeyboardButton("🔍 ថ្មី", callback_data="action:new_search"),
        InlineKeyboardButton("❌ បិទ", callback_data="action:close")
    ])

    return InlineKeyboardMarkup(buttons)

def build_start_keyboard():
    buttons = [
        [InlineKeyboardButton("⚖️ ការលួច", callback_data="quick:លួច"),
         InlineKeyboardButton("💰 ការក្លែងបន្លំ", callback_data="quick:ក្លែងបន្លំ")],
        [InlineKeyboardButton("👨‍👩‍👧 គ្រួសារ", callback_data="quick:គ្រួសារ"),
         InlineKeyboardButton("💼 កិច្ចសន្យា", callback_data="quick:កិច្ចសន្យា")],
        [InlineKeyboardButton("🏞️ ដីធ្លី", callback_data="quick:ដីធ្លី"),
         InlineKeyboardButton("🚗 ចរាចរណ៍", callback_data="quick:ចរាចរណ៍")],
        [InlineKeyboardButton("🔥 ពេញនិយម", callback_data="action:popular"),
         InlineKeyboardButton("📚 ឯកសារ", callback_data="action:docs")],
        [InlineKeyboardButton("❓ ជំនួយ", callback_data="action:help")]
    ]
    return InlineKeyboardMarkup(buttons)

# ═══════════════════════════════════════════════
# Send Helpers
# ═══════════════════════════════════════════════
def smart_split_html(text, max_length=3800):
    if len(text) <= max_length:
        return [text]
    separators = ["━━━━━━━━━━━━━━━━━━━━\n", "\n\n📌 ", "\n\n", "\n"]
    for sep in separators:
        if sep in text:
            parts = []
            sections = text.split(sep)
            current = ""
            for i, section in enumerate(sections):
                s = section + sep if i < len(sections) - 1 else section
                if len(current) + len(s) <= max_length:
                    current += s
                else:
                    if current:
                        parts.append(current.rstrip())
                    current = s
            if current:
                parts.append(current.rstrip())
            if all(len(p) <= max_length for p in parts):
                return parts
    return [text[i:i+max_length] for i in range(0, len(text), max_length)]

async def send_results_to_chat(context_or_bot, chat_id, session, user_id=None):
    try:
        pagination = paginate_results(session["results"], session["page"])
        page_data = {"success": True, "results": pagination["results"]}
        view_mode = session.get("view_mode", "preview")

        if user_id and view_mode == "detailed":
            for r in pagination["results"]:
                doc = r.get("document", "")
                article = r.get("article", "")
                if doc and article:
                    threading.Thread(
                        target=record_feedback,
                        args=(user_id, session.get("query", ""), doc, article, "view"),
                        daemon=True
                    ).start()

        if view_mode == "preview":
            text = format_preview_mode(page_data, session, pagination)
        else:
            text = format_detailed_mode(page_data, session, pagination)

        keyboard = build_navigation_keyboard(session)
        parts = smart_split_html(text, 3800)

        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            kb = keyboard if is_last else None
            prefix = f"📄 <i>(ភាគ {i+1}/{len(parts)})</i>\n\n" if len(parts) > 1 else ""
            await context_or_bot.send_message(
                chat_id=chat_id, text=prefix + part,
                parse_mode=ParseMode.HTML, reply_markup=kb
            )
            if not is_last:
                await asyncio.sleep(0.3)
    except Exception as e:
        logger.error(f"❌ send_results_to_chat error: {e}", exc_info=True)

async def send_results_callback(update, session):
    try:
        user_id = update.effective_user.id
        pagination = paginate_results(session["results"], session["page"])
        page_data = {"success": True, "results": pagination["results"]}
        view_mode = session.get("view_mode", "preview")

        if view_mode == "detailed":
            for r in pagination["results"]:
                doc = r.get("document", "")
                article = r.get("article", "")
                if doc and article:
                    threading.Thread(
                        target=record_feedback,
                        args=(user_id, session.get("query", ""), doc, article, "view"),
                        daemon=True
                    ).start()

        if view_mode == "preview":
            text = format_preview_mode(page_data, session, pagination)
        else:
            text = format_detailed_mode(page_data, session, pagination)

        keyboard = build_navigation_keyboard(session)
        parts = smart_split_html(text, 3800)

        if len(parts) == 1:
            try:
                await update.callback_query.edit_message_text(
                    parts[0], parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
            except Exception as e:
                logger.warning(f"Edit failed: {e}")
                await update.effective_chat.send_message(
                    parts[0], parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
        else:
            for i, part in enumerate(parts):
                is_last = (i == len(parts) - 1)
                kb = keyboard if is_last else None
                prefix = f"📄 <i>(ភាគ {i+1}/{len(parts)})</i>\n\n" if len(parts) > 1 else ""
                if i == 0:
                    try:
                        await update.callback_query.edit_message_text(
                            prefix + part, parse_mode=ParseMode.HTML, reply_markup=kb
                        )
                    except:
                        await update.effective_chat.send_message(
                            prefix + part, parse_mode=ParseMode.HTML, reply_markup=kb
                        )
                else:
                    await update.effective_chat.send_message(
                        prefix + part, parse_mode=ParseMode.HTML, reply_markup=kb
                    )
                if not is_last:
                    await asyncio.sleep(0.3)
    except Exception as e:
        logger.error(f"❌ send_results_callback error: {e}", exc_info=True)

async def try_recover_session(update, is_callback=True):
    target = update.callback_query.message if is_callback else update.message
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="action:home")]])
    await target.reply_text(
        "⚠️ <b>Session បាត់</b>\n\nសូមស្វែងរកម្តងទៀត។",
        parse_mode=ParseMode.HTML, reply_markup=keyboard
    )

# ═══════════════════════════════════════════════
# Document Selection Flow
# ═══════════════════════════════════════════════
async def start_doc_selection(update, context, query, search_type="search", article_num=None):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    loading_text = f"📂 កំពុងទាញបញ្ជីច្បាប់សម្រាប់មាត្រា {escape_html(str(article_num))}..." if search_type == "article" else "📂 កំពុងទាញបញ្ជីច្បាប់..."
    loading_msg = await context.bot.send_message(chat_id=chat_id, text=loading_text, parse_mode=ParseMode.HTML)
    docs_data = list_docs()
    try:
        await loading_msg.delete()
    except:
        pass

    if not docs_data.get("success") or not docs_data.get("documents"):
        if search_type == "article":
            await execute_article_search(update, context, article_num, doc_filter=None)
        else:
            await execute_keyword_search(update, context, query, doc_filter=None)
        return

    available_docs = [d["name"] for d in docs_data.get("documents", [])]
    available_docs = sort_documents_by_priority(available_docs)

    session_data = {
        "pending_query": query, "pending_article": article_num,
        "search_type": search_type, "available_docs": available_docs,
        "selected_docs": [], "mode": "doc_selection", "state": "selecting"
    }
    update_session(user_id, session_data)

    display_query = f"មាត្រា {article_num}" if search_type == "article" else query
    msg_text = build_doc_selection_message(display_query, available_docs, [], search_type)
    keyboard = build_doc_selection_keyboard(available_docs, [], search_type)

    await context.bot.send_message(chat_id=chat_id, text=msg_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def execute_keyword_search(update, context, query, doc_filter=None):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔍 កំពុងស្វែងរក... 🤖")
    data = search_law(query, user_id=user_id, use_ai=True)
    total = data.get("count", 0)

    if not data.get("success") or total == 0:
        try:
            await status_msg.delete()
        except:
            pass
        suggestions_data = get_suggestions(query)
        suggestions_text = ""
        if suggestions_data.get("success") and suggestions_data.get("suggestions"):
            suggs = suggestions_data["suggestions"][:5]
            if suggs:
                suggestions_text = "\n\n💡 <b>សាកសំណួរស្រដៀង:</b>\n"
                for s in suggs:
                    suggestions_text += f"  • <code>{escape_html(s['query'])}</code>\n"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់ <b>{escape_html(query)}</b>{suggestions_text}",
            parse_mode=ParseMode.HTML
        )
        return

    all_results = sort_results_by_article(data.get("results", []))
    if doc_filter and doc_filter != ["all"]:
        filtered_results = [r for r in all_results if r.get("document", "") in doc_filter]
    else:
        filtered_results = all_results
        doc_filter = ["all"]

    if not filtered_results:
        try:
            await status_msg.delete()
        except:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔍 រកមិនឃើញលទ្ធផលក្នុងច្បាប់ដែលបានជ្រើសរើស",
            parse_mode=ParseMode.HTML
        )
        return

    session_data = {
        "results": filtered_results, "all_results": filtered_results,
        "page": 0, "query": query, "mode": "search", "view_mode": "preview",
        "keywords": data.get("keywords", []), "filter": "all",
        "selected_docs": doc_filter, "original_query": query,
        "search_type": "search", "ai_reranked": data.get("ai_reranked", False)
    }
    update_session(user_id, session_data)

    try:
        await status_msg.delete()
    except:
        pass

    await send_results_to_chat(context.bot, chat_id, USER_SESSIONS[user_id], user_id=user_id)

async def execute_article_search(update, context, article_num, doc_filter=None):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔍 កំពុងស្វែងរកមាត្រា <b>{escape_html(str(article_num))}</b>...",
        parse_mode=ParseMode.HTML
    )

    doc_name_for_api = None
    if doc_filter and doc_filter != ["all"] and len(doc_filter) == 1:
        doc_name_for_api = doc_filter[0]

    data = find_article(article_num, doc_name_for_api)

    if not data.get("success") or not data.get("results"):
        try:
            await status_msg.delete()
        except:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 រកមិនឃើញមាត្រា <b>{escape_html(str(article_num))}</b>",
            parse_mode=ParseMode.HTML
        )
        return

    all_results = sort_results_by_article(data.get("results", []))
    if doc_filter and doc_filter != ["all"] and len(doc_filter) > 1:
        filtered_results = [r for r in all_results if r.get("document", "") in doc_filter]
    else:
        filtered_results = all_results

    if not filtered_results:
        try:
            await status_msg.delete()
        except:
            pass
        return

    if not doc_filter:
        doc_filter = ["all"]

    for r in filtered_results[:3]:
        doc = r.get("document", "")
        art = r.get("article", "")
        if doc and art:
            threading.Thread(
                target=record_feedback,
                args=(user_id, f"មាត្រា {article_num}", doc, art, "click"),
                daemon=True
            ).start()

    session_data = {
        "results": filtered_results, "all_results": filtered_results,
        "page": 0, "query": f"មាត្រា {article_num}", "mode": "article",
        "view_mode": "detailed", "keywords": [], "filter": "all",
        "selected_docs": doc_filter, "original_query": f"មាត្រា {article_num}",
        "original_article": str(article_num), "search_type": "article",
        "ai_reranked": False
    }
    update_session(user_id, session_data)

    try:
        await status_msg.delete()
    except:
        pass

    await send_results_to_chat(context.bot, chat_id, USER_SESSIONS[user_id], user_id=user_id)

# ═══════════════════════════════════════════════
# Popular & Analytics
# ═══════════════════════════════════════════════
async def show_popular_articles(context, chat_id):
    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔥 កំពុងទាញ...")
    data = get_popular_articles(limit=10)
    try:
        await status_msg.delete()
    except:
        pass
    
    if not data.get("success") or not data.get("articles"):
        await context.bot.send_message(
            chat_id=chat_id,
            text="ℹ️ មិនទាន់មានទិន្នន័យទេ។\n💡 សូមស្វែងរកនិងចុចមាត្រា"
        )
        return
    
    articles = data.get("articles", [])
    msg = "🔥 <b>មាត្រាពេញនិយម TOP 10</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    buttons = []
    for idx, a in enumerate(articles, 1):
        doc = a.get("document", "")
        art = a.get("article", "")
        clicks = a.get("clicks", 0)
        cat = get_law_category(doc)
        
        rank_emoji = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"#{idx}"
        msg += f"{rank_emoji} {cat['emoji']} <b>មាត្រា {escape_html(art)}</b>\n"
        msg += f"    📖 <i>{escape_html(doc)}</i>\n"
        msg += f"    👁 <i>{int(clicks)} ការមើល</i>\n\n"
        
        if idx <= 5:
            short_id = register_callback_data(doc, art)
            buttons.append([InlineKeyboardButton(f"📖 មាត្រា {art}", callback_data=f"viewart:{short_id}")])
    
    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def show_analytics(context, chat_id, user_id, days=7):
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Admin only")
        return
    
    status_msg = await context.bot.send_message(chat_id=chat_id, text="📊 កំពុងទាញ...")
    data = get_analytics(days=days)
    try:
        await status_msg.delete()
    except:
        pass
    
    if not data.get("success"):
        await context.bot.send_message(chat_id=chat_id, text=f"❌ {data.get('error', 'Error')}")
        return
    
    msg = f"📊 <b>Analytics Dashboard</b>\n⏰ <i>{days} ថ្ងៃ</i>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"🔍 <b>ការស្វែងរក:</b> {data.get('total_searches', 0)}\n"
    msg += f"👥 <b>Users:</b> {data.get('unique_users', 0)}\n"
    msg += f"👆 <b>Clicks:</b> {data.get('total_clicks', 0)}\n\n"
    
    top_queries = data.get("top_queries", [])
    if top_queries:
        msg += "🔥 <b>សំណួរពេញនិយម:</b>\n"
        for i, q in enumerate(top_queries[:10], 1):
            msg += f"  {i}. <code>{escape_html(q['query'][:40])}</code> ({q['count']})\n"
        msg += "\n"
    
    top_docs = data.get("top_documents", [])
    if top_docs:
        msg += "📚 <b>ច្បាប់ពេញនិយម:</b>\n"
        for d in top_docs[:5]:
            cat = get_law_category(d['document'])
            msg += f"  {cat['emoji']} {escape_html(d['document'])} ({d['clicks']})\n"
    
    buttons = [[
        InlineKeyboardButton("📅 ១ថ្ងៃ", callback_data="stats:1"),
        InlineKeyboardButton("📅 ៧ថ្ងៃ", callback_data="stats:7"),
        InlineKeyboardButton("📅 ៣០ថ្ងៃ", callback_data="stats:30")
    ]]
    keyboard = InlineKeyboardMarkup(buttons)
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

# ═══════════════════════════════════════════════
# Process Queries
# ═══════════════════════════════════════════════
async def process_search_query(update, context, query):
    await start_doc_selection(update, context, query, search_type="search")

async def process_article_query(update, context, article_num, doc_name=None):
    if doc_name:
        await execute_article_search(update, context, article_num, doc_filter=[doc_name])
    else:
        await start_doc_selection(
            update, context, f"មាត្រា {article_num}",
            search_type="article", article_num=article_num
        )

# ═══════════════════════════════════════════════
# ⭐ v17.5: Callback Handler (+ Preview PDF)
# ═══════════════════════════════════════════════
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data
    chat_id = update.effective_chat.id

    logger.info(f"🔔 CALLBACK: '{data}' from user {user_id}")

    try:
        await query.answer()
    except Exception as e:
        logger.error(f"query.answer() failed: {e}")

    try:
        # ⭐ v17.5: Preview PDF Handler
        if data == "action:preview_pdf":
            session = USER_SESSIONS.get(user_id)
            if not session or not session.get("results"):
                await query.answer("⚠️ គ្មានលទ្ធផលដើម្បី preview", show_alert=True)
                return
            
            # Build preview data
            preview_data = build_preview_data(session)
            preview_id = store_preview(preview_data)
            
            # Build URL
            base_url = RENDER_EXTERNAL_URL.rstrip("/")
            preview_url = f"{base_url}/preview/{preview_id}"
            
            # Send link with instructions
            msg = (
                "🖨️ <b>HTML Preview / Save PDF</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔗 <a href=\"{preview_url}\">👉 ចុចទីនេះដើម្បីបើកមើល</a>\n\n"
                "📱 <b>របៀប Save PDF:</b>\n\n"
                "  🤖 <b>Android:</b>\n"
                "    ចុច ⋮ → Share → Print\n"
                "    → Save as PDF\n\n"
                "  🍎 <b>iOS:</b>\n"
                "    ចុច Share 🔗 → Print 🖨️\n"
                "    → Pinch out → Save PDF\n\n"
                "  💻 <b>Desktop:</b>\n"
                "    Ctrl+P → Save as PDF\n\n"
                f"📊 {preview_data['total_articles']} មាត្រា | "
                f"{preview_data['total_docs']} ច្បាប់\n"
                "⏰ <i>Link មានសុពលភាព ១ ម៉ោង</i>"
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 បើកមើល", url=preview_url)],
                [InlineKeyboardButton("⬅️ ត្រឡប់", callback_data="nav:info")]
            ])
            
            await context.bot.send_message(
                chat_id=chat_id, text=msg,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
                disable_web_page_preview=False
            )
            return

        # View Article (from popular)
        if data.startswith("viewart:"):
            short_id = data.split(":", 1)[1]
            cb_data = get_callback_data(short_id)
            doc = cb_data.get("doc")
            article = cb_data.get("article")
            if not doc or not article:
                await query.answer("⚠️ ទិន្នន័យបានផុតកំណត់", show_alert=True)
                return
            threading.Thread(
                target=record_feedback,
                args=(user_id, f"មាត្រា {article}", doc, article, "click"),
                daemon=True
            ).start()
            await execute_article_search(update, context, article, doc_filter=[doc])
            return

        # Stats
        if data.startswith("stats:"):
            days = int(data.split(":")[1])
            await show_analytics(context, chat_id, user_id, days=days)
            return

        # Document Selection
        if data.startswith("docsel:"):
            session = USER_SESSIONS.get(user_id)
            if not session or session.get("mode") != "doc_selection":
                await query.answer("⚠️ សូមចាប់ផ្ដើមស្វែងរកថ្មី", show_alert=True)
                return

            available_docs = session.get("available_docs", [])
            selected_docs = session.get("selected_docs", [])
            pending_query = session.get("pending_query", "")
            pending_article = session.get("pending_article", None)
            search_type = session.get("search_type", "search")
            display_query = f"មាត្រា {pending_article}" if search_type == "article" else pending_query

            if data == "docsel:all":
                selected_docs = list(available_docs)
                session["selected_docs"] = selected_docs
                update_session(user_id, session)
                await query.edit_message_text(
                    build_doc_selection_message(display_query, available_docs, selected_docs, search_type),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_doc_selection_keyboard(available_docs, selected_docs, search_type)
                )
            elif data == "docsel:none":
                selected_docs = []
                session["selected_docs"] = selected_docs
                update_session(user_id, session)
                await query.edit_message_text(
                    build_doc_selection_message(display_query, available_docs, selected_docs, search_type),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_doc_selection_keyboard(available_docs, selected_docs, search_type)
                )
            elif data.startswith("docsel:toggle:"):
                idx = int(data.split(":")[-1])
                if 0 <= idx < len(available_docs):
                    doc_name = available_docs[idx]
                    if doc_name in selected_docs:
                        selected_docs.remove(doc_name)
                    else:
                        selected_docs.append(doc_name)
                    session["selected_docs"] = selected_docs
                    update_session(user_id, session)
                    await query.edit_message_text(
                        build_doc_selection_message(display_query, available_docs, selected_docs, search_type),
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_doc_selection_keyboard(available_docs, selected_docs, search_type)
                    )
            elif data == "docsel:confirm":
                if not selected_docs:
                    await query.answer("⚠️ សូមជ្រើសរើសយ៉ាងតិច 1 ច្បាប់", show_alert=True)
                    return
                try:
                    await query.message.delete()
                except:
                    pass
                if search_type == "article" and pending_article:
                    await execute_article_search(update, context, pending_article, doc_filter=selected_docs)
                else:
                    await execute_keyword_search(update, context, pending_query, doc_filter=selected_docs)
            elif data == "docsel:skip":
                try:
                    await query.message.delete()
                except:
                    pass
                if search_type == "article" and pending_article:
                    await execute_article_search(update, context, pending_article, doc_filter=None)
                else:
                    await execute_keyword_search(update, context, pending_query, doc_filter=None)
            elif data == "docsel:cancel":
                try:
                    await query.message.delete()
                except:
                    pass
                USER_SESSIONS.pop(user_id, None)
                save_sessions()
            elif data == "docsel:warn":
                await query.answer("⚠️ សូមជ្រើសរើសយ៉ាងតិច 1 ច្បាប់", show_alert=True)
            return

        if data == "action:reselect_docs":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            search_type = session.get("search_type", "search")
            original_query = session.get("original_query", session.get("query", ""))
            original_article = session.get("original_article", None)
            if search_type == "article" and original_article:
                await start_doc_selection(update, context, original_query, search_type="article", article_num=original_article)
            else:
                await start_doc_selection(update, context, original_query, search_type="search")
            return

        # Navigation
        if data == "nav:next":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            pagination = paginate_results(session["results"], session["page"])
            if pagination["has_next"]:
                session["page"] += 1
                update_session(user_id, session)
                await send_results_callback(update, session)
            else:
                await query.answer("⚠️ ទំព័រចុងក្រោយ", show_alert=True)
        elif data == "nav:prev":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            if session["page"] > 0:
                session["page"] -= 1
                update_session(user_id, session)
                await send_results_callback(update, session)
            else:
                await query.answer("⚠️ ទំព័រទី 1", show_alert=True)
        elif data == "nav:info":
            session = USER_SESSIONS.get(user_id)
            if session:
                pagination = paginate_results(session["results"], session["page"])
                await query.answer(
                    f"📊 ទំព័រ {pagination['current_page']}/{pagination['total_pages']}\nសរុប: {pagination['total']}",
                    show_alert=True
                )

        elif data == "mode:detailed":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            session["view_mode"] = "detailed"
            session["page"] = 0
            update_session(user_id, session)
            pagination = paginate_results(session["results"], 0)
            for r in pagination["results"]:
                doc = r.get("document", "")
                article = r.get("article", "")
                if doc and article:
                    threading.Thread(
                        target=record_feedback,
                        args=(user_id, session.get("query", ""), doc, article, "click"),
                        daemon=True
                    ).start()
            await send_results_callback(update, session)

        elif data == "mode:preview":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            session["view_mode"] = "preview"
            session["page"] = 0
            update_session(user_id, session)
            await send_results_callback(update, session)

        elif data.startswith("filter:"):
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            cat = data.split(":", 1)[1]
            session["filter"] = cat
            if cat == "all":
                session["results"] = session["all_results"]
            else:
                session["results"] = [
                    r for r in session["all_results"]
                    if get_law_category(r.get("document", ""))["category"] == cat
                ]
            session["page"] = 0
            update_session(user_id, session)
            if session["results"]:
                await send_results_callback(update, session)
            else:
                await query.answer("⚠️ គ្មានលទ្ធផល", show_alert=True)
                session["results"] = session["all_results"]
                session["filter"] = "all"
                update_session(user_id, session)

        elif data.startswith("quick:"):
            search_term = data.split(":", 1)[1]
            await start_doc_selection(update, context, search_term, search_type="search")

        elif data == "action:new_search":
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔍 <b>សូមវាយសំណួរថ្មី</b>\n\nឧទាហរណ៍:\n  • <code>លួច</code>\n  • <code>មាត្រា ៥៥</code>",
                parse_mode=ParseMode.HTML
            )
        elif data == "action:close":
            try:
                await query.message.delete()
            except:
                pass
        elif data == "action:home":
            await start_from_callback(query)
        elif data == "action:popular":
            await show_popular_articles(context, chat_id)
        elif data == "action:help":
            msg = (
                "📖 <b>ជំនួយ Bot v17.5</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                "🔍 <code>លួច</code> → ស្វែងរក\n"
                "📌 <code>មាត្រា ៥៥</code> → រកមាត្រា\n"
                "📌 <code>មាត្រា ៥ ព្រហ្មទណ្ឌ</code> → រកផ្ទាល់\n\n"
                "🤖 <b>Features:</b>\n"
                "  🔥 មាត្រាពេញនិយម\n"
                "  🤖 AI តម្រៀបលទ្ធផល\n"
                "  🖨️ HTML Preview / Save PDF\n\n"
                "🎯 <b>Commands:</b>\n"
                "  /start /popular /docs /help /preview"
            )
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        elif data == "action:docs":
            status = await context.bot.send_message(chat_id=chat_id, text="📚 កំពុងទាញ...")
            docs_data = list_docs()
            try:
                await status.delete()
            except:
                pass
            if not docs_data.get("success"):
                await context.bot.send_message(chat_id=chat_id, text=f"❌ {docs_data.get('error')}")
                return
            docs = docs_data.get("documents", [])
            docs_sorted = sort_documents_by_priority(docs)
            msg = f"📚 <b>ឯកសារច្បាប់ទាំងអស់ ({len(docs_sorted)}):</b>\n"
            msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
            khmer_numbers = ["១", "២", "៣", "៤", "៥", "៦", "៧", "៨", "៩", "១០",
                             "១១", "១២", "១៣", "១៤", "១៥", "១៦", "១៧", "១៨", "១៩", "២០"]
            for i, d in enumerate(docs_sorted):
                num = khmer_numbers[i] if i < len(khmer_numbers) else str(i + 1)
                msg += f"<b>{num}.</b> {escape_html(d['name'])}\n"
                msg += f"     <i>{d['size']:,} តួអក្សរ</i>\n\n"
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        else:
            logger.warning(f"Unknown callback: {data}")

    except Exception as e:
        logger.error(f"Callback error: {e}", exc_info=True)

async def start_from_callback(query):
    msg = (
        "╔═══════════════════╗\n"
        "║  🇰🇭 <b>ច្បាប់កម្ពុជា</b>  ║\n"
        "╚═══════════════════╝\n\n"
        "សូមស្វាគមន៍! 🤖 AI-Powered v17.5"
    )
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=build_start_keyboard())

# ═══════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "╔═══════════════════╗\n"
        "║  🇰🇭 <b>ច្បាប់កម្ពុជា</b>  ║\n"
        "╚═══════════════════╝\n\n"
        "សូមស្វាគមន៍! 🤖 <i>v17.5</i>\n\n"
        "📌 <b>របៀបប្រើ:</b>\n"
        "  • វាយពាក្យគន្លឹះ\n"
        "  • វាយ <code>មាត្រា ៥៥</code>\n"
        "  • វាយ <code>មាត្រា ៥ ព្រហ្មទណ្ឌ</code>\n\n"
        "✨ <b>Features:</b>\n"
        "  🤖 AI Rerank\n"
        "  🔥 មាត្រាពេញនិយម\n"
        "  🖨️ HTML Preview / Save PDF"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=build_start_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 <b>ជំនួយ v17.5</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 <code>លួច</code> → ស្វែងរក\n"
        "📌 <code>មាត្រា ៥៥</code> → រកមាត្រា\n\n"
        "🖨️ <b>Save PDF:</b>\n"
        "  ស្វែងរក → ចុច 🖨️ Preview/PDF\n"
        "  → បើក Link → Print → Save\n\n"
        "🎯 <b>Commands:</b>\n"
        "  /start /popular /docs /clear\n"
        "  /article /stats /preview"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_SESSIONS.pop(update.effective_user.id, None)
    save_sessions()
    CALLBACK_REGISTRY.clear()
    await update.message.reply_text("✅ លុប session")

async def docs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("📚 កំពុងទាញ...")
    data = list_docs()
    try:
        await status.delete()
    except:
        pass
    if not data.get("success"):
        await update.message.reply_text(f"❌ {data.get('error')}")
        return
    docs = data.get("documents", [])
    docs_sorted = sort_documents_by_priority(docs)
    msg = f"📚 <b>ឯកសារច្បាប់ទាំងអស់ ({len(docs_sorted)}):</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    khmer_numbers = ["១", "២", "៣", "៤", "៥", "៦", "៧", "៨", "៩", "១០",
                     "១១", "១២", "១៣", "១៤", "១៥", "១៦", "១៧", "១៨", "១៩", "២០"]
    for i, d in enumerate(docs_sorted):
        num = khmer_numbers[i] if i < len(khmer_numbers) else str(i + 1)
        msg += f"<b>{num}.</b> {escape_html(d['name'])}\n"
        msg += f"     <i>{d['size']:,} តួអក្សរ</i>\n\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def article_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "📌 <b>របៀបប្រើ:</b>\n  <code>/article ៥៥</code>\n  <code>/article ៥ ព្រហ្មទណ្ឌ</code>",
            parse_mode=ParseMode.HTML
        )
        return
    article_num = args[0]
    doc_name = " ".join(args[1:]) if len(args) > 1 else None
    await process_article_query(update, context, article_num, doc_name)

async def popular_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_popular_articles(context, update.effective_chat.id)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = 7
    if context.args and context.args[0].isdigit():
        days = int(context.args[0])
    await show_analytics(context, update.effective_chat.id, update.effective_user.id, days=days)

# ⭐ v17.5: /preview command
async def preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate preview from current session"""
    user_id = update.effective_user.id
    session = USER_SESSIONS.get(user_id)
    
    if not session or not session.get("results"):
        await update.message.reply_text(
            "⚠️ <b>មិនមានលទ្ធផលដើម្បី preview</b>\n\n"
            "សូមស្វែងរកមុនសិន ហើយចុច /preview\n"
            "ឬចុចប៊ូតុង 🖨️ Preview/PDF",
            parse_mode=ParseMode.HTML
        )
        return
    
    preview_data = build_preview_data(session)
    preview_id = store_preview(preview_data)
    
    base_url = RENDER_EXTERNAL_URL.rstrip("/")
    preview_url = f"{base_url}/preview/{preview_id}"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 បើកមើល Preview", url=preview_url)]
    ])
    
    await update.message.reply_text(
        f"🖨️ <b>Preview Ready!</b>\n\n"
        f"📊 {preview_data['total_articles']} មាត្រា | {preview_data['total_docs']} ច្បាប់\n\n"
        f"👉 ចុចប៊ូតុងខាងក្រោមដើម្បីបើក\n"
        f"ហើយ Save as PDF បាន!",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    logger.info(f"📨 Message: '{query}'")
    
    article_match = re.match(r'^មាត្រា\s*([0-9០-៩]+)\s*(.*)$', query)
    if article_match:
        article_num = article_match.group(1)
        doc_name = article_match.group(2).strip() or None
        logger.info(f"✅ Article: num={article_num}, doc={doc_name}")
        await process_article_query(update, context, article_num, doc_name)
        return
    
    await process_search_query(update, context, query)

# ═══════════════════════════════════════════════
# ⭐ v17.5: Flask Server (runs in thread)
# ═══════════════════════════════════════════════
def run_flask_server():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🌐 Production server starting on port {port}")
    try:
        from waitress import serve
        serve(flask_app, host="0.0.0.0", port=port, threads=4)
    except ImportError:
        logger.warning("⚠️ Waitress not installed, using Flask dev server")
        flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════
def main():
    if not TELEGRAM_TOKEN or not GAS_URL:
        logger.error("❌ Missing env vars")
        return

    logger.info("=" * 50)
    logger.info("🤖 Bot v17.5 (HTML Preview + PDF Save)")
    logger.info(f"📊 Admin IDs: {ADMIN_IDS}")
    logger.info(f"🌐 URL: {RENDER_EXTERNAL_URL}")
    logger.info("=" * 50)

    load_sessions()
    
    # ⭐ Start Flask in background thread
    threading.Thread(target=run_flask_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("docs", docs_cmd))
    app.add_handler(CommandHandler("article", article_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("popular", popular_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("preview", preview_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 Starting polling...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
