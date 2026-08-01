import os
import re
import logging
import threading
import asyncio
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, CallbackQueryHandler
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GAS_URL = os.getenv("GAS_URL")

RESULTS_PER_PAGE = 5  # ⭐ តូចជាង (5 មាត្រា/ទំព័រ) សម្រាប់ preview

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

USER_SESSIONS = {}
USER_BOOKMARKS = {}  # ⭐ NEW: {user_id: [article_data, ...]}


# ═══════════════════════════════════════════════
# Category Detection with Rich Emoji
# ═══════════════════════════════════════════════
def get_law_category(doc_name):
    if not doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "📄", "label": "ច្បាប់"}
    
    name = doc_name.lower()
    
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
# API Calls
# ═══════════════════════════════════════════════
def call_gas(payload, timeout=60):
    try:
        response = requests.post(GAS_URL, json=payload, timeout=timeout)
        if response.status_code != 200:
            return {"success": False, "error": f"HTTP {response.status_code}"}
        return response.json()
    except Exception as e:
        logger.error(f"GAS Error: {e}")
        return {"success": False, "error": str(e)}


def search_law(query):
    return call_gas({"mode": "search", "query": query})


def find_article(article_num, doc_name=None):
    payload = {"mode": "article", "article": article_num}
    if doc_name:
        payload["doc"] = doc_name
    return call_gas(payload)


def list_docs():
    return call_gas({"mode": "list"})


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
            return (doc, 999999)
        arabic = khmer_to_arabic_num(article)
        match = re.search(r'\d+', arabic)
        return (doc, int(match.group()) if match else 999999)
    return sorted(results, key=key_fn)


def paginate_results(results, page=0, per_page=RESULTS_PER_PAGE):
    total = len(results)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = page * per_page
    end = start + per_page
    return {
        "results": results[start:end],
        "total": total,
        "total_pages": total_pages,
        "current_page": page + 1,
        "has_next": (page + 1) < total_pages,
        "has_prev": page > 0,
        "start_idx": start + 1,
        "end_idx": min(end, total)
    }


