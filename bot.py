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

# ⭐ PDF Libraries
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, Image, HRFlowable, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GAS_URL = os.getenv("GAS_URL")

RESULTS_PER_PAGE = 5
SESSIONS_FILE = "user_sessions.json"  # ⭐ Persistent storage
FONTS_DIR = "fonts"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

USER_SESSIONS = {}


# ═══════════════════════════════════════════════
# ⭐ NEW: Session Persistence
# ═══════════════════════════════════════════════
def load_sessions():
    """Load sessions ពី file ពេល bot ចាប់ផ្តើម"""
    global USER_SESSIONS
    try:
        if os.path.exists(SESSIONS_FILE):
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Convert string keys back to int
                USER_SESSIONS = {int(k): v for k, v in data.items()}
                logger.info(f"✅ Loaded {len(USER_SESSIONS)} sessions from file")
        else:
            logger.info("ℹ️ No existing sessions file")
    except Exception as e:
        logger.error(f"❌ Load sessions error: {e}")
        USER_SESSIONS = {}


def save_sessions():
    """Save sessions ទៅ file"""
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            # Convert int keys to string for JSON
            data = {str(k): v for k, v in USER_SESSIONS.items()}
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Saved {len(USER_SESSIONS)} sessions")
    except Exception as e:
        logger.error(f"❌ Save sessions error: {e}")


def update_session(user_id, session_data):
    """Update + save session"""
    USER_SESSIONS[user_id] = session_data
    save_sessions()


# ═══════════════════════════════════════════════
# ⭐ NEW: PDF Font Registration
# ═══════════════════════════════════════════════
def register_khmer_fonts():
    """Register Khmer fonts for PDF"""
    try:
        khmer_regular = os.path.join(FONTS_DIR, "KhmerOS.ttf")
        khmer_bold = os.path.join(FONTS_DIR, "KhmerOSMuol.ttf")
        
        if os.path.exists(khmer_regular):
            pdfmetrics.registerFont(TTFont("KhmerOS", khmer_regular))
            logger.info("✅ Registered KhmerOS font")
        else:
            logger.warning(f"⚠️ Font not found: {khmer_regular}")
        
        if os.path.exists(khmer_bold):
            pdfmetrics.registerFont(TTFont("KhmerOSMuol", khmer_bold))
            logger.info("✅ Registered KhmerOSMuol font")
        else:
            logger.warning(f"⚠️ Font not found: {khmer_bold}")
    except Exception as e:
        logger.error(f"❌ Font registration error: {e}")


# ═══════════════════════════════════════════════
# Category Detection
# ═══════════════════════════════════════════════
def get_law_category(doc_name):
    if not doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "📄", "label": "ច្បាប់", "color": colors.HexColor("#10B981")}
    
    if "នីតិវិធីព្រហ្មទណ្ឌ" in doc_name:
        return {"category": "criminal", "emoji": "🔴", "icon": "👮", "label": "នីតិវិធីព្រហ្មទណ្ឌ", "color": colors.HexColor("#DC2626")}
    if "ព្រហ្មទណ្ឌ" in doc_name or "ព្រហ្មទណ្ឍ" in doc_name:
        return {"category": "criminal", "emoji": "🔴", "icon": "⚖️", "label": "ព្រហ្មទណ្ឌ", "color": colors.HexColor("#DC2626")}
    if "នីតិវិធីរដ្ឋប្បវេណី" in doc_name:
        return {"category": "civil", "emoji": "🔵", "icon": "⚖️", "label": "នីតិវិធីរដ្ឋប្បវេណី", "color": colors.HexColor("#2563EB")}
    if "រដ្ឋប្បវេណី" in doc_name:
        return {"category": "civil", "emoji": "🔵", "icon": "📜", "label": "រដ្ឋប្បវេណី", "color": colors.HexColor("#2563EB")}
    if "ការងារ" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "💼", "label": "ការងារ", "color": colors.HexColor("#059669")}
    if "គ្រួសារ" in doc_name or "អាពាហ៍" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "👨‍👩‍👧", "label": "គ្រួសារ", "color": colors.HexColor("#059669")}
    if "ចរាចរណ៍" in doc_name:
        return {"category": "other", "emoji": "🟢", "icon": "🚗", "label": "ចរាចរណ៍", "color": colors.HexColor("#059669")}
    
    return {"category": "other", "emoji": "🟢", "icon": "📄", "label": doc_name, "color": colors.HexColor("#10B981")}


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


