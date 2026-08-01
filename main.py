import os
import re
import json
import logging
import threading
import asyncio
import requests
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

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

RESULTS_PER_PAGE = 5
SESSIONS_FILE = "user_sessions.json"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

USER_SESSIONS = {}

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
            logger.info(f"✅ Loaded {len(USER_SESSIONS)} sessions from file")
        else:
            logger.info("ℹ️ No existing sessions file")
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
        logger.info(f"→ GAS: {payload}")
        response = requests.post(GAS_URL, json=payload, timeout=timeout, allow_redirects=True)
        logger.info(f"← Status: {response.status_code}")
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
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
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
            r'^(ជំពូកទី|ផ្នែកទី|មាត្រា|វិភាគទី|ផ្នែក|ទី\s*[០-៩\d])',
            line
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
# Format Preview Mode
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
    msg += f"📊 <b>{total_articles}</b> មាត្រា | <b>{total_docs}</b> ច្បាប់\n"

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
        for doc_name, doc_results in all_groups.items():
            cat = get_law_category(doc_name)
            in_page = "👁" if doc_name in page_groups else ""
            msg += f"  {cat['emoji']} {escape_html(doc_name)} ({len(doc_results)}) {in_page}\n"

    for doc_name, doc_results in page_groups.items():
        cat = get_law_category(doc_name)
        msg += f"\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b>\n"
        for r in doc_results:
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, _ = _split_title_and_body(content, article)
            if title:
                if len(title) > 45:
                    title = title[:42] + "..."
                msg += f"  ├ 📌 <b>មាត្រា {escape_html(str(article))}</b> - {escape_html(title)}\n"
            else:
                msg += f"  ├ 📌 <b>មាត្រា {escape_html(str(article))}</b>\n"

    msg += "\n👆 <i>ចុចប៊ូតុងខាងក្រោមដើម្បីមើលពេញ</i>"
    return msg

# ═══════════════════════════════════════════════
# Format Detailed Mode
# ═══════════════════════════════════════════════
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
    if pagination_info:
        msg += f" | 📄 <b>{pagination_info['current_page']}/{pagination_info['total_pages']}</b>"
    msg += "\n━━━━━━━━━━━━━━━━━━━━"

    selected_docs = session.get("selected_docs", [])
    if selected_docs and selected_docs != ["all"]:
        msg += f"\n📂 <b>ស្វែងរកក្នុង:</b> {len(selected_docs)} ច្បាប់"

    page_groups = group_results_by_document(results)
    doc_list = list(page_groups.keys())

    for doc_idx, (doc_name, doc_results) in enumerate(page_groups.items()):
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

            if article and title:
                msg += f"\n\n📌 <b>មាត្រា {escape_html(str(article))} - {escape_html(title)}</b>"
            elif article:
                msg += f"\n\n📌 <b>មាត្រា {escape_html(str(article))}</b>"

            if body:
                formatted_body = format_body_paragraphs(body, indent="    ")
                if keywords:
                    formatted_body = highlight_keywords_html(formatted_body, keywords)
                msg += f"\n{formatted_body}"

            if r_idx < len(doc_results) - 1:
                msg += "\n▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️"

        if doc_idx < len(doc_list) - 1:
            msg += "\n━━━━━━━━━━━━━━━━━━━━"

    return msg

