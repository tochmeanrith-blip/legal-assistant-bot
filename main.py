import os
import re
import json
import logging
import threading
import asyncio
import requests
from datetime import datetime
from io import BytesIO
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
            title = re.sub(r'^មាត្រា\s*[០-៩\d]+\s*[.។\-–—]*\s*', '', title).strip()
            body_start_idx = idx + 1
            break
    body = "\n".join(lines[body_start_idx:]).strip()
    body = re.sub(r'\n{2,}', '\n', body)
    return (title, body)


def _split_into_paragraphs(text, max_sentences_per_para=3):
    """បំបែកកថាខណ្ឌ - 3 ប្រយោគ/para"""
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


def format_body_paragraphs(body, indent="    "):
    """
    ⭐ v15: Compact - គ្មានគម្លាតបន្ទាត់ធំៗ
    """
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
    # ⭐ ប្រើ \n តែមួយ (compact)
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
    
    msg += "\n👆 <i>ចុចប៊ូតុងខាងក្រោមដើម្បីមើលពេញ ឬ 💾 រក្សា PDF</i>"
    return msg


# ═══════════════════════════════════════════════
# ⭐ v15: Format Detailed Mode (COMPACT)
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
    
    page_groups = group_results_by_document(results)
    doc_list = list(page_groups.keys())
    
    for doc_idx, (doc_name, doc_results) in enumerate(page_groups.items()):
        cat = get_law_category(doc_name)
        total_in_doc = len(all_groups.get(doc_name, []))
        current_in_page = len(doc_results)
        
        # ⭐ Doc header - compact
        if total_in_doc > current_in_page:
            msg += f"\n\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b> ({current_in_page}/{total_in_doc})"
        else:
            msg += f"\n\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b> ({total_in_doc})"
        
        for r_idx, r in enumerate(doc_results):
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, body = _split_title_and_body(content, article)
            
            # ⭐ Article header - តែ \n\n មួយ
            if article and title:
                msg += f"\n\n📌 <b>មាត្រា {escape_html(str(article))} - {escape_html(title)}</b>"
            elif article:
                msg += f"\n\n📌 <b>មាត្រា {escape_html(str(article))}</b>"
            
            # ⭐ Body - តែ \n មួយ មុន body
            if body:
                formatted_body = format_body_paragraphs(body, indent="    ")
                if keywords:
                    formatted_body = highlight_keywords_html(formatted_body, keywords)
                msg += f"\n{formatted_body}"
            
            # ⭐ Separator តូច
            if r_idx < len(doc_results) - 1:
                msg += "\n▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️"
        
        if doc_idx < len(doc_list) - 1:
            msg += "\n━━━━━━━━━━━━━━━━━━━━"
    
    return msg