def _split_into_paragraphs(text, max_sentences_per_para=2):
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
        if len(current) >= max_sentences_per_para or total_len > 250:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def format_body_paragraphs(body, indent="    "):
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
            formatted_lines.append(f"\n<b>▸ {escape_html(line)}</b>\n")
        else:
            paragraphs = _split_into_paragraphs(line)
            for para in paragraphs:
                if para.strip():
                    formatted_lines.append(f"{indent}{escape_html(para.strip())}")
    return "\n\n".join(formatted_lines)


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
# Format Preview + Detailed
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
    
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    filter_active = session.get("filter", "all")
    if filter_active == "all" and total_docs > 1:
        msg += "📚 <b>ច្បាប់ទាំងអស់:</b>\n"
        for doc_name, doc_results in all_groups.items():
            cat = get_law_category(doc_name)
            in_page = "👁" if doc_name in page_groups else ""
            msg += f"  {cat['emoji']} {escape_html(doc_name)} ({len(doc_results)}) {in_page}\n"
        msg += "\n"
    
    for doc_name, doc_results in page_groups.items():
        cat = get_law_category(doc_name)
        msg += f"{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b>\n"
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
        msg += "\n"
    
    msg += "👆 <i>ចុចប៊ូតុងខាងក្រោមដើម្បីមើលពេញ ឬ 💾 រក្សាទុក PDF</i>"
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
    if pagination_info:
        msg += f" | 📄 <b>{pagination_info['current_page']}/{pagination_info['total_pages']}</b>"
    msg += "\n━━━━━━━━━━━━━━━━━━━━\n"
    
    page_groups = group_results_by_document(results)
    doc_list = list(page_groups.keys())
    
    for doc_idx, (doc_name, doc_results) in enumerate(page_groups.items()):
        cat = get_law_category(doc_name)
        total_in_doc = len(all_groups.get(doc_name, []))
        current_in_page = len(doc_results)
        
        if total_in_doc > current_in_page:
            msg += f"\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b> ({current_in_page}/{total_in_doc})\n"
        else:
            msg += f"\n{cat['emoji']} {cat['icon']} <b>{escape_html(doc_name)}</b> ({total_in_doc})\n"
        
        for r_idx, r in enumerate(doc_results):
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, body = _split_title_and_body(content, article)
            
            msg += "\n"
            if article and title:
                msg += f"📌 <b>មាត្រា {escape_html(str(article))} - {escape_html(title)}</b>\n"
            elif article:
                msg += f"📌 <b>មាត្រា {escape_html(str(article))}</b>\n"
            
            if body:
                formatted_body = format_body_paragraphs(body, indent="    ")
                if keywords:
                    formatted_body = highlight_keywords_html(formatted_body, keywords)
                msg += f"{formatted_body}\n"
            
            if r_idx < len(doc_results) - 1:
                msg += "\n▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
        
        if doc_idx < len(doc_list) - 1:
            msg += "\n━━━━━━━━━━━━━━━━━━━━\n"
    
    return msg