# ═══════════════════════════════════════════════
# Document Selection Keyboard
# ═══════════════════════════════════════════════
def build_doc_selection_keyboard(available_docs, selected_docs=None, search_type="search"):
    if selected_docs is None:
        selected_docs = []
    buttons = []

    if len(selected_docs) == len(available_docs) and len(available_docs) > 0:
        buttons.append([
            InlineKeyboardButton("☑️ បានជ្រើសទាំងអស់ — ចុចដើម្បីដកចេញ", callback_data="docsel:none")
        ])
    else:
        buttons.append([
            InlineKeyboardButton("📚 ជ្រើសរើសទាំងអស់", callback_data="docsel:all")
        ])

    for idx, doc_name in enumerate(available_docs):
        cat = get_law_category(doc_name)
        is_selected = doc_name in selected_docs
        check = "✅" if is_selected else "⬜"
        short_name = doc_name if len(doc_name) <= 30 else doc_name[:27] + "..."
        buttons.append([
            InlineKeyboardButton(
                f"{check} {cat['emoji']} {cat['icon']} {short_name}",
                callback_data=f"docsel:toggle:{idx}"
            )
        ])

    action_row = []
    if selected_docs:
        count = len(selected_docs)
        action_row.append(
            InlineKeyboardButton(f"🔍 ស្វែងរក ({count} ច្បាប់)", callback_data="docsel:confirm")
        )
    else:
        action_row.append(
            InlineKeyboardButton("⚠️ សូមជ្រើសរើសច្បាប់", callback_data="docsel:warn")
        )
    action_row.append(
        InlineKeyboardButton("🔍 ស្វែងរកទាំងអស់", callback_data="docsel:skip")
    )
    buttons.append(action_row)

    buttons.append([
        InlineKeyboardButton("❌ បោះបង់", callback_data="docsel:cancel")
    ])

    return InlineKeyboardMarkup(buttons)

def build_doc_selection_message(query, available_docs, selected_docs=None, search_type="search"):
    if selected_docs is None:
        selected_docs = []

    if search_type == "article":
        msg = f"📂 <b>ជ្រើសរើសច្បាប់ដើម្បីរកមាត្រា</b>\n"
    else:
        msg = f"📂 <b>ជ្រើសរើសច្បាប់ដើម្បីស្វែងរក</b>\n"

    msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"🔍 សំណួរ: <code>{escape_html(query)}</code>\n\n"

    if selected_docs:
        msg += f"✅ <b>បានជ្រើសរើស {len(selected_docs)}/{len(available_docs)} ច្បាប់:</b>\n"
        for doc in selected_docs:
            cat = get_law_category(doc)
            msg += f"  {cat['emoji']} {cat['icon']} {escape_html(doc)}\n"
    else:
        msg += "⬜ <i>មិនទាន់បានជ្រើសរើសច្បាប់ទេ</i>\n"

    msg += f"\n💡 <i>ចុចលើច្បាប់ដើម្បីជ្រើសរើស/ដកចេញ</i>\n"
    msg += f"<i>អាចជ្រើសរើសច្បាប់ 1 ឬច្រើនបាន</i>"
    return msg

# ═══════════════════════════════════════════════
# Navigation Keyboard
# ═══════════════════════════════════════════════
def build_navigation_keyboard(session):
    pagination = paginate_results(session["results"], session["page"])
    buttons = []

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

    mode = session.get("view_mode", "preview")
    if mode == "preview":
        buttons.append([InlineKeyboardButton("👁 មើលពេញ", callback_data="mode:detailed")])
    else:
        buttons.append([InlineKeyboardButton("📋 មើលសង្ខេប", callback_data="mode:preview")])

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

    buttons.append([
        InlineKeyboardButton("📂 ប្តូរច្បាប់", callback_data="action:reselect_docs"),
        InlineKeyboardButton("🔍 ថ្មី", callback_data="action:new_search"),
        InlineKeyboardButton("❌ បិទ", callback_data="action:close")
    ])

    return InlineKeyboardMarkup(buttons)

def build_start_keyboard():
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
# Send Helpers (⭐ FIXED: ប្រើ chat_id ជំនួស update.message)
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


async def send_results_to_chat(context_or_bot, chat_id, session):
    """
    ⭐ ផ្ញើលទ្ធផលទៅ chat_id ដោយផ្ទាល់ (មិនពឹង update.message)
    """
    try:
        pagination = paginate_results(session["results"], session["page"])
        page_data = {"success": True, "results": pagination["results"]}
        view_mode = session.get("view_mode", "preview")

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
                chat_id=chat_id,
                text=prefix + part,
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
            if not is_last:
                await asyncio.sleep(0.3)

    except Exception as e:
        logger.error(f"❌ send_results_to_chat error: {e}", exc_info=True)


async def send_results_callback(update, session):
    """
    ⭐ ផ្ញើលទ្ធផលពី callback (edit message ដែលមានស្រាប់)
    """
    try:
        pagination = paginate_results(session["results"], session["page"])
        page_data = {"success": True, "results": pagination["results"]}
        view_mode = session.get("view_mode", "preview")

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