# ═══════════════════════════════════════════════
# ⭐ v15: PDF Generation with WeasyPrint
# ═══════════════════════════════════════════════
def generate_pdf(session):
    """Generate beautiful PDF with WeasyPrint (Khmer 100% support)"""
    from weasyprint import HTML
    from weasyprint.text.fonts import FontConfiguration
    
    all_results = session.get("all_results", [])
    all_groups = group_results_by_document(all_results)
    total_docs = len(all_groups)
    total_articles = len(all_results)
    query = session.get("query", "")
    date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    
    # ⭐ Build HTML
    html_content = f"""<!DOCTYPE html>
<html lang="km">
<head>
<meta charset="UTF-8">
<title>ច្បាប់កម្ពុជា - {escape_html(query)}</title>
<style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Khmer:wght@400;700&family=Noto+Serif+Khmer:wght@400;700&display=swap');
    
    @page {{
        size: A4;
        margin: 2.5cm 2cm 2cm 2cm;
        
        @top-center {{
            content: "🇰🇭 ច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា";
            font-family: 'Noto Sans Khmer', 'Khmer OS', sans-serif;
            font-size: 9pt;
            color: #DC2626;
            font-weight: bold;
            padding-bottom: 5px;
        }}
        
        @bottom-center {{
            content: "ទំព័រទី " counter(page) " / " counter(pages);
            font-family: 'Noto Sans Khmer', 'Khmer OS', sans-serif;
            font-size: 9pt;
            color: #94A3B8;
        }}
    }}
    
    * {{
        font-family: 'Noto Sans Khmer', 'Khmer OS', sans-serif;
        box-sizing: border-box;
    }}
    
    body {{
        color: #1E293B;
        line-height: 1.7;
        font-size: 11pt;
        margin: 0;
        padding: 0;
    }}
    
    /* ─── Cover Page ─── */
    .cover {{
        text-align: center;
        padding: 3cm 0;
        page-break-after: always;
    }}
    
    .cover .flag {{
        font-size: 48pt;
        margin: 20px 0;
    }}
    
    .cover h1 {{
        font-family: 'Noto Serif Khmer', serif;
        font-size: 28pt;
        color: #DC2626;
        margin: 20px 0;
        line-height: 1.3;
        font-weight: bold;
    }}
    
    .cover .subtitle {{
        font-size: 14pt;
        color: #64748B;
        margin-bottom: 40px;
        font-style: italic;
    }}
    
    .divider {{
        width: 60%;
        height: 3px;
        background: linear-gradient(to right, transparent, #DC2626, transparent);
        margin: 30px auto;
        border: none;
    }}
    
    /* ─── Info Table ─── */
    .info-table {{
        width: 80%;
        margin: 40px auto;
        border-collapse: collapse;
        background: #F8FAFC;
        border: 2px solid #DC2626;
        border-radius: 8px;
        overflow: hidden;
    }}
    
    .info-table td {{
        padding: 12px 20px;
        border-bottom: 1px solid #E2E8F0;
        text-align: left;
        font-size: 11pt;
    }}
    
    .info-table tr:last-child td {{
        border-bottom: none;
    }}
    
    .info-table td:first-child {{
        font-weight: bold;
        color: #475569;
        width: 40%;
        background: #F1F5F9;
    }}
    
    .info-table td:last-child {{
        color: #0F172A;
    }}
    
    /* ─── TOC ─── */
    .toc {{
        page-break-after: always;
    }}
    
    .toc-title {{
        font-size: 22pt;
        color: white;
        background: linear-gradient(135deg, #1E293B, #334155);
        padding: 15px 20px;
        text-align: center;
        margin-bottom: 20px;
        border-radius: 8px;
        font-weight: bold;
    }}
    
    .toc-table {{
        width: 100%;
        border-collapse: collapse;
        margin-top: 20px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }}
    
    .toc-table th {{
        background: #1E293B;
        color: white;
        padding: 12px;
        text-align: left;
        font-size: 11pt;
        font-weight: bold;
    }}
    
    .toc-table td {{
        padding: 10px 12px;
        border-bottom: 1px solid #E2E8F0;
        font-size: 10pt;
    }}
    
    .toc-table tr:nth-child(even) {{
        background: #F8FAFC;
    }}
    
    /* ─── Document Section ─── */
    .doc-section {{
        page-break-before: always;
        margin-top: 10px;
    }}
    
    .doc-section:first-of-type {{
        page-break-before: auto;
    }}
    
    .doc-header {{
        padding: 15px 20px;
        color: white;
        border-radius: 8px;
        margin-bottom: 20px;
        font-size: 15pt;
        font-weight: bold;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }}
    
    .doc-header.criminal {{
        background: linear-gradient(135deg, #DC2626, #991B1B);
    }}
    
    .doc-header.civil {{
        background: linear-gradient(135deg, #2563EB, #1E40AF);
    }}
    
    .doc-header.other {{
        background: linear-gradient(135deg, #059669, #047857);
    }}
    
    .doc-count {{
        display: inline-block;
        background: rgba(255,255,255,0.25);
        padding: 3px 10px;
        border-radius: 20px;
        font-size: 10pt;
        margin-left: 10px;
        font-weight: normal;
    }}
    
    /* ─── Article ─── */
    .article {{
        margin: 15px 0;
        page-break-inside: avoid;
    }}
    
    .article-header {{
        background: linear-gradient(to right, #FEF3C7, #FDE68A);
        border-left: 4px solid #F59E0B;
        padding: 10px 15px;
        margin-bottom: 8px;
        font-weight: bold;
        font-size: 12pt;
        color: #78350F;
        border-radius: 4px;
    }}
    
    .article-body {{
        padding: 0 10px;
    }}
    
    .article-body p {{
        text-indent: 30px;
        text-align: justify;
        margin: 6px 0;
        line-height: 1.8;
        color: #334155;
    }}
    
    .sub-header {{
        color: #7C3AED;
        font-weight: bold;
        margin: 12px 0 6px 0;
        padding: 8px 12px;
        background: #F3E8FF;
        border-left: 3px solid #7C3AED;
        border-radius: 4px;
        font-size: 11pt;
    }}
    
    /* ─── Separator ─── */
    .separator {{
        text-align: center;
        margin: 12px 0;
        color: #CBD5E1;
        font-size: 10pt;
        letter-spacing: 5px;
    }}
    
    /* ─── Badge ─── */
    .badge {{
        display: inline-block;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 9pt;
        margin-right: 5px;
        font-weight: bold;
    }}
    
    .badge.criminal {{ background: #FEE2E2; color: #991B1B; }}
    .badge.civil {{ background: #DBEAFE; color: #1E40AF; }}
    .badge.other {{ background: #D1FAE5; color: #065F46; }}
    
    /* ─── Footer ─── */
    .footer {{
        margin-top: 40px;
        padding-top: 20px;
        border-top: 2px solid #DC2626;
        text-align: center;
        color: #94A3B8;
        font-size: 9pt;
    }}
</style>
</head>
<body>

<!-- ═══════ COVER PAGE ═══════ -->
<div class="cover">
    <div class="flag">🇰🇭</div>
    <h1>ច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា</h1>
    <div class="subtitle">Cambodia Law Reference</div>
    <hr class="divider">
    
    <table class="info-table">
        <tr>
            <td>🔍 សំណួរស្វែងរក</td>
            <td><strong>{escape_html(query)}</strong></td>
        </tr>
        <tr>
            <td>📊 ចំនួនមាត្រា</td>
            <td>{total_articles} មាត្រា</td>
        </tr>
        <tr>
            <td>📚 ចំនួនច្បាប់</td>
            <td>{total_docs} ច្បាប់</td>
        </tr>
        <tr>
            <td>📅 កាលបរិច្ឆេទ</td>
            <td>{date_str}</td>
        </tr>
    </table>
</div>

<!-- ═══════ TOC ═══════ -->
<div class="toc">
    <div class="toc-title">📋 មាតិកា</div>
    <table class="toc-table">
        <thead>
            <tr>
                <th style="width:5%; text-align:center">#</th>
                <th style="width:75%">ច្បាប់</th>
                <th style="width:20%; text-align:center">ចំនួនមាត្រា</th>
            </tr>
        </thead>
        <tbody>
"""
    
    for i, (doc_name, doc_results) in enumerate(all_groups.items(), 1):
        cat = get_law_category(doc_name)
        html_content += f"""            <tr>
                <td style="text-align:center"><strong>{i}</strong></td>
                <td>
                    <span class="badge {cat['category']}">{cat['label']}</span>
                    {escape_html(doc_name)}
                </td>
                <td style="text-align:center"><strong>{len(doc_results)}</strong> មាត្រា</td>
            </tr>
"""
    
    html_content += """        </tbody>
    </table>
</div>

<!-- ═══════ CONTENT ═══════ -->
"""
    
    for doc_idx, (doc_name, doc_results) in enumerate(all_groups.items()):
        cat = get_law_category(doc_name)
        
        html_content += f"""
<div class="doc-section">
    <div class="doc-header {cat['category']}">
        {cat['icon']} {escape_html(doc_name)}
        <span class="doc-count">{len(doc_results)} មាត្រា</span>
    </div>
"""
        
        for r_idx, r in enumerate(doc_results):
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, body = _split_title_and_body(content, article)
            
            if article and title:
                article_title = f"📌 មាត្រា {article} - {title}"
            elif article:
                article_title = f"📌 មាត្រា {article}"
            else:
                article_title = "📌 ខ្លឹមសារ"
            
            html_content += f"""    <div class="article">
        <div class="article-header">{escape_html(article_title)}</div>
        <div class="article-body">
"""
            
            if body:
                body_lines = body.split("\n")
                for line in body_lines:
                    line = line.strip()
                    if not line:
                        continue
                    
                    is_subheader = bool(re.match(
                        r'^(ជំពូកទី|ផ្នែកទី|វិភាគទី|ផ្នែក)',
                        line
                    ))
                    
                    if is_subheader:
                        html_content += f'            <div class="sub-header">▸ {escape_html(line)}</div>\n'
                    else:
                        paragraphs = _split_into_paragraphs(line)
                        for para in paragraphs:
                            if para.strip():
                                html_content += f'            <p>{escape_html(para.strip())}</p>\n'
            
            html_content += """        </div>
    </div>
"""
            
            if r_idx < len(doc_results) - 1:
                html_content += '    <div class="separator">◦ ◦ ◦</div>\n'
        
        html_content += "</div>\n"
    
    html_content += f"""
<div class="footer">
    <p><strong>ឯកសារនេះបានបង្កើតដោយស្វ័យប្រវត្តិ</strong></p>
    <p>🤖 Cambodia Law Bot | {date_str}</p>
</div>

</body>
</html>
"""
    
    # Generate PDF
    logger.info("🔨 Generating PDF with WeasyPrint...")
    font_config = FontConfiguration()
    pdf_bytes = HTML(string=html_content).write_pdf(font_config=font_config)
    
    buffer = BytesIO(pdf_bytes)
    buffer.seek(0)
    logger.info(f"✅ PDF generated: {len(pdf_bytes):,} bytes")
    
    return buffer


