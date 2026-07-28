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

# ✨ NEW: Gemini Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
USE_GEMINI = os.getenv("USE_GEMINI", "true").lower() == "true"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


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


# ===================== ✨ NEW: Convert to Markdown =====================
def convert_to_markdown(data, mode="search"):
    """
    បំលែងទិន្នន័យ raw ពី GAS ទៅជា Markdown format ស្អាត
    
    Args:
        data: dict ពី GAS (មាន results, keywords, etc.)
        mode: "search" ឬ "article"
    
    Returns:
        str: Markdown formatted text
    """
    if not data.get("success"):
        return f"❌ Error: {data.get('error', 'Unknown')}"
    
    results = data.get("results", [])
    if not results:
        query = data.get("query") or data.get("article", "")
        return f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់៖ **{query}**"
    
    md = ""
    
    # ── Header ─────────────────────────────────────────
    if mode == "search":
        query = data.get("query", "")
        keywords = data.get("keywords", [])
        md += f"# 🔍 លទ្ធផលស្វែងរក: {query}\n\n"
        if keywords:
            md += f"**Keywords:** `{', '.join(keywords)}`\n\n"
        md += f"**រកឃើញ:** {len(results)} លទ្ធផល\n\n"
    else:  # article
        article = data.get("article", "")
        md += f"# ⚖️ មាត្រា {article}\n\n"
        md += f"**រកឃើញ:** {len(results)} កន្លែង\n\n"
    
    md += "---\n\n"
    
    # ── Each Result ────────────────────────────────────
    for i, r in enumerate(results, 1):
        doc_name = r.get("document", "N/A")
        article = r.get("article", "")
        content = r.get("content", "").strip()
        
        # Sub-header
        md += f"## 📖 លទ្ធផលទី {i}/{len(results)}\n\n"
        md += f"**ឯកសារ:** {doc_name}\n"
        if article:
            md += f"**មាត្រា:** {article}\n"
        md += "\n"
        
        # Clean up content
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = re.sub(r'^\s+', '', content, flags=re.MULTILINE)
        content = content.strip()
        
        md += f"{content}\n\n"
        
        if i < len(results):
            md += "---\n\n"
    
    # ── Footer ─────────────────────────────────────────
    md += "\n---\n"
    md += "*ប្រភព: ឯកសារច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា*\n"
    
    return md


# ===================== ✨ NEW: Gemini Integration =====================
def format_with_gemini(markdown_text, query, mode="search"):
    """
    ផ្ញើ Markdown ទៅ Gemini សម្រាប់រៀបចំ Format ឲ្យស្អាត។
    Fallback: បើ Gemini fail → return markdown_text (មិន break bot)
    """
    if not GEMINI_API_KEY:
        logger.warning("⚠️ GEMINI_API_KEY not set, using markdown")
        return markdown_text
    
    if not markdown_text or len(markdown_text) < 20:
        return markdown_text
    
    try:
        # ── System Instruction ──────────────────────────────
        system_prompt = (
            "អ្នកជាជំនួយការផ្នែកច្បាប់កម្ពុជា មានតួនាទីរៀបចំ Format អត្ថបទច្បាប់សម្រាប់ Telegram។\n\n"
            "ច្បាប់ដែលត្រូវអនុវត្តតាមយ៉ាងតឹងរឹង:\n"
            "១. រក្សាខ្លឹមសារច្បាប់ ១០០% — មិនត្រូវផ្លាស់ប្តូរ, កាត់, ឬបន្ថែមខ្លឹមសារ\n"
            "២. រៀបចំ Format ឲ្យអានងាយ មានលំដាប់លំដោយ\n"
            "៣. ប្រើ Emoji សមស្រប (⚖️ 📋 🔹 📌 ✅ ⚠️ 📖 📄 ▪️)\n"
            "៤. បំបែក Paragraph ឲ្យច្បាស់\n"
            "៥. ឆ្លើយជាភាសាខ្មែរ\n"
            "៦. កុំប្រើ Markdown syntax (** __ ##) — ប្រើអក្សរធម្មតា + emoji\n"
            "៧. រក្សា structure: ឯកសារ → មាត្រា → ខ្លឹមសារ\n"
            "៨. កុំបន្ថែម disclaimer, note, ឬការណែនាំបន្ថែម\n"
            "៩. បើមានចំណុចជា list (១. ២. ៣.) → ដាក់បន្ទាត់ថ្មី\n"
            "១០. រក្សាលេខមាត្រា និងឈ្មោះឯកសារឲ្យដដែល\n"
            "១១. បើមានច្រើនមាត្រា → រៀបតាមលំដាប់លេខមាត្រា\n"
            "១២. ប្រើ separator (▬▬▬▬▬▬▬▬▬▬) ដើម្បីបំបែក sections"
        )
        
        # ── User Prompt ──────────────────────────────────────
        if mode == "search":
            user_prompt = (
                f"សំណួរអ្នកប្រើ: {query}\n\n"
                f"ទិន្នន័យច្បាប់ (Markdown):\n"
                f"```markdown\n{markdown_text}\n```\n\n"
                f"សូមរៀបចំទិន្នន័យខាងលើឲ្យស្អាត អានងាយ "
                f"មានលំដាប់លំដោយ រក្សាខ្លឹមសារ ១០០% ត្រូវ។"
            )
        else:  # article
            user_prompt = (
                f"ស្វែងរក: មាត្រា {query}\n\n"
                f"ទិន្នន័យ (Markdown):\n"
                f"```markdown\n{markdown_text}\n```\n\n"
                f"សូមរៀបចំបង្ហាញ: ឯកសារ + លេខមាត្រា + ខ្លឹមសារពេញ "
                f"ដោយស្អាត អានងាយ រក្សាខ្លឹមសារ ១០០% ត្រូវ។"
            )
        
        # ── API Payload ─────────────────────────────────────
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
            url,
            json=payload,
            timeout=45,
            headers={"Content-Type": "application/json"}
        )
        
        logger.info(f"← Gemini status: {response.status_code}")
        
        if response.status_code != 200:
            logger.error(f"Gemini error: {response.text[:300]}")
            return markdown_text
        
        result = response.json()
        
        candidates = result.get("candidates", [])
        if not candidates:
            logger.warning("Gemini: no candidates")
            return markdown_text
        
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            logger.warning("Gemini: no parts")
            return markdown_text
        
        formatted = parts[0].get("text", "").strip()
        
        if not formatted:
            logger.warning("Gemini: empty text")
            return markdown_text
        
        logger.info(f"✅ Gemini success: output len={len(formatted)}")
        return formatted
        
    except requests.exceptions.Timeout:
        logger.error("Gemini timeout — fallback to markdown")
        return markdown_text
    except Exception as e:
        logger.error(f"Gemini exception: {e}")
        return markdown_text


