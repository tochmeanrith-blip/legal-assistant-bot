import os
import logging
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GAS_URL = os.getenv("GAS_URL")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ===================== Apps Script API =====================
def call_gas(payload, timeout=60):
    """Call Google Apps Script API"""
    try:
        logger.info(f"→ GAS request: {payload}")
        response = requests.post(
            GAS_URL,
            json=payload,
            timeout=timeout,
            allow_redirects=True
        )
        logger.info(f"← GAS status: {response.status_code}")
        
        if response.status_code != 200:
            return {"success": False, "error": f"HTTP {response.status_code}"}
        
        return response.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Timeout - GAS ឆ្លើយយឺត"}
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


# ===================== Format Response =====================
def format_search_results(data):
    if not data.get("success"):
        return f"❌ Error: {data.get('error', 'Unknown')}"
    
    results = data.get("results", [])
    if not results:
        return f"🔍 រកមិនឃើញលទ្ធផលសម្រាប់៖ {data.get('query', '')}"
    
    keywords = data.get("keywords", [])
    msg = f"🔍 លទ្ធផលស្វែងរក ({data.get('count')} matches)\n"
    if keywords:
        msg += f"🔑 Keywords: {', '.join(keywords)}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for i, r in enumerate(results[:5], 1):
        article = f"មាត្រា {r['article']}" if r.get("article") else ""
        msg += f"📌 លទ្ធផលទី {i}\n"
        msg += f"📖 {r['document']}\n"
        if article:
            msg += f"📄 {article}\n"
        msg += f"\n{r['content']}\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    return msg


def format_article_results(data):
    if not data.get("success"):
        return f"❌ Error: {data.get('error', 'Unknown')}"
    
    results = data.get("results", [])
    if not results:
        return f"🔍 រកមិនឃើញមាត្រា {data.get('article')}"
    
    msg = f"📄 មាត្រា {data.get('article')} ({len(results)} matches)\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for r in results:
        msg += f"📖 {r['document']}\n\n"
        msg += f"{r['content']}\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    
    return msg


# ===================== Telegram Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "សួស្តី! 🇰🇭\n"
        "ខ្ញុំគឺ Bot ស្វែងរកច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា\n\n"
        "របៀបប្រើ:\n\n"
        "១. ស្វែងរកតាមពាក្យ:\n"
        "   វាយ: មូលហេតុនៃទោស\n\n"
        "២. ស្វែងរកមាត្រា:\n"
        "   វាយ: មាត្រា ៥ ព្រហ្មទណ្ឌ\n"
        "   ឬ: /article ៥ ព្រហ្មទណ្ឌ\n\n"
        "៣. មើលឯកសារ:\n"
        "   វាយ: /docs\n\n"
        "៤. Test API:\n"
        "   វាយ: /ping"
    )


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test connection to GAS"""
    await update.message.reply_text("🔍 កំពុងសាកល្បង GAS...")
    try:
        response = requests.get(GAS_URL, timeout=30)
        await update.message.reply_text(
            f"Status: {response.status_code}\n"
            f"Response: {response.text[:500]}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def docs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 កំពុងទាញបញ្ជីឯកសារ...")
    
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
        await update.message.reply_text(
            "សូមបញ្ជាក់លេខមាត្រា\n"
            "ឧ.: /article ៥\n"
            "ឬ: /article ៥ ព្រហ្មទណ្ឌ"
        )
        return
    
    article_num = args[0]
    doc_name = " ".join(args[1:]) if len(args) > 1 else None
    
    await update.message.reply_text(f"🔍 កំពុងស្វែងរកមាត្រា {article_num}...")
    
    data = find_article(article_num, doc_name)
    msg = format_article_results(data)
    
    await send_long_message(update, msg)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text
    await update.message.reply_text(f"🔍 កំពុងស្វែងរក...")
    
    data = search_law(query)
    msg = format_search_results(data)
    
    await send_long_message(update, msg)


async def send_long_message(update, msg):
    """Split messages វែង"""
    if len(msg) > 4000:
        for i in range(0, len(msg), 4000):
            await update.message.reply_text(msg[i:i+4000])
    else:
        await update.message.reply_text(msg)


# ===================== HTTP Server (Render) =====================
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
    logger.info(f"HTTP server on port {port}")
    server.serve_forever()


# ===================== Main =====================
def main():
    # Validate env vars
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN not set!")
        return
    if not GAS_URL:
        logger.error("❌ GAS_URL not set!")
        return
    
    logger.info(f"✅ TELEGRAM_TOKEN: {TELEGRAM_TOKEN[:20]}...")
    logger.info(f"✅ GAS_URL: {GAS_URL[:60]}...")
    
    # HTTP server
    threading.Thread(target=run_http_server, daemon=True).start()

    # Telegram bot
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("docs", docs_cmd))
    application.add_handler(CommandHandler("article", article_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot starting (GAS mode)")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
