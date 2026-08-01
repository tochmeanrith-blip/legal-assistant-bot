import os
import re
import logging
import threading
import asyncio
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GAS_URL = os.getenv("GAS_URL")

# Gemini Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
USE_GEMINI = os.getenv("USE_GEMINI", "true").lower() == "true"

# ✨ NEW: Pagination Config
RESULTS_PER_PAGE = 20

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ✨ NEW: User Sessions (in-memory storage)
# Format: {user_id: {"results": [...], "page": 0, "query": "...", "mode": "search"}}
USER_SESSIONS = {}


# ===================== Apps Script API =====================
def call_gas(payload, timeout=60):
    try:
        logger.info(f"→ GAS: {payload}")
        response = requests.post(GAS_URL, json=payload, timeout=timeout, allow_redirects=True)
        logger.info(f"← Status: {response.status_code}, Size: {len(response.text)}")
        
        if response.status_code != 200:
            return {"success": False, "error": f"HTTP {response.status_code}"}
        return response.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Timeout"}
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


# ===================== ✨ NEW: Sort Results by Article Number =====================
def khmer_to_arabic_num(s):
    """Convert Khmer digits to Arabic for sorting"""
    khmer_map = {"០":"0","១":"1","២":"2","៣":"3","៤":"4",
                 "៥":"5","៦":"6","៧":"7","៨":"8","៩":"9"}
    result = ""
    for c in str(s):
        result += khmer_map.get(c, c)
    return result


def get_article_sort_key(result):
    """
    ត្រឡប់ tuple (doc_name, article_number_as_int)
    សម្រាប់ sort តម្រៀបតាមឯកសារ + លេខមាត្រា
    """
    doc = result.get("document", "")
    article = result.get("article", "")
    
    if not article:
        return (doc, 999999)  # មិនមានលេខ → ដាក់ចុងគេ
    
    # Convert Khmer → Arabic
    arabic = khmer_to_arabic_num(article)
    
    # Extract number only
    match = re.search(r'\d+', arabic)
    if match:
        return (doc, int(match.group()))
    return (doc, 999999)


def sort_results_by_article(results):
    """
    តម្រៀបលទ្ធផលតាមឯកសារ + លេខមាត្រា (តូច → ធំ)
    """
    return sorted(results, key=get_article_sort_key)


# ===================== ✨ NEW: Pagination =====================
def paginate_results(results, page=0, per_page=RESULTS_PER_PAGE):
    """
    បំបែកលទ្ធផលតាម page
    Returns: (page_results, total_pages, current_page, has_next)
    """
    total = len(results)
    total_pages = (total + per_page - 1) // per_page  # ceil
    
    start = page * per_page
    end = start + per_page
    page_results = results[start:end]
    
    has_next = (page + 1) < total_pages
    has_prev = page > 0
    
    return {
        "results": page_results,
        "total": total,
        "total_pages": total_pages,
        "current_page": page + 1,  # 1-indexed for display
        "has_next": has_next,
        "has_prev": has_prev,
        "start_idx": start + 1,
        "end_idx": min(end, total)
    }