def format_with_gemini_chunked(markdown_text, query, mode="search", chunk_size=6000):
    """
    សម្រាប់ text វែងខ្លាំង — បំបែកជា chunks មុនផ្ញើ Gemini
    """
    if len(markdown_text) <= chunk_size:
        return format_with_gemini(markdown_text, query, mode)
    
    # បំបែកតាម "---"
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


# ===================== Format (Old - Fallback) =====================
def format_search_results_combined(data):
    """រួមលទ្ធផលទាំងអស់ក្នុងសារតែមួយ (មិន Gemini)"""
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
    """រួមមាត្រាទាំងអស់ក្នុងសារតែមួយ (មិន Gemini)"""
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
    """បំបែក text ត្រង់ចន្លោះលទ្ធផល"""
    if len(text) <= max_length:
        return [text]
    
    # Try different separators (priority order)
    separators = [
        "▬▬▬▬▬▬▬▬▬▬\n\n",
        "━━━━━━━━━━━━━━━━━━━━\n\n",
        "\n---\n\n",
        "\n---\n",
    ]
    
    for sep in separators:
        if sep in text:
            return _split_by_separator(text, sep, max_length)
    
    # Fallback: split by newline
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
    """ផ្ញើសារវែង ដោយបំបែកតែពេលចាំបាច់"""
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


# ===================== ✨ NEW: Process Query with Gemini =====================
async def process_search_query(update, query):
    """
    ដំណើរការសំណួរស្វែងរក (keyword) — ជាមួយ Gemini format
    """
    status_msg = await update.message.reply_text("🔍 កំពុងស្វែងរក...")
    
    # 1️⃣ Call GAS
    data = search_law(query)
    total = data.get("count", 0)
    
    if not data.get("success") or total == 0:
        # No results → send simple message
        try:
            await status_msg.delete()
        except:
            pass
        text = format_search_results_combined(data)
        await send_long_message(update, text)
        return
    
    # 2️⃣ Convert to Markdown
    try:
        await status_msg.edit_text(f"🔍 រកឃើញ {total} លទ្ធផល\n📝 កំពុងបំលែងជា Markdown...")
    except:
        pass
    
    markdown = convert_to_markdown(data, mode="search")
    logger.info(f"📝 Markdown length: {len(markdown)}")
    
    # 3️⃣ Gemini Format (if enabled)
    if USE_GEMINI and GEMINI_API_KEY:
        try:
            await status_msg.edit_text(
                f"🔍 រកឃើញ {total} លទ្ធផល\n"
                f"🤖 កំពុងរៀបចំ Format ដោយ Gemini AI..."
            )
        except:
            pass
        
        final_text = format_with_gemini_chunked(markdown, query, mode="search")
    else:
        final_text = markdown
    
    # 4️⃣ Send to user
    try:
        await status_msg.delete()
    except:
        pass
    
    await send_long_message(update, final_text)


