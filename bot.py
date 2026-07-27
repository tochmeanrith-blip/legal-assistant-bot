import os
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

# Config
MAX_RESULTS_PER_QUERY = None  # None = unlimited, ឬដាក់លេខ ឧ. 20
MESSAGE_DELAY = 0.5  # seconds between messages (avoid rate limit)

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


# ===================== Format =====================
def format_article_results(data):
    if not data.get("success"):
        return [f"❌ Error: {data.get('error', 'Unknown')}"]
    
    results = data.get("results", [])
    if not results:
        return [f"🔍 រកមិនឃើញមាត្រា {data.get('article')}"]
    
    messages = []
    header = f"📄 មាត្រា {data.get('article')} ({len(results)} match)\n"
    header += "━━━━━━━━━━━━━━━━━━━━"
    messages.append(header)
    
    for i, r in enumerate(results, 1):
        content = r.get('content', '').replace("**", "").strip()
        msg = f"📖 ឯកសារ៖ {r['document']}\n"
        if len(results) > 1:
            msg += f"📄 លទ្ធផលទី {i}/{len(results)}\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += content
        messages.append(msg)
    
    return messages


def format_search_results(data, max_results=None):
    """Format search results - យកទាំងអស់ (default)"""
    if not data.get("success"):
        return [f"❌ Error: {data.get('error', 'Unknown')}"]
    
    results = data.get("results", [])
    if not results:
        return [f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់៖ {data.get('query', '')}"]
    
    if max_results is None:
        display_results = results
    else:
        display_results = results[:max_results]
    
    messages = []
    keywords = data.get("keywords", [])
    total = len(results)
    showing = len(display_results)
    
    # Header
    header = f"🔍 លទ្ធផលស្វែងរក\n"
    if keywords:
        header += f"🔑 Keywords: {', '.join(keywords)}\n"
    header += f"📊 រកឃើញ៖ {total} លទ្ធផល"
    if showing < total:
        header += f" (បង្ហាញ {showing})"
    header += "\n━━━━━━━━━━━━━━━━━━━━"
    messages.append(header)
    
    for i, r in enumerate(display_results, 1):
        article = f"មាត្រា {r['article']}" if r.get("article") else ""
        content = r.get('content', '').replace("**", "").strip()
        
        msg = f"📌 លទ្ធផលទី {i}/{total}\n"
        msg += f"📖 {r['document']}\n"
        if article:
            msg += f"📄 {article}\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += content
        messages.append(msg)
    
    return messages


def split_by_lines(text, max_length=4000):
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


async def send_messages(update, messages, delay=MESSAGE_DELAY):
    """Send messages with rate limiting"""
    total = len(messages)
    for i, msg in enumerate(messages):
        try:
            if len(msg) <= 4000:
                await update.message.reply_text(msg)
            else:
                parts = split_by_lines(msg, max_length=4000)
                for j, part in enumerate(parts):
                    prefix = f"(ភាគ {j+1}/{len(parts)})\n\n" if len(parts) > 1 else ""
                    await update.message.reply_text(prefix + part)
                    if j < len(parts) - 1:
                        await asyncio.sleep(delay)
            
            # Delay between messages
            if i < total - 1:
                await asyncio.sleep(delay)
                
        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Send error at {i+1}/{total}: {e}")
            
            # Rate limit handling
            if "429" in error_msg or "flood" in error_msg or "too many" in error_msg:
                logger.warning("Rate limited, waiting 5s...")
                await asyncio.sleep(5)
                try:
                    await update.message.reply_text(msg[:4000])
                    await asyncio.sleep(delay)
                except Exception as e2:
                    logger.error(f"Retry failed: {e2}")


# ===================== Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "សួស្តី! 🇰🇭\n"
        "ខ្ញុំគឺ Bot ស្វែងរកច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា\n\n"
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
        "   /help - ជំនួយ"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 ជំនួយប្រើប្រាស់\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔍 ស្វែងរកតាមមាត្រា:\n"
        "  /article ៥\n"
        "  /article ១៨១ នីតិវិធីព្រហ្មទណ្ឌ\n\n"
        "🔎 ស្វែងរកតាមពាក្យគន្លឹះ:\n"
        "  លួច\n"
        "  ចាប់ខ្លួន\n"
        "  មូលហេតុនៃទោស\n\n"
        "📚 ឯកសារ:\n"
        "  /docs - បញ្ជីឯកសារទាំងអស់\n\n"
        "🛠 គ្រប់គ្រង:\n"
        "  /ping - សាកល្បង API\n"
        "  /start - ចាប់ផ្តើមថ្មី\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 គន្លឹះ: លទ្ធផលនឹងបង្ហាញទាំងអស់\n"
        "   រៀងម្នាក់ៗតាមមាត្រា"
    )


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 កំពុងសាកល្បង...")
    try:
        response = requests.get(GAS_URL, timeout=30)
        await update.message.reply_text(
            f"Status: {response.status_code}\n"
            f"Response: {response.text[:500]}"
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
    
    await update.message.reply_text(f"🔍 កំពុងស្វែងរកមាត្រា {article_num}...")
    data = find_article(article_num, doc_name)
    messages = format_article_results(data)
    await send_messages(update, messages)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text
    
    data = search_law(query)
    total = data.get("count", 0)
    
    # ជូនដំណឹងអំពីចំនួនលទ្ធផល
    if total > 10:
        await update.message.reply_text(
            f"🔍 រកឃើញ {total} លទ្ធផល\n"
            f"⏳ កំពុងបញ្ជូន... សូមរង់ចាំ ~{total * MESSAGE_DELAY:.0f} វិនាទី"
        )
    else:
        await update.message.reply_text("🔍 កំពុងស្វែងរក...")
    
    messages = format_search_results(data, max_results=MAX_RESULTS_PER_QUERY)
    await send_messages(update, messages)


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
    logger.info(f"✅ Max results: {MAX_RESULTS_PER_QUERY or 'unlimited'}")
    
    threading.Thread(target=run_http_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("docs", docs_cmd))
    app.add_handler(CommandHandler("article", article_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