# ===================== Convert to Markdown =====================
def convert_to_markdown(data, mode="search", pagination_info=None):
    """
    បំលែងទិន្នន័យ raw ពី GAS ទៅជា Markdown format
    """
    if not data.get("success"):
        return f"❌ Error: {data.get('error', 'Unknown')}"
    
    results = data.get("results", [])
    if not results:
        query = data.get("query") or data.get("article", "")
        return f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់៖ **{query}**"
    
    md = ""
    
    # Header
    if mode == "search":
        query = data.get("query", "")
        keywords = data.get("keywords", [])
        md += f"# 🔍 លទ្ធផលស្វែងរក: {query}\n\n"
        if keywords:
            md += f"**Keywords:** `{', '.join(keywords)}`\n\n"
        
        if pagination_info:
            md += (
                f"**បង្ហាញ:** លទ្ធផលទី {pagination_info['start_idx']}-{pagination_info['end_idx']} "
                f"ក្នុងចំណោម {pagination_info['total']}\n"
                f"**ទំព័រ:** {pagination_info['current_page']}/{pagination_info['total_pages']}\n\n"
            )
        else:
            md += f"**រកឃើញ:** {len(results)} លទ្ធផល\n\n"
    else:
        article = data.get("article", "")
        md += f"# ⚖️ មាត្រា {article}\n\n"
        md += f"**រកឃើញ:** {len(results)} កន្លែង\n\n"
    
    md += "---\n\n"
    
    # Each Result
    for i, r in enumerate(results, 1):
        doc_name = r.get("document", "N/A")
        article = r.get("article", "")
        content = r.get("content", "").strip()
        
        # Use global index if pagination
        display_idx = pagination_info["start_idx"] + i - 1 if pagination_info else i
        total_display = pagination_info["total"] if pagination_info else len(results)
        
        md += f"## 📖 លទ្ធផលទី {display_idx}/{total_display}\n\n"
        md += f"**ឯកសារ:** {doc_name}\n"
        if article:
            md += f"**មាត្រា:** {article}\n"
        md += "\n"
        
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = re.sub(r'^\s+', '', content, flags=re.MULTILINE)
        content = content.strip()
        
        md += f"{content}\n\n"
        
        if i < len(results):
            md += "---\n\n"
    
    # Footer
    md += "\n---\n"
    md += "*ប្រភព: ឯកសារច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា*\n"
    
    return md


# ===================== Gemini Integration =====================
def format_with_gemini(markdown_text, query, mode="search"):
    """
    ផ្ញើ Markdown ទៅ Gemini សម្រាប់រៀបចំ Format
    """
    if not GEMINI_API_KEY:
        logger.warning("⚠️ GEMINI_API_KEY not set, using markdown")
        return markdown_text
    
    if not markdown_text or len(markdown_text) < 20:
        return markdown_text
    
    try:
        # ✨ UPDATED: Enhanced system prompt with sorting instruction
        system_prompt = (
            "អ្នកជាជំនួយការផ្នែកច្បាប់កម្ពុជា មានតួនាទីរៀបចំ Format អត្ថបទច្បាប់សម្រាប់ Telegram។\n\n"
            "ច្បាប់ដែលត្រូវអនុវត្តតាមយ៉ាងតឹងរឹង:\n"
            "១. រក្សាខ្លឹមសារច្បាប់ ១០០% — មិនត្រូវផ្លាស់ប្តូរ, កាត់, ឬបន្ថែមខ្លឹមសារ\n"
            "២. រៀបចំ Format ឲ្យអានងាយ មានលំដាប់លំដោយ\n"
            "៣. ⚠️ សំខាន់: រក្សាលំដាប់មាត្រាដដែល (មិនត្រូវតម្រៀបឡើងវិញ)\n"
            "    - លទ្ធផលបានតម្រៀបជាមុនហើយ តាមឯកសារ + លេខមាត្រា\n"
            "    - អ្នកគ្រាន់តែ format ស្អាត កុំរៀបលំដាប់ថ្មី\n"
            "៤. ប្រើ Emoji សមស្រប (⚖️ 📋 🔹 📌 ✅ ⚠️ 📖 📄)\n"
            "៥. បំបែក Paragraph ឲ្យច្បាស់\n"
            "៦. ឆ្លើយជាភាសាខ្មែរ\n"
            "៧. កុំប្រើ Markdown syntax (** __ ##) — ប្រើអក្សរធម្មតា + emoji\n"
            "៨. រក្សា structure: ឯកសារ → មាត្រា → ខ្លឹមសារ\n"
            "៩. កុំបន្ថែម disclaimer, note, ឬការណែនាំបន្ថែម\n"
            "១០. រក្សាលេខមាត្រា និងឈ្មោះឯកសារឲ្យដដែល\n"
            "១១. រក្សាលេខ 'លទ្ធផលទី X/Y' ដដែល\n"
            "១២. ប្រើ separator (▬▬▬▬▬▬▬▬▬▬) បំបែក sections"
        )
        
        if mode == "search":
            user_prompt = (
                f"សំណួរអ្នកប្រើ: {query}\n\n"
                f"ទិន្នន័យច្បាប់ (បានតម្រៀបតាមលេខមាត្រាហើយ):\n"
                f"```markdown\n{markdown_text}\n```\n\n"
                f"សូមរៀបចំទិន្នន័យខាងលើឲ្យស្អាត អានងាយ។\n"
                f"⚠️ សំខាន់: រក្សាលំដាប់មាត្រាដដែល មិនត្រូវរៀបឡើងវិញ។\n"
                f"រក្សាខ្លឹមសារ ១០០% ត្រូវ។"
            )
        else:
            user_prompt = (
                f"ស្វែងរក: មាត្រា {query}\n\n"
                f"ទិន្នន័យ (Markdown):\n"
                f"```markdown\n{markdown_text}\n```\n\n"
                f"សូមរៀបចំបង្ហាញឲ្យស្អាត អានងាយ រក្សាខ្លឹមសារ ១០០% ត្រូវ។"
            )
        
        payload = {
            "contents": [{
                "role": "user",
                "parts": [{"text": user_prompt}]
            }],
            "systemInstruction": {
                "parts": [{"text": system_prompt}]
            },
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 8192,
                "topP": 0.8,
                "topK": 10
            }
        }
        
        url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
        
        logger.info(f"→ Gemini: {mode}, input len={len(markdown_text)}")
        response = requests.post(
            url, json=payload, timeout=45,
            headers={"Content-Type": "application/json"}
        )
        
        logger.info(f"← Gemini status: {response.status_code}")
        
        if response.status_code != 200:
            logger.error(f"Gemini error: {response.text[:300]}")
            return markdown_text
        
        result = response.json()
        candidates = result.get("candidates", [])
        if not candidates:
            return markdown_text
        
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return markdown_text
        
        formatted = parts[0].get("text", "").strip()
        if not formatted:
            return markdown_text
        
        logger.info(f"✅ Gemini success: output len={len(formatted)}")
        return formatted
        
    except requests.exceptions.Timeout:
        logger.error("Gemini timeout — fallback")
        return markdown_text
    except Exception as e:
        logger.error(f"Gemini exception: {e}")
        return markdown_text