# ═══════════════════════════════════════════════
# ⭐ NEW: PDF Generation
# ═══════════════════════════════════════════════
def generate_pdf(session):
    """បង្កើត PDF ស្អាតៗ ពី session"""
    buffer = BytesIO()
    
    # Check fonts
    try:
        pdfmetrics.getFont("KhmerOS")
        khmer_font = "KhmerOS"
    except:
        khmer_font = "Helvetica"
        logger.warning("⚠️ Using Helvetica (Khmer font not available)")
    
    try:
        pdfmetrics.getFont("KhmerOSMuol")
        khmer_bold = "KhmerOSMuol"
    except:
        khmer_bold = "Helvetica-Bold"
    
    # Create document
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=2*cm,
        leftMargin=2*cm,
        topMargin=2*cm,
        bottomMargin=2.5*cm,
        title=f"ច្បាប់កម្ពុជា - {session.get('query', '')}",
        author="Cambodia Law Bot"
    )
    
    # ⭐ Styles
    styles = getSampleStyleSheet()
    
    # Title style
    title_style = ParagraphStyle(
        'KhmerTitle',
        parent=styles['Title'],
        fontName=khmer_bold,
        fontSize=22,
        textColor=colors.HexColor("#1E293B"),
        alignment=TA_CENTER,
        spaceAfter=12,
        leading=28
    )
    
    # Subtitle
    subtitle_style = ParagraphStyle(
        'KhmerSubtitle',
        parent=styles['Normal'],
        fontName=khmer_font,
        fontSize=12,
        textColor=colors.HexColor("#64748B"),
        alignment=TA_CENTER,
        spaceAfter=6,
        leading=16
    )
    
    # Document header (ក្រម)
    doc_header_style = ParagraphStyle(
        'DocHeader',
        parent=styles['Heading1'],
        fontName=khmer_bold,
        fontSize=16,
        textColor=colors.white,
        backColor=colors.HexColor("#DC2626"),
        alignment=TA_LEFT,
        borderPadding=10,
        spaceBefore=15,
        spaceAfter=10,
        leading=22,
        leftIndent=0,
        rightIndent=0
    )
    
    # Article header
    article_style = ParagraphStyle(
        'Article',
        parent=styles['Heading2'],
        fontName=khmer_bold,
        fontSize=13,
        textColor=colors.HexColor("#1E293B"),
        backColor=colors.HexColor("#FEF3C7"),
        alignment=TA_LEFT,
        borderPadding=8,
        spaceBefore=10,
        spaceAfter=6,
        leading=18,
        leftIndent=0,
        borderColor=colors.HexColor("#F59E0B"),
        borderWidth=0
    )
    
    # Body paragraph
    body_style = ParagraphStyle(
        'Body',
        parent=styles['Normal'],
        fontName=khmer_font,
        fontSize=11,
        textColor=colors.HexColor("#334155"),
        alignment=TA_JUSTIFY,
        spaceBefore=4,
        spaceAfter=8,
        leading=18,
        firstLineIndent=20,
        leftIndent=10,
        rightIndent=5
    )
    
    # Sub-header (ជំពូក)
    subheader_style = ParagraphStyle(
        'SubHeader',
        parent=styles['Normal'],
        fontName=khmer_bold,
        fontSize=12,
        textColor=colors.HexColor("#7C3AED"),
        alignment=TA_LEFT,
        spaceBefore=6,
        spaceAfter=4,
        leading=16
    )
    
    # Footer info
    info_style = ParagraphStyle(
        'Info',
        parent=styles['Normal'],
        fontName=khmer_font,
        fontSize=9,
        textColor=colors.HexColor("#94A3B8"),
        alignment=TA_CENTER,
        leading=12
    )
    
    # ⭐ Build content
    story = []
    
    # ─────── HEADER ───────
    story.append(Paragraph("🇰🇭 ច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា", title_style))
    story.append(Paragraph("Cambodia Law Reference", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#DC2626")))
    story.append(Spacer(1, 0.5*cm))
    
    # ─────── QUERY INFO ───────
    all_results = session.get("all_results", [])
    all_groups = group_results_by_document(all_results)
    total_docs = len(all_groups)
    total_articles = len(all_results)
    query = session.get("query", "")
    date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    
    # Info table
    info_data = [
        ["សំណួរស្វែងរក:", query],
        ["ចំនួនមាត្រា:", f"{total_articles} មាត្រា"],
        ["ចំនួនច្បាប់:", f"{total_docs} ច្បាប់"],
        ["កាលបរិច្ឆេទ:", date_str],
    ]
    
    info_table = Table(info_data, colWidths=[4*cm, 12*cm])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), khmer_bold),
        ('FONTNAME', (1, 0), (1, -1), khmer_font),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor("#475569")),
        ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor("#0F172A")),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F1F5F9")),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.5*cm))
    
    # ─────── TABLE OF CONTENTS ───────
    story.append(Paragraph("📋 <b>មាតិកា</b>", doc_header_style))
    
    toc_data = [["#", "ច្បាប់", "ចំនួនមាត្រា"]]
    for i, (doc_name, doc_results) in enumerate(all_groups.items(), 1):
        cat = get_law_category(doc_name)
        toc_data.append([str(i), f"{cat['label']} - {doc_name}", f"{len(doc_results)} មាត្រា"])
    
    toc_table = Table(toc_data, colWidths=[1*cm, 12*cm, 3*cm])
    toc_table.setStyle(TableStyle([
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), khmer_bold),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        # Data rows
        ('FONTNAME', (0, 1), (-1, -1), khmer_font),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('TEXTCOLOR', (0, 1), (-1, -1), colors.HexColor("#334155")),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor("#FAFAFA")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor("#FAFAFA"), colors.white]),
        ('ALIGN', (0, 1), (0, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
    ]))
    story.append(toc_table)
    story.append(PageBreak())
    
    # ─────── CONTENT ───────
    for doc_idx, (doc_name, doc_results) in enumerate(all_groups.items()):
        cat = get_law_category(doc_name)
        
        # Custom doc header with color
        colored_doc_style = ParagraphStyle(
            f'DocHeader_{doc_idx}',
            parent=doc_header_style,
            backColor=cat["color"]
        )
        story.append(Paragraph(f"{cat['label']} - {doc_name}", colored_doc_style))
        story.append(Paragraph(f"ចំនួន: {len(doc_results)} មាត្រា", info_style))
        story.append(Spacer(1, 0.3*cm))
        
        # Articles
        for r_idx, r in enumerate(doc_results):
            article = r.get("article", "")
            content = clean_content(r.get("content", ""))
            title, body = _split_title_and_body(content, article)
            
            # Article header
            if article and title:
                article_text = f"📌 មាត្រា {article} - {title}"
            elif article:
                article_text = f"📌 មាត្រា {article}"
            else:
                article_text = "📌 ខ្លឹមសារ"
            
            # Keep article + body together (avoid split across pages)
            article_block = []
            article_block.append(Paragraph(article_text, article_style))
            
            # Body paragraphs
            if body:
                body_lines = body.split("\n")
                for line in body_lines:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # Check sub-header
                    is_subheader = bool(re.match(
                        r'^(ជំពូកទី|ផ្នែកទី|វិភាគទី|ផ្នែក)',
                        line
                    ))
                    
                    if is_subheader:
                        article_block.append(Paragraph(f"▸ {line}", subheader_style))
                    else:
                        # Split into paragraphs
                        paragraphs = _split_into_paragraphs(line)
                        for para in paragraphs:
                            if para.strip():
                                article_block.append(Paragraph(para.strip(), body_style))
            
            # Try to keep article together
            try:
                story.append(KeepTogether(article_block))
            except:
                for block in article_block:
                    story.append(block)
            
            # Separator between articles
            if r_idx < len(doc_results) - 1:
                story.append(Spacer(1, 0.2*cm))
                story.append(HRFlowable(
                    width="30%",
                    thickness=0.5,
                    color=colors.HexColor("#CBD5E1"),
                    hAlign='CENTER'
                ))
                story.append(Spacer(1, 0.2*cm))
        
        # Page break between documents
        if doc_idx < len(all_groups) - 1:
            story.append(PageBreak())
    
    # ─────── FOOTER ───────
    story.append(Spacer(1, 1*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#DC2626")))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(
        "ឯកសារនេះបានបង្កើតដោយស្វ័យប្រវត្តិពី Cambodia Law Bot",
        info_style
    ))
    story.append(Paragraph(
        f"បង្កើតនៅ: {date_str}",
        info_style
    ))
    
    # ⭐ Build PDF with page numbers
    def add_page_number(canvas_obj, doc):
        canvas_obj.saveState()
        canvas_obj.setFont(khmer_font, 9)
        canvas_obj.setFillColor(colors.HexColor("#94A3B8"))
        
        # Page number
        page_text = f"ទំព័រទី {doc.page}"
        canvas_obj.drawCentredString(A4[0] / 2, 1.5*cm, page_text)
        
        # Header
        canvas_obj.setFillColor(colors.HexColor("#DC2626"))
        canvas_obj.setFont(khmer_bold, 8)
        canvas_obj.drawString(2*cm, A4[1] - 1*cm, "🇰🇭 ច្បាប់កម្ពុជា")
        canvas_obj.drawRightString(A4[0] - 2*cm, A4[1] - 1*cm, session.get("query", ""))
        
        # Line under header
        canvas_obj.setStrokeColor(colors.HexColor("#DC2626"))
        canvas_obj.setLineWidth(0.5)
        canvas_obj.line(2*cm, A4[1] - 1.2*cm, A4[0] - 2*cm, A4[1] - 1.2*cm)
        
        canvas_obj.restoreState()
    
    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)
    
    buffer.seek(0)
    return buffer