# ═══════════════════════════════════════════════
# Text Helpers
# ═══════════════════════════════════════════════
def escape_html(text):
    if not text:
        return ""
    text = str(text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


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
            title = re.sub(r'^មាត្រា\s*[០-៩\d]+\s*[.។\-–—]*\s*', '', title).strip()
            body_start_idx = idx + 1
            break
    body = "\n".join(lines[body_start_idx:]).strip()
    body = re.sub(r'\n{2,}', '\n', body)
    return (title, body)


def _shorten_doc_name(name):
    if not name:
        return "N/A"
    replacements = {
        "ក្រមនីតិវិធីព្រហ្មទណ្ឌ": "នី.ព្រហ្មទណ្ឌ",
        "ក្រមព្រហ្មទណ្ឌ": "ព្រហ្មទណ្ឌ",
        "ក្រមនីតិវិធីរដ្ឋប្បវេណី": "នី.រដ្ឋប្បវេណី",
        "ក្រមរដ្ឋប្បវេណី": "រដ្ឋប្បវេណី",
        "ច្បាប់ស្តីពី": "",
        "ច្បាប់ស្ដីពី": "",
    }
    short = name
    for k, v in replacements.items():
        short = short.replace(k, v)
    short = short.strip()
    if len(short) > 20:
        short = short[:17] + "..."
    return short


def highlight_keywords(text, keywords):
    """⭐ Highlight ពាក្យស្វែងរកជា Bold"""
    if not keywords or not text:
        return escape_html(text)
    
    escaped = escape_html(text)
    for kw in keywords:
        if not kw or len(kw) < 2:
            continue
        # Case-insensitive replacement with bold
        pattern = re.compile(re.escape(escape_html(kw)), re.IGNORECASE)
        escaped = pattern.sub(f"<b><u>{escape_html(kw)}</u></b>", escaped)
    return escaped


def make_progress_bar(current, total, width=15):
    """⭐ Progress bar visualization"""
    if total == 0:
        return ""
    filled = int((current / total) * width)
    bar = "▓" * filled + "░" * (width - filled)
    percent = int((current / total) * 100)
    return f"{bar} {percent}%"


# ═══════════════════════════════════════════════
# ⭐ NEW: Preview Mode (TOC)
# ═══════════════════════════════════════════════
def format_preview_mode(data, session, pagination_info=None):
    """
    Preview mode - បង្ហាញ title ខ្លីៗ មិនបង្ហាញខ្លឹមសារពេញ
    """
    results = data.get("results", [])
    if not results:
        return "🔍 រកមិនឃើញលទ្ធផល"
    
    groups = group_results_by_document(results)
    total_docs = len(groups)
    total_articles = pagination_info["total"] if pagination_info else len(results)
    
    msg = "╔═══════════════════╗\n"
    msg += "║ 🔍 <b>លទ្ធផលស្វែងរក</b> ║\n"
    msg += "╚═══════════════════╝\n\n"
    
    query = session.get("query", "")
    msg += f"📝 <code>{escape_html(query)}</code>\n"
    msg += f"📊 <b>{total_articles}</b> មាត្រា | <b>{total_docs}</b> ច្បាប់\n"
    
    if pagination_info and pagination_info["total_pages"] > 1:
        bar = make_progress_bar(pagination_info["current_page"], pagination_info["total_pages"])
        msg += f"📄 {bar}\n"
    
    msg += "\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # ⭐ បង្ហាញ preview តាមច្បាប់
    page_groups = group_results_by_document(results)
    for doc_name, doc_results in page_groups.items():
        cat = get_law_category(doc_name)
        msg += f"{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b>\n"
        
        for r in doc_results:
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, _ = _split_title_and_body(content, article)
            
            if title:
                # Truncate title
                if len(title) > 45:
                    title = title[:42] + "..."
                msg += f"  ├ 📌 <b>មាត្រា {escape_html(str(article))}</b> - {escape_html(title)}\n"
            else:
                msg += f"  ├ 📌 <b>មាត្រា {escape_html(str(article))}</b>\n"
        msg += "\n"
    
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "👆 <i>ចុចប៊ូតុងខាងក្រោមដើម្បីមើលពេញ</i>\n"
    
    return msg


# ═══════════════════════════════════════════════
# ⭐ Detailed Mode (Full content)
# ═══════════════════════════════════════════════
def format_detailed_mode(data, session, pagination_info=None):
    """
    Detailed mode - បង្ហាញខ្លឹមសារពេញ + highlight
    """
    results = data.get("results", [])
    keywords = session.get("keywords", [])
    
    if not results:
        return "🔍 រកមិនឃើញលទ្ធផល"
    
    groups = group_results_by_document(results)
    total_articles = pagination_info["total"] if pagination_info else len(results)
    total_docs = len(groups)
    
    msg = f"🔍 <b>ស្វែងរក:</b> <code>{escape_html(session.get('query', ''))}</code>\n"
    msg += f"📊 <b>{total_articles}</b> មាត្រា | <b>{total_docs}</b> ច្បាប់"
    
    if pagination_info:
        msg += f" | 📄 <b>{pagination_info['current_page']}/{pagination_info['total_pages']}</b>"
    msg += "\n━━━━━━━━━━━━━━━━━━━━\n"
    
    page_groups = group_results_by_document(results)
    doc_list = list(page_groups.keys())
    
    for doc_idx, (doc_name, doc_results) in enumerate(page_groups.items()):
        cat = get_law_category(doc_name)
        msg += f"\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b>\n"
        
        for r in doc_results:
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, body = _split_title_and_body(content, article)
            
            if article and title:
                msg += f"\n📌 <b>មាត្រា {escape_html(str(article))} - {escape_html(title)}</b>\n"
            elif article:
                msg += f"\n📌 <b>មាត្រា {escape_html(str(article))}</b>\n"
            
            if body:
                # ⭐ Highlight keywords
                highlighted = highlight_keywords(body, keywords)
                msg += f"{highlighted}\n"
        
        if doc_idx < len(doc_list) - 1:
            msg += "━━━━━━━━━━━━━━━━━━━━\n"
    
    return msg


# ═══════════════════════════════════════════════
# ⭐ Inline Keyboard Builders
# ═══════════════════════════════════════════════
def build_navigation_keyboard(session):
    """ប៊ូតុង Navigation"""
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
    
    # Row 2: Mode toggle
    mode = session.get("view_mode", "preview")
    if mode == "preview":
        buttons.append([
            InlineKeyboardButton("👁 មើលពេញ", callback_data="mode:detailed")
        ])
    else:
        buttons.append([
            InlineKeyboardButton("📋 មើលសង្ខេប", callback_data="mode:preview")
        ])
    
    # Row 3: Filter by category
    results = session["results"]
    categories = set()
    for r in results:
        cat = get_law_category(r.get("document", ""))
        categories.add((cat["emoji"], cat["category"]))
    
    if len(categories) > 1:
        filter_row = [InlineKeyboardButton("🔍 ទាំងអស់", callback_data="filter:all")]
        for emoji, cat in categories:
            if cat == "criminal":
                filter_row.append(InlineKeyboardButton(f"{emoji} ព្រហ្មទណ្ឌ", callback_data=f"filter:{cat}"))
            elif cat == "civil":
                filter_row.append(InlineKeyboardButton(f"{emoji} រដ្ឋប្បវេណី", callback_data=f"filter:{cat}"))
            else:
                filter_row.append(InlineKeyboardButton(f"{emoji} ផ្សេងៗ", callback_data=f"filter:{cat}"))
        buttons.append(filter_row)
    
    # Row 4: Actions
    buttons.append([
        InlineKeyboardButton("🔍 ស្វែងរកថ្មី", callback_data="action:new_search"),
        InlineKeyboardButton("❌ បិទ", callback_data="action:close")
    ])
    
    return InlineKeyboardMarkup(buttons)


def build_start_keyboard():
    """ប៊ូតុងសំណួរពេញនិយម"""
    buttons = [
        [
            InlineKeyboardButton("⚖️ ការលួច", callback_data="quick:លួច"),
            InlineKeyboardButton("💰 ការក្លែងបន្លំ", callback_data="quick:ក្លែងបន្លំ"),
        ],
        [
            InlineKeyboardButton("👨‍👩‍👧 គ្រួសារ", callback_data="quick:គ្រួសារ"),
            InlineKeyboardButton("💼 កិច្ចសន្យា", callback_data="quick:កិច្ចសន្យា"),
        ],
        [
            InlineKeyboardButton("🏞️ ដីធ្លី", callback_data="quick:ដីធ្លី"),
            InlineKeyboardButton("🚗 ចរាចរណ៍", callback_data="quick:ចរាចរណ៍"),
        ],
        [
            InlineKeyboardButton("📚 មើលឯកសារ", callback_data="action:docs"),
            InlineKeyboardButton("❓ ជំនួយ", callback_data="action:help"),
        ]
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


async def send_or_edit(update_or_query, text, keyboard=None, is_callback=False):
    """Send new message or edit existing"""
    try:
        if is_callback:
            await update_or_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        else:
            await update_or_query.message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Send error: {e}")
        # Fallback without HTML
        try:
            plain = re.sub(r'<[^>]+>', '', text)
            if is_callback:
                await update_or_query.edit_message_text(plain, reply_markup=keyboard)
            else:
                await update_or_query.message.reply_text(plain, reply_markup=keyboard)
        except Exception as e2:
            logger.error(f"Fallback error: {e2}")


async def send_results(update, session, is_callback=False):
    """បង្ហាញលទ្ធផលជាមួយ inline keyboard"""
    pagination = paginate_results(session["results"], session["page"])
    
    page_data = {
        "success": True,
        "results": pagination["results"]
    }
    
    view_mode = session.get("view_mode", "preview")
    
    if view_mode == "preview":
        text = format_preview_mode(page_data, session, pagination)
    else:
        text = format_detailed_mode(page_data, session, pagination)
    
    keyboard = build_navigation_keyboard(session)
    
    # Split if too long
    parts = smart_split_html(text, 3800)
    
    if len(parts) == 1:
        target = update.callback_query if is_callback else update
        await send_or_edit(target, parts[0], keyboard, is_callback)
    else:
        # Send first part with header
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            kb = keyboard if is_last else None
            prefix = f"📄 <i>(ភាគ {i+1}/{len(parts)})</i>\n\n" if len(parts) > 1 else ""
            
            if i == 0 and is_callback:
                await update.callback_query.edit_message_text(
                    prefix + part, parse_mode=ParseMode.HTML, reply_markup=kb
                )
            else:
                await update.effective_chat.send_message(
                    prefix + part, parse_mode=ParseMode.HTML, reply_markup=kb
                )
            await asyncio.sleep(0.3)


# ═══════════════════════════════════════════════
# Process Queries
# ═══════════════════════════════════════════════
async def process_search_query(update, query, is_callback=False):
    user_id = update.effective_user.id
    
    if not is_callback:
        status_msg = await update.message.reply_text("🔍 កំពុងស្វែងរក...")
    
    data = search_law(query)
    total = data.get("count", 0)
    
    if not data.get("success") or total == 0:
        if not is_callback:
            try:
                await status_msg.delete()
            except:
                pass
        await update.effective_chat.send_message(
            f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់ <b>{escape_html(query)}</b>\n\n"
            f"💡 សូមសាកសំណួរផ្សេង",
            parse_mode=ParseMode.HTML
        )
        return
    
    sorted_results = sort_results_by_article(data.get("results", []))
    
    USER_SESSIONS[user_id] = {
        "results": sorted_results,
        "all_results": sorted_results,  # ⭐ NEW: backup for filter
        "page": 0,
        "query": query,
        "mode": "search",
        "view_mode": "preview",  # ⭐ Start with preview
        "keywords": data.get("keywords", []),
        "filter": "all"
    }
    
    if not is_callback:
        try:
            await status_msg.delete()
        except:
            pass
    
    await send_results(update, USER_SESSIONS[user_id], is_callback=False)


async def process_article_query(update, article_num, doc_name=None):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text(
        f"🔍 កំពុងស្វែងរកមាត្រា <b>{escape_html(article_num)}</b>...",
        parse_mode=ParseMode.HTML
    )
    
    data = find_article(article_num, doc_name)
    
    if not data.get("success") or not data.get("results"):
        try:
            await status_msg.delete()
        except:
            pass
        await update.message.reply_text(
            f"🔍 រកមិនឃើញមាត្រា <b>{escape_html(article_num)}</b>",
            parse_mode=ParseMode.HTML
        )
        return
    
    sorted_results = sort_results_by_article(data.get("results", []))
    
    USER_SESSIONS[user_id] = {
        "results": sorted_results,
        "all_results": sorted_results,
        "page": 0,
        "query": f"មាត្រា {article_num}",
        "mode": "article",
        "view_mode": "detailed",  # ⭐ Article = detailed
        "keywords": [],
        "filter": "all"
    }
    
    try:
        await status_msg.delete()
    except:
        pass
    
    await send_results(update, USER_SESSIONS[user_id], is_callback=False)


# ═══════════════════════════════════════════════
# ⭐ Callback Handler (Inline Buttons)
# ═══════════════════════════════════════════════
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data
    
    logger.info(f"Callback: {data} from user {user_id}")
    
    # Navigation
    if data == "nav:next":
        session = USER_SESSIONS.get(user_id)
        if session:
            pagination = paginate_results(session["results"], session["page"])
            if pagination["has_next"]:
                session["page"] += 1
                await send_results(update, session, is_callback=True)
    
    elif data == "nav:prev":
        session = USER_SESSIONS.get(user_id)
        if session and session["page"] > 0:
            session["page"] -= 1
            await send_results(update, session, is_callback=True)
    
    elif data == "nav:info":
        session = USER_SESSIONS.get(user_id)
        if session:
            pagination = paginate_results(session["results"], session["page"])
            await query.answer(
                f"ទំព័រ {pagination['current_page']}/{pagination['total_pages']} "
                f"({pagination['total']} លទ្ធផលសរុប)",
                show_alert=True
            )
    
    # Mode toggle
    elif data == "mode:detailed":
        session = USER_SESSIONS.get(user_id)
        if session:
            session["view_mode"] = "detailed"
            session["page"] = 0
            await send_results(update, session, is_callback=True)
    
    elif data == "mode:preview":
        session = USER_SESSIONS.get(user_id)
        if session:
            session["view_mode"] = "preview"
            session["page"] = 0
            await send_results(update, session, is_callback=True)
    
    # Filter
    elif data.startswith("filter:"):
        session = USER_SESSIONS.get(user_id)
        if session:
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
            if session["results"]:
                await send_results(update, session, is_callback=True)
            else:
                await query.answer("គ្មានលទ្ធផលក្នុងច្បាប់នេះ", show_alert=True)
    
    # Quick search
    elif data.startswith("quick:"):
        search_term = data.split(":", 1)[1]
        # Create a fake update for search
        await query.message.reply_text(f"🔍 កំពុងស្វែងរក: <b>{search_term}</b>", parse_mode=ParseMode.HTML)
        # Manually run search
        data_result = search_law(search_term)
        if data_result.get("success") and data_result.get("count", 0) > 0:
            sorted_results = sort_results_by_article(data_result.get("results", []))
            USER_SESSIONS[user_id] = {
                "results": sorted_results,
                "all_results": sorted_results,
                "page": 0,
                "query": search_term,
                "mode": "search",
                "view_mode": "preview",
                "keywords": data_result.get("keywords", []),
                "filter": "all"
            }
            # Send new message with keyboard
            pagination = paginate_results(sorted_results, 0)
            page_data = {"success": True, "results": pagination["results"]}
            text = format_preview_mode(page_data, USER_SESSIONS[user_id], pagination)
            kb = build_navigation_keyboard(USER_SESSIONS[user_id])
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    
    # Actions
    elif data == "action:new_search":
        await query.message.reply_text(
            "🔍 សូមវាយសំណួរថ្មី\n\n"
            "ឧទាហរណ៍:\n"
            "  • <code>លួច</code>\n"
            "  • <code>មាត្រា ៥៥</code>\n"
            "  • <code>ការក្លែងបន្លំ</code>",
            parse_mode=ParseMode.HTML
        )
    
    elif data == "action:close":
        try:
            await query.message.delete()
        except:
            pass
        USER_SESSIONS.pop(user_id, None)
    
    elif data == "action:help":
        await help_cmd_from_callback(query)
    
    elif data == "action:docs":
        await docs_from_callback(query)


async def help_cmd_from_callback(query):
    msg = (
        "📖 <b>ជំនួយប្រើប្រាស់</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 <b>ស្វែងរក:</b>\n"
        "  <code>លួច</code> - keyword\n"
        "  <code>មាត្រា ៥៥</code> - article\n\n"
        "🎯 <b>ការប្រើប៊ូតុង:</b>\n"
        "  👁 មើលពេញ - ខ្លឹមសារពេញ\n"
        "  📋 មើលសង្ខេប - តែចំណងជើង\n"
        "  🔴🔵🟢 - Filter តាមច្បាប់\n\n"
        "🎨 <b>ពណ៌:</b>\n"
        "  🔴 = ព្រហ្មទណ្ឌ\n"
        "  🔵 = រដ្ឋប្បវេណី\n"
        "  🟢 = ច្បាប់ផ្សេងៗ"
    )
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def docs_from_callback(query):
    data = list_docs()
    if not data.get("success"):
        await query.message.reply_text(f"❌ {data.get('error')}")
        return
    
    docs = data.get("documents", [])
    msg = f"📚 <b>ឯកសារ {len(docs)}៖</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, d in enumerate(docs, 1):
        cat = get_law_category(d['name'])
        msg += f"{cat['emoji']} {cat['icon']} <b>{escape_html(d['name'])}</b>\n"
        msg += f"   <i>{d['size']:,} តួអក្សរ</i>\n\n"
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════
# Command Handlers
# ═══════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "╔═══════════════════╗\n"
        "║  🇰🇭 <b>ច្បាប់កម្ពុជា</b>  ║\n"
        "╚═══════════════════╝\n\n"
        "សូមស្វាគមន៍មកកាន់ Bot ស្វែងរកច្បាប់!\n\n"
        "🔥 <b>ស្វែងរកពេញនិយម:</b>\n"
        "👇 ចុចប៊ូតុងខាងក្រោម ឬវាយសំណួរផ្ទាល់"
    )
    keyboard = build_start_keyboard()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 <b>ជំនួយប្រើប្រាស់</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 <b>ស្វែងរក:</b>\n"
        "  <code>លួច</code> - keyword\n"
        "  <code>មាត្រា ៥៥</code> - article\n\n"
        "🎯 <b>ការប្រើប៊ូតុង:</b>\n"
        "  👁 មើលពេញ - ខ្លឹមសារពេញ\n"
        "  📋 មើលសង្ខេប - តែចំណងជើង\n"
        "  ⬅️ ➡️ - ទំព័រមុន/បន្ទាប់\n"
        "  🔴🔵🟢 - Filter តាមច្បាប់\n\n"
        "🎨 <b>ពណ៌:</b>\n"
        "  🔴 ព្រហ្មទណ្ឌ | 🔵 រដ្ឋប្បវេណី | 🟢 ផ្សេងៗ\n\n"
        "⚙️ <b>Commands:</b>\n"
        "  /start - ទំព័រដើម\n"
        "  /docs - បញ្ជីឯកសារ\n"
        "  /clear - លុប session"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_SESSIONS.pop(update.effective_user.id, None)
    await update.message.reply_text("✅ លុប session រួច")


async def docs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 កំពុងទាញឯកសារ...")
    data = list_docs()
    if not data.get("success"):
        await update.message.reply_text(f"❌ {data.get('error')}")
        return
    
    docs = data.get("documents", [])
    msg = f"📚 <b>ឯកសារ {len(docs)}៖</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, d in enumerate(docs, 1):
        cat = get_law_category(d['name'])
        msg += f"{cat['emoji']} {cat['icon']} <b>{escape_html(d['name'])}</b>\n"
        msg += f"   <i>{d['size']:,} តួអក្សរ</i>\n\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def article_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("ឧ.: /article ៥ ព្រហ្មទណ្ឌ")
        return
    article_num = args[0]
    doc_name = " ".join(args[1:]) if len(args) > 1 else None
    await process_article_query(update, article_num, doc_name)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    
    article_match = re.match(r'^មាត្រា\s*([0-9០-៩]+)(.*)$', query)
    if article_match:
        article_num = article_match.group(1)
        doc_name = article_match.group(2).strip() or None
        await process_article_query(update, article_num, doc_name)
        return
    
    await process_search_query(update, query)


# ═══════════════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════════════
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def log_message(self, format, *args):
        return


def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), SimpleHandler).serve_forever()


# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════
def main():
    if not TELEGRAM_TOKEN or not GAS_URL:
        logger.error("❌ Missing env vars")
        return
    
    logger.info("🤖 Bot v13 starting (Ultimate Beautiful)")
    threading.Thread(target=run_http_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("docs", docs_cmd))
    app.add_handler(CommandHandler("article", article_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))  # ⭐ NEW
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