def format_with_gemini_chunked(markdown_text, query, mode="search", chunk_size=6000):
    if len(markdown_text) <= chunk_size:
        return format_with_gemini(markdown_text, query, mode)
    
    sections = markdown_text.split("\n---\n")
    chunks = []
    current = ""
    
    for section in sections:
        candidate = current + "\n---\n" + section if current else section
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = section
    
    if current:
        chunks.append(current)
    
    logger.info(f"Chunked into {len(chunks)} parts for Gemini")
    
    formatted_parts = []
    for i, chunk in enumerate(chunks):
        logger.info(f"Processing chunk {i+1}/{len(chunks)}")
        formatted = format_with_gemini(chunk, query, mode)
        formatted_parts.append(formatted)
    
    return "\n\n".join(formatted_parts)


# ===================== Format Fallback =====================
def format_search_results_combined(data):
    if not data.get("success"):
        return f"❌ Error: {data.get('error', 'Unknown')}"
    
    results = data.get("results", [])
    if not results:
        return f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់៖ {data.get('query', '')}"
    
    keywords = data.get("keywords", [])
    total = len(results)
    
    msg = f"🔍 លទ្ធផលស្វែងរក\n"
    if keywords:
        msg += f"🔑 Keywords: {', '.join(keywords)}\n"
    msg += f"📊 រកឃើញ៖ {total} លទ្ធផល\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for i, r in enumerate(results, 1):
        article = f"មាត្រា {r['article']}" if r.get("article") else ""
        content = r.get('content', '').replace("**", "").strip()
        
        msg += f"📌 លទ្ធផលទី {i}/{total}\n"
        msg += f"📖 {r['document']}\n"
        if article:
            msg += f"📄 {article}\n"
        msg += "─────────────────────\n"
        msg += content
        msg += "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    return msg


def format_article_results_combined(data):
    if not data.get("success"):
        return f"❌ Error: {data.get('error', 'Unknown')}"
    
    results = data.get("results", [])
    if not results:
        return f"🔍 រកមិនឃើញមាត្រា {data.get('article')}"
    
    msg = f"📄 មាត្រា {data.get('article')} ({len(results)} match)\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for i, r in enumerate(results, 1):
        content = r.get('content', '').replace("**", "").strip()
        msg += f"📖 ឯកសារ៖ {r['document']}\n"
        if len(results) > 1:
            msg += f"📄 លទ្ធផលទី {i}/{len(results)}\n"
        msg += "─────────────────────\n"
        msg += content
        msg += "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    return msg