# ═══════════════════════════════════════════════
# Inline Keyboard
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


# ═══════════════════════════════════════════════
# ⭐ NEW: Session recovery helper
# ═══════════════════════════════════════════════
async def try_recover_session(update, is_callback=True):
    """បើ session បាត់ → guide user"""
    if is_callback:
        target = update.callback_query.message
    else:
        target = update.message
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 ត្រឡប់ Home", callback_data="action:home")
    ]])
    
    await target.reply_text(
        "⚠️ <b>Session បាត់</b>\n\n"
        "Session របស់អ្នកបានបាត់ (bot ភ្ញាក់ពីដេក)។\n"
        "សូមស្វែងរកម្តងទៀត។\n\n"
        "💡 <b>ឧទាហរណ៍:</b>\n"
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
            f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់ <b>{escape_html(query)}</b>\n\n"
            f"💡 សូមសាកសំណួរផ្សេង",
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
    update_session(user_id, session_data)  # ⭐ Persist
    
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
        # ─── Navigation ───
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
        
        # ─── Mode ───
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
        
        # ─── Filter ───
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
        
        # ─── Quick search ───
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
        
        # ─── ⭐ NEW: Save PDF ───
        elif data == "action:save_pdf":
            session = USER_SESSIONS.get(user_id)
            if not session:
                await try_recover_session(update, is_callback=True)
                return
            
            await query.answer("📄 កំពុងបង្កើត PDF...", show_alert=False)
            status = await query.message.reply_text("📄 កំពុងបង្កើត PDF... សូមរង់ចាំ...")
            
            try:
                pdf_buffer = generate_pdf(session)
                
                # Filename
                query_safe = re.sub(r'[^\w\u1780-\u17FF]', '_', session.get("query", "law"))[:30]
                date_str = datetime.now().strftime("%Y%m%d_%H%M")
                filename = f"ច្បាប់_{query_safe}_{date_str}.pdf"
                
                try:
                    await status.delete()
                except:
                    pass
                
                # Send PDF
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
                logger.error(f"❌ PDF generation error: {e}", exc_info=True)
                try:
                    await status.delete()
                except:
                    pass
                await query.message.reply_text(f"❌ បង្កើត PDF ខុស: {str(e)[:100]}")
        
        # ─── Actions ───
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
                "  👁 មើលពេញ\n"
                "  📋 មើលសង្ខេប\n"
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
        self.wfile.write(b"Bot v14 running")
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
    logger.info("🤖 Bot v14 (Persistent Sessions + PDF)")
    logger.info("=" * 50)
    
    # ⭐ Register fonts
    register_khmer_fonts()
    
    # ⭐ Load sessions
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