async def send_results_message(update, session):
    """
    ⭐ ផ្ញើលទ្ធផលពី message (reply to user message)
    """
    try:
        pagination = paginate_results(session["results"], session["page"])
        page_data = {"success": True, "results": pagination["results"]}
        view_mode = session.get("view_mode", "preview")

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

            await update.message.reply_text(
                prefix + part, parse_mode=ParseMode.HTML, reply_markup=kb
            )
            if not is_last:
                await asyncio.sleep(0.3)

    except Exception as e:
        logger.error(f"❌ send_results_message error: {e}", exc_info=True)


async def try_recover_session(update, is_callback=True):
    if is_callback:
        target = update.callback_query.message
    else:
        target = update.message

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 ត្រឡប់ Home", callback_data="action:home")
    ]])

    await target.reply_text(
        "⚠️ <b>Session បាត់</b>\n\n"
        "សូមស្វែងរកម្តងទៀត។\n\n"
        "💡 ឧទាហរណ៍:\n"
        "  • វាយ <code>លួច</code>\n"
        "  • វាយ <code>មាត្រា ៥៥</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

# ═══════════════════════════════════════════════
# Document Selection Flow
# ═══════════════════════════════════════════════
async def start_doc_selection(update, context, query, search_type="search", article_num=None):
    """
    ⭐ Show document selection. Works from both message and callback.
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if search_type == "article":
        loading_text = f"📂 កំពុងទាញបញ្ជីច្បាប់សម្រាប់មាត្រា {escape_html(str(article_num))}..."
    else:
        loading_text = "📂 កំពុងទាញបញ្ជីច្បាប់..."

    loading_msg = await context.bot.send_message(
        chat_id=chat_id, text=loading_text, parse_mode=ParseMode.HTML
    )

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

    session_data = {
        "pending_query": query,
        "pending_article": article_num,
        "search_type": search_type,
        "available_docs": available_docs,
        "selected_docs": [],
        "mode": "doc_selection",
        "state": "selecting"
    }
    update_session(user_id, session_data)

    display_query = f"មាត្រា {article_num}" if search_type == "article" else query
    msg_text = build_doc_selection_message(display_query, available_docs, [], search_type)
    keyboard = build_doc_selection_keyboard(available_docs, [], search_type)

    await context.bot.send_message(
        chat_id=chat_id,
        text=msg_text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


async def execute_keyword_search(update, context, query, doc_filter=None):
    """
    ⭐ Execute keyword search. ប្រើ context.bot.send_message ជំនួស update.message
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    status_msg = await context.bot.send_message(
        chat_id=chat_id, text="🔍 កំពុងស្វែងរក..."
    )

    data = search_law(query)
    total = data.get("count", 0)

    if not data.get("success") or total == 0:
        try:
            await status_msg.delete()
        except:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់ <b>{escape_html(query)}</b>\n\n💡 សូមសាកសំណួរផ្សេង",
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
        other_count = len(all_results)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 រកមិនឃើញលទ្ធផលក្នុងច្បាប់ដែលបានជ្រើសរើស\n\n"
                 f"💡 មាន <b>{other_count}</b> លទ្ធផលក្នុងច្បាប់ផ្សេងទៀត\n"
                 f"សូមសាកជ្រើសរើសច្បាប់ផ្សេង ឬស្វែងរកក្នុងទាំងអស់",
            parse_mode=ParseMode.HTML
        )
        return

    session_data = {
        "results": filtered_results,
        "all_results": filtered_results,
        "page": 0,
        "query": query,
        "mode": "search",
        "view_mode": "preview",
        "keywords": data.get("keywords", []),
        "filter": "all",
        "selected_docs": doc_filter,
        "original_query": query,
        "search_type": "search"
    }
    update_session(user_id, session_data)

    try:
        await status_msg.delete()
    except:
        pass

    # ⭐ ប្រើ send_results_to_chat ដែលប្រើ bot.send_message
    await send_results_to_chat(context.bot, chat_id, USER_SESSIONS[user_id])