# ===================== Smart Split =====================
def smart_split(text, max_length=4000):
    if len(text) <= max_length:
        return [text]
    
    separators = [
        "▬▬▬▬▬▬▬▬▬▬\n\n",
        "━━━━━━━━━━━━━━━━━━━━\n\n",
        "\n---\n\n",
        "\n---\n",
    ]
    
    for sep in separators:
        if sep in text:
            return _split_by_separator(text, sep, max_length)
    
    return _split_by_newline(text, max_length)


def _split_by_separator(text, separator, max_length):
    parts = []
    sections = text.split(separator)
    current = ""
    
    for i, section in enumerate(sections):
        section_with_sep = section + separator if i < len(sections) - 1 else section
        
        if len(current) + len(section_with_sep) <= max_length:
            current += section_with_sep
        else:
            if current:
                parts.append(current.rstrip())
                current = ""
            
            if len(section_with_sep) > max_length:
                sub_parts = _split_by_newline(section_with_sep, max_length)
                parts.extend(sub_parts[:-1])
                current = sub_parts[-1] if sub_parts else ""
            else:
                current = section_with_sep
    
    if current:
        parts.append(current.rstrip())
    
    return parts


def _split_by_newline(text, max_length):
    if len(text) <= max_length:
        return [text]
    
    lines = text.split('\n')
    parts = []
    current = ""
    
    for line in lines:
        if len(line) > max_length:
            if current:
                parts.append(current)
                current = ""
            for i in range(0, len(line), max_length):
                parts.append(line[i:i+max_length])
            continue
        
        if len(current) + len(line) + 1 > max_length:
            parts.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    
    if current:
        parts.append(current)
    return parts


async def send_long_message(update, text, delay=0.3):
    MAX_LEN = 4000
    
    if len(text) <= MAX_LEN:
        await update.message.reply_text(text)
        return
    
    parts = smart_split(text, MAX_LEN)
    total = len(parts)
    
    for i, part in enumerate(parts, 1):
        prefix = f"📄 (ភាគ {i}/{total})\n\n" if total > 1 else ""
        try:
            await update.message.reply_text(prefix + part)
            if i < total:
                await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Send error part {i}: {e}")
            if "429" in str(e) or "flood" in str(e).lower():
                await asyncio.sleep(5)
                try:
                    await update.message.reply_text(prefix + part)
                except:
                    pass


# ===================== ✨ NEW: Send Page with Navigation =====================
async def send_page_with_navigation(update, session):
    """
    ផ្ញើលទ្ធផលមួយ page ជាមួយ navigation instructions
    """
    all_results = session["results"]
    page = session["page"]
    query = session["query"]
    mode = session["mode"]
    
    # Get page
    pagination = paginate_results(all_results, page)
    
    # Build data structure for markdown
    page_data = {
        "success": True,
        "query": query,
        "article": query if mode == "article" else "",
        "keywords": session.get("keywords", []),
        "results": pagination["results"]
    }
    
    # Convert to Markdown
    markdown = convert_to_markdown(page_data, mode=mode, pagination_info=pagination)
    
    # Gemini format
    if USE_GEMINI and GEMINI_API_KEY:
        try:
            status_msg = await update.message.reply_text(
                f"🤖 កំពុងរៀបចំទំព័រ {pagination['current_page']}/{pagination['total_pages']}..."
            )
        except:
            status_msg = None
        
        final_text = format_with_gemini_chunked(markdown, query, mode=mode)
        
        if status_msg:
            try:
                await status_msg.delete()
            except:
                pass
    else:
        final_text = markdown
    
    # ✨ Add navigation footer
    nav_footer = "\n\n"
    nav_footer += "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
    nav_footer += f"📊 ទំព័រ {pagination['current_page']}/{pagination['total_pages']} "
    nav_footer += f"(លទ្ធផលទី {pagination['start_idx']}-{pagination['end_idx']}/{pagination['total']})\n\n"
    
    if pagination["has_next"]:
        nav_footer += "➡️ វាយ 'បន្ត' ឬ '/next' ដើម្បីមើលលទ្ធផលបន្ទាប់\n"
    if pagination["has_prev"]:
        nav_footer += "⬅️ វាយ 'ថយ' ឬ '/prev' ដើម្បីមើលលទ្ធផលមុន\n"
    if not pagination["has_next"] and not pagination["has_prev"]:
        nav_footer += "✅ បានបង្ហាញលទ្ធផលទាំងអស់\n"
    else:
        nav_footer += "🔍 វាយសំណួរថ្មីដើម្បីស្វែងរកម្តងទៀត\n"
    
    final_text += nav_footer
    
    await send_long_message(update, final_text)