async def process_article_query(update, article_num, doc_name=None):
    """
    ដំណើរការសំណួរមាត្រា — ជាមួយ Gemini format
    """
    status_msg = await update.message.reply_text(f"🔍 កំពុងស្វែងរកមាត្រា {article_num}...")
    
    # 1️⃣ Call GAS
    data = find_article(article_num, doc_name)
    
    if not data.get("success") or not data.get("results"):
        try:
            await status_msg.delete()
        except:
            pass
        text = format_article_results_combined(data)
        await send_long_message(update, text)
        return
    
    # 2️⃣ Convert to Markdown
    try:
        await status_msg.edit_text(f"📝 កំពុងបំលែងជា Markdown...")
    except:
        pass
    
    markdown = convert_to_markdown(data, mode="article")
    logger.info(f"📝 Markdown length: {len(markdown)}")
    
    # 3️⃣ Gemini Format
    if USE_GEMINI and GEMINI_API_KEY:
        try:
            await status_msg.edit_text(f"🤖 កំពុងរៀបចំ Format ដោយ Gemini AI...")
        except:
            pass
        
        final_text = format_with_gemini_chunked(markdown, article_num, mode="article")
    else:
        final_text = markdown
    
    # 4️⃣ Send
    try:
        await status_msg.delete()
    except:
        pass
    
    await send_long_message(update, final_text)


# ===================== Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gemini_status = "🟢 បើក" if USE_GEMINI and GEMINI_API_KEY else "🔴 បិទ"
    
    await update.message.reply_text(
        "សួស្តី! 🇰🇭\n"
        "ខ្ញុំគឺ Bot ស្វែងរកច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា\n\n"
        f"🤖 Gemini AI Format: {gemini_status}\n\n"
        "🔍 របៀបប្រើ:\n\n"
        "១. ស្វែងរកតាមពាក្យ:\n"
        "   លួច\n"
        "   មូលហេតុនៃទោស\n\n"
        "២. ស្វែងរកមាត្រា:\n"
        "   មាត្រា ៥ ព្រហ្មទណ្ឌ\n"
        "   /article ៥ ព្រហ្មទណ្ឌ\n\n"
        "៣. Commands:\n"
        "   /docs - បញ្ជីឯកសារ\n"
        "   /ping - test API\n"
        "   /raw <query> - លទ្ធផលដើម (មិន Gemini)\n"
        "   /md <query> - Markdown ដើម (មិន Gemini)\n"
        "   /help - ជំនួយ"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 ជំនួយ\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 ស្វែងរកតាមមាត្រា:\n"
        "  /article ៥\n"
        "  /article ១៨១ នីតិវិធីព្រហ្មទណ្ឌ\n\n"
        "🔎 ស្វែងរកតាមពាក្យ:\n"
        "  លួច\n"
        "  ចាប់ខ្លួន\n\n"
        "🤖 ជាមួយ Gemini AI (ស្វ័យប្រវត្តិ):\n"
        "  លទ្ធផលនឹងស្អាត អានងាយ\n\n"
        "📝 ការមើលទិន្នន័យដើម:\n"
        "  /raw លួច - ខ្លឹមសារធម្មតា\n"
        "  /md លួច - Markdown format\n\n"
        "📚 /docs — បញ្ជីឯកសារ\n"
        "🛠 /ping — សាកល្បង API"
    )


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 កំពុងសាកល្បង...")
    try:
        response = requests.get(GAS_URL, timeout=30)
        gemini_status = "✅ Configured" if GEMINI_API_KEY else "❌ Not set"
        await update.message.reply_text(
            f"📡 GAS Status: {response.status_code}\n"
            f"🤖 Gemini API: {gemini_status}\n"
            f"🎯 Model: {GEMINI_MODEL}\n"
            f"⚙️ Use Gemini: {USE_GEMINI}\n\n"
            f"Response preview:\n{response.text[:400]}"
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


# ✨ NEW: /raw command
async def raw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """បង្ហាញលទ្ធផលដើម (មិន Gemini, មិន Markdown)"""
    if not context.args:
        await update.message.reply_text("ប្រើ: /raw <សំណួរ>\nឧ.: /raw លួច")
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


# ✨ NEW: /md command - show markdown only
async def md_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """បង្ហាញលទ្ធផលជា Markdown (មិន Gemini)"""
    if not context.args:
        await update.message.reply_text("ប្រើ: /md <សំណួរ>\nឧ.: /md លួច")
        return
    
    query = " ".join(context.args)
    await update.message.reply_text(f"📝 (Markdown) ស្វែងរក: {query}...")
    
    if query.startswith("មាត្រា"):
        parts = query.split()
        if len(parts) >= 2:
            article_num = parts[1]
            doc_name = " ".join(parts[2:]) if len(parts) > 2 else None
            data = find_article(article_num, doc_name)
            markdown = convert_to_markdown(data, mode="article")
            await send_long_message(update, markdown)
            return
    
    data = search_law(query)
    markdown = convert_to_markdown(data, mode="search")
    await send_long_message(update, markdown)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular text messages"""
    query = update.message.text.strip()
    
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
        logger.warning("⚠️ GEMINI_API_KEY not set — will use markdown format")
    
    threading.Thread(target=run_http_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("docs", docs_cmd))
    app.add_handler(CommandHandler("article", article_cmd))
    app.add_handler(CommandHandler("raw", raw_cmd))
    app.add_handler(CommandHandler("md", md_cmd))  # ✨ NEW
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