async def execute_article_search(update, context, article_num, doc_filter=None):
    """
    ⭐ Execute article search. ប្រើ context.bot.send_message ជំនួស update.message
    """
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

        if doc_filter and doc_filter != ["all"]:
            all_data = find_article(article_num, None)
            if all_data.get("success") and all_data.get("results"):
                all_docs = set(r.get("document", "") for r in all_data["results"])
                docs_list = "\n".join(f"  • {escape_html(d)}" for d in all_docs)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔍 មាត្រា <b>{escape_html(str(article_num))}</b> "
                         f"រកមិនឃើញក្នុងច្បាប់ដែលបានជ្រើសរើស\n\n"
                         f"💡 មាត្រានេះមាននៅក្នុង:\n{docs_list}\n\n"
                         f"សូមសាកជ្រើសរើសច្បាប់ផ្សេង",
                    parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔍 រកមិនឃើញមាត្រា <b>{escape_html(str(article_num))}</b>\n\n"
                         f"💡 សូមពិនិត្យលេខមាត្រាម្តងទៀត",
                    parse_mode=ParseMode.HTML
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔍 រកមិនឃើញមាត្រា <b>{escape_html(str(article_num))}</b>\n\n"
                     f"💡 សូមពិនិត្យលេខមាត្រាម្តងទៀត",
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
        all_docs = set(r.get("document", "") for r in all_results)
        docs_list = "\n".join(f"  • {escape_html(d)}" for d in all_docs)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 មាត្រា <b>{escape_html(str(article_num))}</b> "
                 f"រកមិនឃើញក្នុងច្បាប់ដែលបានជ្រើសរើស\n\n"
                 f"💡 មាត្រានេះមាននៅក្នុង:\n{docs_list}",
            parse_mode=ParseMode.HTML
        )
        return

    if not doc_filter:
        doc_filter = ["all"]

    session_data = {
        "results": filtered_results,
        "all_results": filtered_results,
        "page": 0,
        "query": f"មាត្រា {article_num}",
        "mode": "article",
        "view_mode": "detailed",
        "keywords": [],
        "filter": "all",
        "selected_docs": doc_filter,
        "original_query": f"មាត្រា {article_num}",
        "original_article": str(article_num),
        "search_type": "article"
    }
    update_session(user_id, session_data)

    try:
        await status_msg.delete()
    except:
        pass

    # ⭐ ប្រើ send_results_to_chat
    await send_results_to_chat(context.bot, chat_id, USER_SESSIONS[user_id])


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
# Callback Handler
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
        # ═══════════════════════════════════════
        # Document Selection
        # ═══════════════════════════════════════
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
                msg_text = build_doc_selection_message(display_query, available_docs, selected_docs, search_type)
                keyboard = build_doc_selection_keyboard(available_docs, selected_docs, search_type)
                await query.edit_message_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

            elif data == "docsel:none":
                selected_docs = []
                session["selected_docs"] = selected_docs
                update_session(user_id, session)
                msg_text = build_doc_selection_message(display_query, available_docs, selected_docs, search_type)
                keyboard = build_doc_selection_keyboard(available_docs, selected_docs, search_type)
                await query.edit_message_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

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
                    msg_text = build_doc_selection_message(display_query, available_docs, selected_docs, search_type)
                    keyboard = build_doc_selection_keyboard(available_docs, selected_docs, search_type)
                    await query.edit_message_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

            elif data == "docsel:confirm":
                if not selected_docs:
                    await query.answer("⚠️ សូមជ្រើសរើសយ៉ាងតិច 1 ច្បាប់", show_alert=True)
                    return
                try:
                    await query.message.delete()
                except:
                    pass

                # ⭐ Execute search ដោយប្រើ context
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

        # ═══════════════════════════════════════
        # Re-select docs
        # ═══════════════════════════════════════
        if data == "action:reselect_docs":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            search_type = session.get("search_type", "search")
            original_query = session.get("original_query", session.get("query", ""))
            original_article = session.get("original_article", None)

            if search_type == "article" and original_article:
                await start_doc_selection(
                    update, context, original_query,
                    search_type="article", article_num=original_article
                )
            else:
                await start_doc_selection(
                    update, context, original_query, search_type="search"
                )
            return

        # ═══════════════════════════════════════
        # Navigation
        # ═══════════════════════════════════════
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
                await query.answer("⚠️ ទំព័រទី 1 ហើយ", show_alert=True)

        elif data == "nav:info":
            session = USER_SESSIONS.get(user_id)
            if session:
                pagination = paginate_results(session["results"], session["page"])
                await query.answer(
                    f"📊 ទំព័រ {pagination['current_page']}/{pagination['total_pages']}\n"
                    f"សរុប: {pagination['total']} លទ្ធផល",
                    show_alert=True
                )

        # ═══════════════════════════════════════
        # Mode
        # ═══════════════════════════════════════
        elif data == "mode:detailed":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            session["view_mode"] = "detailed"
            session["page"] = 0
            update_session(user_id, session)
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

        # ═══════════════════════════════════════
        # Filter
        # ═══════════════════════════════════════
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

        # ═══════════════════════════════════════
        # Quick search
        # ═══════════════════════════════════════
        elif data.startswith("quick:"):
            search_term = data.split(":", 1)[1]
            await start_doc_selection(update, context, search_term, search_type="search")

        # ═══════════════════════════════════════
        # Actions
        # ═══════════════════════════════════════
        elif data == "action:new_search":
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔍 <b>សូមវាយសំណួរថ្មី</b>\n\n"
                     "ឧទាហរណ៍:\n"
                     "  • <code>លួច</code> → ស្វែងរកពាក្យគន្លឹះ\n"
                     "  • <code>មាត្រា ៥៥</code> → រកមាត្រា\n"
                     "  • <code>មាត្រា ៥ ព្រហ្មទណ្ឌ</code> → រកក្នុងច្បាប់ជាក់លាក់",
                parse_mode=ParseMode.HTML
            )

        elif data == "action:close":
            try:
                await query.message.delete()
            except:
                pass

        elif data == "action:home":
            await start_from_callback(query)

        elif data == "action:help":
            msg = (
                "📖 <b>ជំនួយ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "🔍 <b>ស្វែងរកពាក្យគន្លឹះ:</b>\n"
                "  <code>លួច</code> → Bot សួរថាចង់រកក្នុងច្បាប់ណា\n\n"
                "📌 <b>រកមាត្រា:</b>\n"
                "  <code>មាត្រា ៥៥</code> → Bot សួរថាចង់រកក្នុងច្បាប់ណា\n"
                "  <code>មាត្រា ៥ ព្រហ្មទណ្ឌ</code> → រកផ្ទាល់\n\n"
                "📂 <b>ជ្រើសរើសច្បាប់:</b>\n"
                "  ✅ ជ្រើស 1 ឬច្រើន\n"
                "  📚 ឬស្វែងរកទាំងអស់\n\n"
                "🎨 <b>ពណ៌:</b>\n"
                "  🔴 ព្រហ្មទណ្ឌ | 🔵 រដ្ឋប្បវេណី | 🟢 ផ្សេងៗ"
            )
            await context.bot.send_message(
                chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML
            )

        elif data == "action:docs":
            status = await context.bot.send_message(chat_id=chat_id, text="📚 កំពុងទាញ...")
            docs_data = list_docs()
            try:
                await status.delete()
            except:
                pass
            if not docs_data.get("success"):
                await context.bot.send_message(
                    chat_id=chat_id, text=f"❌ {docs_data.get('error')}"
                )
                return
            docs = docs_data.get("documents", [])
            msg = f"📚 <b>ឯកសារ {len(docs)}៖</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
            for d in docs:
                cat = get_law_category(d['name'])
                msg += f"{cat['emoji']} {cat['icon']} <b>{escape_html(d['name'])}</b>\n"
                msg += f"   <i>{d['size']:,} តួអក្សរ</i>\n\n"
            await context.bot.send_message(
                chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML
            )

        else:
            logger.warning(f"Unknown callback: {data}")

    except Exception as e:
        logger.error(f"Callback error: {e}", exc_info=True)