# ===================== ✨ NEW: Process Query (with Session) =====================
async def process_search_query(update, query):
    """ដំណើរការសំណួរស្វែងរក — ជាមួយ Sort + Pagination"""
    user_id = update.effective_user.id
    
    status_msg = await update.message.reply_text("🔍 កំពុងស្វែងរក...")
    
    # 1️⃣ Call GAS
    data = search_law(query)
    total = data.get("count", 0)
    
    if not data.get("success") or total == 0:
        try:
            await status_msg.delete()
        except:
            pass
        text = format_search_results_combined(data)
        await send_long_message(update, text)
        # Clear session
        USER_SESSIONS.pop(user_id, None)
        return
    
    # 2️⃣ ✨ Sort results by article number
    all_results = data.get("results", [])
    sorted_results = sort_results_by_article(all_results)
    logger.info(f"📊 Sorted {len(sorted_results)} results by article number")
    
    # 3️⃣ ✨ Save session
    USER_SESSIONS[user_id] = {
        "results": sorted_results,
        "page": 0,
        "query": query,
        "mode": "search",
        "keywords": data.get("keywords", [])
    }
    
    # 4️⃣ Show info
    total_pages = (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    try:
        await status_msg.edit_text(
            f"🔍 រកឃើញ {total} លទ្ធផល\n"
            f"📊 បង្ហាញ {min(RESULTS_PER_PAGE, total)} ដំបូង "
            f"(ទំព័រ 1/{total_pages})\n"
            f"📝 កំពុងរៀបចំ..."
        )
    except:
        pass
    
    try:
        await status_msg.delete()
    except:
        pass
    
    # 5️⃣ Send first page
    await send_page_with_navigation(update, USER_SESSIONS[user_id])


async def process_article_query(update, article_num, doc_name=None):
    """ដំណើរការសំណួរមាត្រា"""
    user_id = update.effective_user.id
    
    status_msg = await update.message.reply_text(f"🔍 កំពុងស្វែងរកមាត្រា {article_num}...")
    
    data = find_article(article_num, doc_name)
    
    if not data.get("success") or not data.get("results"):
        try:
            await status_msg.delete()
        except:
            pass
        text = format_article_results_combined(data)
        await send_long_message(update, text)
        USER_SESSIONS.pop(user_id, None)
        return
    
    # Sort article results too (by document)
    all_results = data.get("results", [])
    sorted_results = sort_results_by_article(all_results)
    
    USER_SESSIONS[user_id] = {
        "results": sorted_results,
        "page": 0,
        "query": article_num,
        "mode": "article",
        "keywords": []
    }
    
    try:
        await status_msg.delete()
    except:
        pass
    
    await send_page_with_navigation(update, USER_SESSIONS[user_id])


# ===================== ✨ NEW: Navigation Commands =====================
async def next_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """បង្ហាញលទ្ធផលបន្ទាប់"""
    user_id = update.effective_user.id
    
    session = USER_SESSIONS.get(user_id)
    if not session:
        await update.message.reply_text(
            "⚠️ គ្មានលទ្ធផលមុន\n"
            "សូមស្វែងរកមុនសិន (ឧ. វាយ 'លួច')"
        )
        return
    
    total = len(session["results"])
    total_pages = (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    
    if session["page"] + 1 >= total_pages:
        await update.message.reply_text(
            f"✅ អ្នកនៅទំព័រចុងក្រោយ ({session['page'] + 1}/{total_pages})\n"
            f"គ្មានលទ្ធផលបន្ថែម"
        )
        return
    
    session["page"] += 1
    logger.info(f"User {user_id}: next page → {session['page'] + 1}/{total_pages}")
    
    await send_page_with_navigation(update, session)


async def prev_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """បង្ហាញលទ្ធផលមុន"""
    user_id = update.effective_user.id
    
    session = USER_SESSIONS.get(user_id)
    if not session:
        await update.message.reply_text("⚠️ គ្មានលទ្ធផលមុន")
        return
    
    if session["page"] <= 0:
        await update.message.reply_text("⚠️ អ្នកនៅទំព័រទី 1 ហើយ")
        return
    
    session["page"] -= 1
    logger.info(f"User {user_id}: prev page → {session['page'] + 1}")
    
    await send_page_with_navigation(update, session)


# ===================== Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gemini_status = "🟢 បើក" if USE_GEMINI and GEMINI_API_KEY else "🔴 បិទ"
    
    await update.message.reply_text(
        "សួស្តី! 🇰🇭\n"
        "ខ្ញុំគឺ Bot ស្វែងរកច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា\n\n"
        f"🤖 Gemini AI Format: {gemini_status}\n"
        f"📊 លទ្ធផល/ទំព័រ: {RESULTS_PER_PAGE}\n\n"
        "🔍 របៀបប្រើ:\n\n"
        "១. ស្វែងរកតាមពាក្យ:\n"
        "   លួច\n"
        "   មូលហេតុនៃទោស\n\n"
        "២. ស្វែងរកមាត្រា:\n"
        "   មាត្រា ៥ ព្រហ្មទណ្ឌ\n\n"
        "៣. Navigation:\n"
        "   'បន្ត' ឬ /next - លទ្ធផលបន្ទាប់\n"
        "   'ថយ' ឬ /prev - លទ្ធផលមុន\n\n"
        "៤. Commands:\n"
        "   /docs - បញ្ជីឯកសារ\n"
        "   /ping - test API\n"
        "   /help - ជំនួយ"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 ជំនួយ\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 ស្វែងរក:\n"
        "  លួច - keyword search\n"
        "  មាត្រា ៥ - article search\n"
        "  /article ១៨១ ព្រហ្មទណ្ឌ\n\n"
        "📊 Navigation (20 លទ្ធផល/ទំព័រ):\n"
        "  បន្ត ឬ /next - ទំព័របន្ទាប់\n"
        "  ថយ ឬ /prev - ទំព័រមុន\n\n"
        "📝 ការមើលទិន្នន័យ:\n"
        "  /raw <query> - raw format\n"
        "  /md <query> - Markdown\n\n"
        "🛠 Utilities:\n"
        "  /docs - បញ្ជីឯកសារ\n"
        "  /ping - test API\n"
        "  /clear - លុប session"
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """លុប session"""
    user_id = update.effective_user.id
    USER_SESSIONS.pop(user_id, None)
    await update.message.reply_text("✅ លុប session រួច")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 កំពុងសាកល្បង...")
    try:
        response = requests.get(GAS_URL, timeout=30)
        gemini_status = "✅ Configured" if GEMINI_API_KEY else "❌ Not set"
        active_sessions = len(USER_SESSIONS)
        await update.message.reply_text(
            f"📡 GAS Status: {response.status_code}\n"
            f"🤖 Gemini API: {gemini_status}\n"
            f"🎯 Model: {GEMINI_MODEL}\n"
            f"⚙️ Use Gemini: {USE_GEMINI}\n"
            f"📊 Results/Page: {RESULTS_PER_PAGE}\n"
            f"👥 Active Sessions: {active_sessions}\n\n"
            f"Response:\n{response.text[:300]}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def docs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 កំពុងទាញឯកសារ...")
    data = list_docs()
    if not data.get("success"):
        await update.message.reply_text(f"❌ {data.get('error')}")
        return
    
    docs = data.get("documents", [])
    msg = f"📚 ឯកសារ {len(docs)}៖\n\n"
    for i, d in enumerate(docs, 1):
        msg += f"{i}. {d['name']}\n   ({d['size']:,} តួអក្សរ)\n\n"
    await update.message.reply_text(msg)


async def article_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("សូមបញ្ជាក់លេខមាត្រា\nឧ.: /article ៥ ព្រហ្មទណ្ឌ")
        return
    
    article_num = args[0]
    doc_name = " ".join(args[1:]) if len(args) > 1 else None
    await process_article_query(update, article_num, doc_name)


async def raw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("ប្រើ: /raw <សំណួរ>")
        return
    
    query = " ".join(context.args)
    
    if query.startswith("មាត្រា"):
        parts = query.split()
        if len(parts) >= 2:
            article_num = parts[1]
            doc_name = " ".join(parts[2:]) if len(parts) > 2 else None
            await update.message.reply_text(f"🔍 (Raw) មាត្រា {article_num}...")
            data = find_article(article_num, doc_name)
            text = format_article_results_combined(data)
            await send_long_message(update, text)
            return
    
    await update.message.reply_text(f"🔍 (Raw) ស្វែងរក: {query}...")
    data = search_law(query)
    text = format_search_results_combined(data)
    await send_long_message(update, text)


async def md_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("ប្រើ: /md <សំណួរ>")
        return
    
    query = " ".join(context.args)
    await update.message.reply_text(f"📝 (Markdown) ស្វែងរក: {query}...")
    
    if query.startswith("មាត្រា"):
        parts = query.split()
        if len(parts) >= 2:
            article_num = parts[1]
            doc_name = " ".join(parts[2:]) if len(parts) > 2 else None
            data = find_article(article_num, doc_name)
            data["results"] = sort_results_by_article(data.get("results", []))
            markdown = convert_to_markdown(data, mode="article")
            await send_long_message(update, markdown)
            return
    
    data = search_law(query)
    data["results"] = sort_results_by_article(data.get("results", []))
    markdown = convert_to_markdown(data, mode="search")
    await send_long_message(update, markdown)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular text messages"""
    query = update.message.text.strip()
    
    # ✨ NEW: Navigation keywords
    query_lower = query.lower()
    
    # "បន្ត" or "next" → next page
    if query in ["បន្ត", "next", "Next", "NEXT", "->",  "→"]:
        await next_page(update, context)
        return
    
    # "ថយ" or "prev" → prev page
    if query in ["ថយ", "prev", "Prev", "PREV", "back", "Back", "<-", "←"]:
        await prev_page(update, context)
        return
    
    # Auto-detect article query
    article_match = re.match(r'^មាត្រា\s*([0-9០-៩]+)(.*)$', query)
    
    if article_match:
        article_num = article_match.group(1)
        doc_name = article_match.group(2).strip() or None
        await process_article_query(update, article_num, doc_name)
        return
    
    # Regular keyword search
    await process_search_query(update, query)


# ===================== HTTP Server =====================
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
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    logger.info(f"HTTP on port {port}")
    server.serve_forever()


# ===================== Main =====================
def main():
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN not set!")
        return
    if not GAS_URL:
        logger.error("❌ GAS_URL not set!")
        return
    
    logger.info(f"✅ Token: {TELEGRAM_TOKEN[:20]}...")
    logger.info(f"✅ GAS: {GAS_URL[:60]}...")
    
    if GEMINI_API_KEY:
        logger.info(f"✅ Gemini API: {GEMINI_API_KEY[:15]}...")
        logger.info(f"✅ Gemini Model: {GEMINI_MODEL}")
        logger.info(f"✅ Gemini Enabled: {USE_GEMINI}")
    else:
        logger.warning("⚠️ GEMINI_API_KEY not set")
    
    logger.info(f"📊 Results per page: {RESULTS_PER_PAGE}")
    
    threading.Thread(target=run_http_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("docs", docs_cmd))
    app.add_handler(CommandHandler("article", article_cmd))
    app.add_handler(CommandHandler("raw", raw_cmd))
    app.add_handler(CommandHandler("md", md_cmd))
    app.add_handler(CommandHandler("next", next_page))    # ✨ NEW
    app.add_handler(CommandHandler("prev", prev_page))    # ✨ NEW
    app.add_handler(CommandHandler("clear", clear_cmd))   # ✨ NEW
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot v10 starting (Sort + Pagination)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