# ═══════════════════════════════════════════════
# Inline Keyboards
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
    
    # Row 2: Mode + PDF
    mode = session.get("view_mode", "preview")
    mode_row = []
    if mode == "preview":
        mode_row.append(InlineKeyboardButton("👁 មើលពេញ", callback_data="mode:detailed"))
    else:
        mode_row.append(InlineKeyboardButton("📋 មើលសង្ខេប", callback_data="mode:preview"))
    mode_row.append(InlineKeyboardButton("💾 រក្សា PDF", callback_data="action:save_pdf"))
    buttons.append(mode_row)
    
    # Row 3: Filter
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
    
    # Row 4: Actions
    buttons.append([
        InlineKeyboardButton("🔍 ស្វែងរកថ្មី", callback_data="action:new_search"),
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


async def send_results(update, session, is_callback=False):
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
            if is_callback:
                try:
                    await update.callback_query.edit_message_text(
                        parts[0], parse_mode=ParseMode.HTML, reply_markup=keyboard
                    )
                except Exception as e:
                    logger.warning(f"Edit failed, sending new: {e}")
                    await update.effective_chat.send_message(
                        parts[0], parse_mode=ParseMode.HTML, reply_markup=keyboard
                    )
            else:
                await update.message.reply_text(
                    parts[0], parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
        else:
            for i, part in enumerate(parts):
                is_last = (i == len(parts) - 1)
                kb = keyboard if is_last else None
                prefix = f"📄 <i>(ភាគ {i+1}/{len(parts)})</i>\n\n" if len(parts) > 1 else ""
                
                if i == 0 and is_callback:
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
        logger.error(f"❌ send_results error: {e}", exc_info=True)


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
# Process Queries
# ═══════════════════════════════════════════════
async def process_search_query(update, query, is_callback=False):
    user_id = update.effective_user.id
    
    if not is_callback:
        status_msg = await update.message.reply_text("🔍 កំពុងស្វែងរក...")
    else:
        status_msg = None
    
    data = search_law(query)
    total = data.get("count", 0)
    
    if not data.get("success") or total == 0:
        if status_msg:
            try:
                await status_msg.delete()
            except:
                pass
        await update.effective_chat.send_message(
            f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់ <b>{escape_html(query)}</b>\n\n💡 សូមសាកសំណួរផ្សេង",
            parse_mode=ParseMode.HTML
        )
        return
    
    sorted_results = sort_results_by_article(data.get("results", []))
    
    session_data = {
        "results": sorted_results,
        "all_results": sorted_results,
        "page": 0,
        "query": query,
        "mode": "search",
        "view_mode": "preview",
        "keywords": data.get("keywords", []),
        "filter": "all"
    }
    update_session(user_id, session_data)
    
    if status_msg:
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
    
    session_data = {
        "results": sorted_results,
        "all_results": sorted_results,
        "page": 0,
        "query": f"មាត្រា {article_num}",
        "mode": "article",
        "view_mode": "detailed",
        "keywords": [],
        "filter": "all"
    }
    update_session(user_id, session_data)
    
    try:
        await status_msg.delete()
    except:
        pass
    
    await send_results(update, USER_SESSIONS[user_id], is_callback=False)


# ═══════════════════════════════════════════════
# Callback Handler
# ═══════════════════════════════════════════════
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data
    
    logger.info(f"🔔 CALLBACK: '{data}' from user {user_id}")
    
    try:
        await query.answer()
    except Exception as e:
        logger.error(f"query.answer() failed: {e}")
    
    try:
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
                await send_results(update, session, is_callback=True)
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
                await send_results(update, session, is_callback=True)
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
        
        # Mode
        elif data == "mode:detailed":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            session["view_mode"] = "detailed"
            session["page"] = 0
            update_session(user_id, session)
            await send_results(update, session, is_callback=True)
        
        elif data == "mode:preview":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            session["view_mode"] = "preview"
            session["page"] = 0
            update_session(user_id, session)
            await send_results(update, session, is_callback=True)
        
        # Filter
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
                await send_results(update, session, is_callback=True)
            else:
                await query.answer("⚠️ គ្មានលទ្ធផល", show_alert=True)
                session["results"] = session["all_results"]
                session["filter"] = "all"
                update_session(user_id, session)
        
        # Quick search
        elif data.startswith("quick:"):
            search_term = data.split(":", 1)[1]
            status = await query.message.reply_text(
                f"🔍 កំពុងស្វែងរក: <b>{escape_html(search_term)}</b>...",
                parse_mode=ParseMode.HTML
            )
            data_result = search_law(search_term)
            try:
                await status.delete()
            except:
                pass
            
            if data_result.get("success") and data_result.get("count", 0) > 0:
                sorted_results = sort_results_by_article(data_result.get("results", []))
                session_data = {
                    "results": sorted_results,
                    "all_results": sorted_results,
                    "page": 0,
                    "query": search_term,
                    "mode": "search",
                    "view_mode": "preview",
                    "keywords": data_result.get("keywords", []),
                    "filter": "all"
                }
                update_session(user_id, session_data)
                
                pagination = paginate_results(sorted_results, 0)
                page_data = {"success": True, "results": pagination["results"]}
                text = format_preview_mode(page_data, USER_SESSIONS[user_id], pagination)
                kb = build_navigation_keyboard(USER_SESSIONS[user_id])
                
                parts = smart_split_html(text, 3800)
                for i, part in enumerate(parts):
                    is_last = (i == len(parts) - 1)
                    await query.message.reply_text(
                        part, parse_mode=ParseMode.HTML,
                        reply_markup=kb if is_last else None
                    )
                    if i < len(parts) - 1:
                        await asyncio.sleep(0.3)
            else:
                await query.message.reply_text(
                    f"🔍 រកមិនឃើញ <b>{escape_html(search_term)}</b>",
                    parse_mode=ParseMode.HTML
                )
        
        # Save PDF
        elif data == "action:save_pdf":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            
            await query.answer("📄 កំពុងបង្កើត PDF...", show_alert=False)
            status = await query.message.reply_text(
                "📄 <b>កំពុងបង្កើត PDF...</b>\n\nសូមរង់ចាំ 10-30 វិនាទី...",
                parse_mode=ParseMode.HTML
            )
            
            try:
                pdf_buffer = generate_pdf(session)
                
                query_safe = re.sub(r'[^\w\u1780-\u17FF]', '_', session.get("query", "law"))[:30]
                date_str = datetime.now().strftime("%Y%m%d_%H%M")
                filename = f"ច្បាប់_{query_safe}_{date_str}.pdf"
                
                try:
                    await status.delete()
                except:
                    pass
                
                await query.message.reply_document(
                    document=pdf_buffer,
                    filename=filename,
                    caption=(
                        f"📄 <b>PDF - {escape_html(session.get('query', ''))}</b>\n\n"
                        f"📊 {len(session.get('all_results', []))} មាត្រា\n"
                        f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                        f"💾 <i>រក្សាទុកសម្រាប់មើលថ្ងៃក្រោយ</i>"
                    ),
                    parse_mode=ParseMode.HTML
                )
                logger.info(f"✅ PDF sent to user {user_id}")
            except Exception as e:
                logger.error(f"❌ PDF error: {e}", exc_info=True)
                try:
                    await status.delete()
                except:
                    pass
                await query.message.reply_text(f"❌ បង្កើត PDF ខុស: {str(e)[:100]}")
        
        # Actions
        elif data == "action:new_search":
            await query.message.reply_text(
                "🔍 <b>សូមវាយសំណួរថ្មី</b>\n\n"
                "ឧទាហរណ៍:\n"
                "  • <code>លួច</code>\n"
                "  • <code>មាត្រា ៥៥</code>",
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
                "🔍 <b>ស្វែងរក:</b>\n"
                "  <code>លួច</code>\n"
                "  <code>មាត្រា ៥៥</code>\n\n"
                "🎯 <b>ប៊ូតុង:</b>\n"
                "  👁 មើលពេញ | 📋 សង្ខេប\n"
                "  💾 រក្សា PDF\n"
                "  🔴🔵🟢 Filter\n\n"
                "🎨 <b>ពណ៌:</b>\n"
                "  🔴 ព្រហ្មទណ្ឌ\n"
                "  🔵 រដ្ឋប្បវេណី\n"
                "  🟢 ផ្សេងៗ"
            )
            await query.message.reply_text(msg, parse_mode=ParseMode.HTML)
        
        elif data == "action:docs":
            status = await query.message.reply_text("📚 កំពុងទាញ...")
            docs_data = list_docs()
            try:
                await status.delete()
            except:
                pass
            if not docs_data.get("success"):
                await query.message.reply_text(f"❌ {docs_data.get('error')}")
                return
            docs = docs_data.get("documents", [])
            msg = f"📚 <b>ឯកសារ {len(docs)}៖</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
            for i, d in enumerate(docs, 1):
                cat = get_law_category(d['name'])
                msg += f"{cat['emoji']} {cat['icon']} <b>{escape_html(d['name'])}</b>\n"
                msg += f"   <i>{d['size']:,} តួអក្សរ</i>\n\n"
            await query.message.reply_text(msg, parse_mode=ParseMode.HTML)
        
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
        "🔥 <b>ស្វែងរកពេញនិយម:</b>\n"
        "👇 ចុចប៊ូតុងខាងក្រោម ឬវាយសំណួរផ្ទាល់\n\n"
        "💾 <b>ថ្មី!</b> អ្នកអាចរក្សា PDF ទុកមើលថ្ងៃក្រោយ"
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.HTML,
        reply_markup=build_start_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 <b>ជំនួយ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 <code>លួច</code> - ស្វែងរក\n"
        "🔍 <code>មាត្រា ៥៥</code> - មាត្រា\n\n"
        "🎯 <b>ប៊ូតុង:</b>\n"
        "  👁 មើលពេញ | 📋 សង្ខេប\n"
        "  💾 រក្សា PDF\n"
        "  🔴🔵🟢 Filter"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_SESSIONS.pop(update.effective_user.id, None)
    save_sessions()
    await update.message.reply_text("✅ លុប session")


async def docs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 កំពុងទាញ...")
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
        self.wfile.write(b"Bot v15 running")
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
    logger.info("🤖 Bot v15 (Compact + WeasyPrint PDF)")
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