async def start_from_callback(query):
    msg = (
        "╔═══════════════════╗\n"
        "║  🇰🇭 <b>ច្បាប់កម្ពុជា</b>  ║\n"
        "╚═══════════════════╝\n\n"
        "សូមស្វាគមន៍!\n\n"
        "🔥 <b>ស្វែងរកពេញនិយម:</b>"
    )
    await query.message.reply_text(
        msg, parse_mode=ParseMode.HTML,
        reply_markup=build_start_keyboard()
    )

# ═══════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "╔═══════════════════╗\n"
        "║  🇰🇭 <b>ច្បាប់កម្ពុជា</b>  ║\n"
        "╚═══════════════════╝\n\n"
        "សូមស្វាគមន៍មកកាន់ Bot ស្វែងរកច្បាប់!\n\n"
        "📌 <b>របៀបប្រើ:</b>\n"
        "  • វាយពាក្យគន្លឹះ → ជ្រើសរើសច្បាប់ → មើលលទ្ធផល\n"
        "  • វាយ <code>មាត្រា ៥៥</code> → ជ្រើសរើសច្បាប់\n"
        "  • វាយ <code>មាត្រា ៥ ព្រហ្មទណ្ឌ</code> → រកផ្ទាល់\n\n"
        "🔥 <b>ស្វែងរកពេញនិយម:</b>\n"
        "👇 ចុចប៊ូតុងខាងក្រោម ឬវាយសំណួរផ្ទាល់"
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.HTML,
        reply_markup=build_start_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 <b>ជំនួយ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 <code>លួច</code> → ជ្រើសរើសច្បាប់ រួចស្វែងរក\n"
        "📌 <code>មាត្រា ៥៥</code> → ជ្រើសរើសច្បាប់ រួចរកមាត្រា\n"
        "📌 <code>មាត្រា ៥ ព្រហ្មទណ្ឌ</code> → រកផ្ទាល់\n\n"
        "📂 <b>ជ្រើសរើសច្បាប់:</b>\n"
        "  អាចជ្រើស 1 ឬច្រើនបាន\n\n"
        "🎯 <b>ប៊ូតុង:</b>\n"
        "  👁 មើលពេញ | 📋 សង្ខេប\n"
        "  📂 ប្តូរច្បាប់ | 🔴🔵🟢 Filter"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_SESSIONS.pop(update.effective_user.id, None)
    save_sessions()
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
    msg = f"📚 <b>ឯកសារ {len(docs)}៖</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for d in docs:
        cat = get_law_category(d['name'])
        msg += f"{cat['emoji']} {cat['icon']} <b>{escape_html(d['name'])}</b>\n"
        msg += f"   <i>{d['size']:,} តួអក្សរ</i>\n\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def article_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "📌 <b>របៀបប្រើ:</b>\n"
            "  <code>/article ៥៥</code> → ជ្រើសរើសច្បាប់\n"
            "  <code>/article ៥ ព្រហ្មទណ្ឌ</code> → រកផ្ទាល់",
            parse_mode=ParseMode.HTML
        )
        return
    article_num = args[0]
    doc_name = " ".join(args[1:]) if len(args) > 1 else None
    await process_article_query(update, context, article_num, doc_name)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    article_match = re.match(r'^មាត្រា\s*([0-9០-៩]+)\s*(.*)$', query)
    if article_match:
        article_num = article_match.group(1)
        doc_name = article_match.group(2).strip() or None
        await process_article_query(update, context, article_num, doc_name)
        return
    await process_search_query(update, context, query)

# ═══════════════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════════════
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot v16.2 running")
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

    logger.info("=" * 50)
    logger.info("🤖 Bot v16.2 (Fixed: callback send)")
    logger.info("=" * 50)

    load_sessions()
    threading.Thread(target=run_http_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("docs", docs_cmd))
    app.add_handler(CommandHandler("article", article_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 Starting polling...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

# ═══════════════════════════════════════════════
# v16: New API calls
# ═══════════════════════════════════════════════
def record_feedback(user_id, query, document, article, action="click"):
    return call_gas({
        "mode": "feedback",
        "user_id": str(user_id),
        "query": query,
        "document": document,
        "article": article,
        "action": action
    })

def get_suggestions(partial_query):
    return call_gas({
        "mode": "suggestions",
        "query": partial_query
    })

def get_related_articles(document, article, count=5):
    return call_gas({
        "mode": "related",
        "document": document,
        "article": article,
        "count": count
    })

def get_popular_articles(limit=10):
    return call_gas({
        "mode": "popular",
        "limit": limit
    })

# Modify search_law to include user_id
def search_law(query, user_id="anonymous", use_ai=True):
    return call_gas({
        "mode": "search",
        "query": query,
        "user_id": str(user_id),
        "use_ai": use_ai
    })
