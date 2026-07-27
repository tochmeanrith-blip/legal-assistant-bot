import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
MODEL_NAME = "gemini-flash-lite-latest"

# ⭐ Config
MAX_TOTAL_CHARS = 200000       # កម្រិត context បញ្ជូនទៅ Gemini
USE_FULL_KNOWLEDGE = True      # True = បញ្ជូនឯកសារទាំងអស់, False = ប្រើ RAG

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

KNOWLEDGE_CACHE = {"text": None, "docs": None}


# ===================== Google Drive =====================
def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def get_all_google_docs(folder_id):
    service = get_drive_service()
    all_files = []

    def fetch_recursive(parent_id):
        query = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.document'"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        for f in results.get("files", []):
            all_files.append(f)

        folders = service.files().list(
            q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder'",
            fields="files(id)"
        ).execute()
        for folder in folders.get("files", []):
            fetch_recursive(folder["id"])

    fetch_recursive(parent_id=folder_id)
    return all_files


def extract_all_text(files):
    service = get_drive_service()
    docs = []
    for file in files:
        try:
            content = service.files().export(
                fileId=file["id"], mimeType="text/plain"
            ).execute()
            text = content.decode("utf-8")
            docs.append({"name": file["name"], "text": text})
            logger.info(f"  ✓ Loaded: {file['name']} ({len(text)} chars)")
        except Exception as e:
            logger.error(f"  ✗ Error reading {file['name']}: {e}")
    return docs


def load_knowledge():
    logger.info("📚 Loading knowledge base...")
    files = get_all_google_docs(DRIVE_FOLDER_ID)
    docs = extract_all_text(files)

    # បង្កើត text ពេញ
    combined = ""
    for d in docs:
        combined += f"\n\n=== ឯកសារ: {d['name']} ===\n{d['text']}"

    KNOWLEDGE_CACHE["docs"] = docs
    KNOWLEDGE_CACHE["text"] = combined
    logger.info(f"✅ Total: {len(docs)} docs, {len(combined)} chars")


# ===================== N-gram Search (សម្រាប់អក្សរខ្មែរ) =====================
def make_ngrams(text, n=3):
    """បង្កើត n-gram ពី text"""
    text = text.replace(" ", "").replace("\n", "")
    return set(text[i:i+n] for i in range(len(text) - n + 1))


def find_relevant_docs(question, docs, max_chars=200000):
    """រកឯកសារពាក់ព័ន្ធដោយប្រើ n-gram matching"""
    question_ngrams = make_ngrams(question, n=3)
    
    if not question_ngrams:
        # បើ n-gram ទទេ → យកឯកសារទាំងអស់
        return build_full_context(docs, max_chars)
    
    # ដាក់ពិន្ទុឯកសារនីមួយៗ
    scored = []
    for doc in docs:
        doc_ngrams = make_ngrams(doc["text"][:5000], n=3)  # sample ដើម្បីលឿន
        overlap = len(question_ngrams & doc_ngrams)
        scored.append((overlap, doc))
        logger.info(f"  Doc '{doc['name']}': score={overlap}")
    
    # តម្រៀប
    scored.sort(reverse=True, key=lambda x: x[0])
    
    # យកឯកសារពាក់ព័ន្ធបំផុត
    result = ""
    for score, doc in scored:
        chunk = f"\n\n=== ឯកសារ: {doc['name']} ===\n{doc['text']}"
        if len(result) + len(chunk) > max_chars:
            # កាត់ឲ្យសមនឹង limit
            remaining = max_chars - len(result)
            if remaining > 1000:
                result += chunk[:remaining]
            break
        result += chunk
    
    return result


def build_full_context(docs, max_chars=200000):
    """បញ្ជូនឯកសារទាំងអស់ដោយ distribute ស្មើគ្នា"""
    if not docs:
        return ""
    
    # បែងចែក budget ស្មើគ្នា
    per_doc = max_chars // len(docs)
    
    result = ""
    for doc in docs:
        text = doc["text"][:per_doc]
        result += f"\n\n=== ឯកសារ: {doc['name']} ===\n{text}"
    
    return result


# ===================== Gemini =====================
def ask_gemini(question, context_text):
    prompt = f"""អ្នកគឺជាអ្នកជំនាញច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា។
ឆ្លើយសំណួរដោយផ្អែកលើឯកសារច្បាប់ដែលបានផ្តល់ជូនខាងក្រោម។

**គោលការណ៍៖**
- ឆ្លើយច្បាស់លាស់ ត្រង់ចំណុច ជាភាសាខ្មែរ
- បញ្ជាក់ឈ្មោះឯកសារ និងលេខមាត្រា (បើមាន)
- បើគ្មានទិន្នន័យពាក់ព័ន្ធនៅក្នុងឯកសារ សូមឆ្លើយថា "ខ្ញុំមិនមានព័ត៌មានពាក់ព័ន្ធនឹងសំណួរនេះនៅក្នុងឯកសារទេ"

--- ឯកសារច្បាប់ ---
{context_text}

--- សំណួរ ---
{question}

--- ចម្លើយ ---
"""

    logger.info(f"📤 Sending to Gemini: {len(prompt)} chars")

    try:
        response = model.generate_content(prompt)
        
        if not response.candidates:
            logger.error(f"No candidates: {response.prompt_feedback}")
            return "⚠️ Gemini មិនអាចឆ្លើយបានទេ (blocked)។"
        
        if not response.text:
            reason = response.candidates[0].finish_reason
            logger.error(f"Empty response. Reason: {reason}")
            return f"⚠️ ចម្លើយទទេ (reason: {reason})។"
        
        return response.text
        
    except Exception as e:
        import traceback
        logger.error(f"Gemini Error: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        return f"⚠️ Gemini Error: {str(e)[:200]}"


# ===================== Telegram Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "សួស្តី! ខ្ញុំគឺជា Bot ជំនួយច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា 🇰🇭\n\n"
        "សូមវាយសំណួររបស់លោកអ្នក។\n\n"
        "Commands:\n"
        "/reload - Reload ឯកសារ\n"
        "/status - មើលស្ថានភាព\n"
        "/test - test Gemini\n"
        "/mode - ប្តូរ mode (full/rag)"
    )


async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("កំពុង reload...")
    try:
        load_knowledge()
        docs = KNOWLEDGE_CACHE["docs"]
        msg = f"✅ Reload រួច\n"
        for d in docs:
            msg += f"  • {d['name']}: {len(d['text']):,} chars\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = KNOWLEDGE_CACHE.get("docs") or []
    text = KNOWLEDGE_CACHE.get("text") or ""
    
    mode = "FULL (ឯកសារទាំងអស់)" if USE_FULL_KNOWLEDGE else "RAG (ស្វែងរក)"
    
    msg = f"📊 **Status**\n"
    msg += f"Model: {MODEL_NAME}\n"
    msg += f"Mode: {mode}\n"
    msg += f"Max chars: {MAX_TOTAL_CHARS:,}\n"
    msg += f"Documents: {len(docs)}\n"
    msg += f"Total chars: {len(text):,}\n\n"
    for d in docs:
        msg += f"  • {d['name']}: {len(d['text']):,}\n"
    
    await update.message.reply_text(msg)


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = model.generate_content("សូមឆ្លើយថា 'ដំណើរការ'")
        await update.message.reply_text(f"✅ Response: {response.text}")
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}: {e}")


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global USE_FULL_KNOWLEDGE
    USE_FULL_KNOWLEDGE = not USE_FULL_KNOWLEDGE
    mode = "FULL" if USE_FULL_KNOWLEDGE else "RAG"
    await update.message.reply_text(f"✅ Mode: {mode}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text
    await update.message.reply_text("កំពុងគិត... 🤔")

    try:
        docs = KNOWLEDGE_CACHE.get("docs")
        if not docs:
            load_knowledge()
            docs = KNOWLEDGE_CACHE["docs"]
        
        # ជ្រើសរើសវិធី
        if USE_FULL_KNOWLEDGE:
            context_text = build_full_context(docs, MAX_TOTAL_CHARS)
            logger.info(f"Using FULL mode: {len(context_text)} chars")
        else:
            context_text = find_relevant_docs(question, docs, MAX_TOTAL_CHARS)
            logger.info(f"Using RAG mode: {len(context_text)} chars")
        
        answer = ask_gemini(question, context_text)
        
        # Split messages វែង
        if len(answer) > 4000:
            for i in range(0, len(answer), 4000):
                await update.message.reply_text(answer[i:i+4000])
        else:
            await update.message.reply_text(answer)
            
    except Exception as e:
        logger.error(e)
        await update.message.reply_text(f"❌ បញ្ហា: {e}")


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
    logger.info(f"HTTP server on port {port}")
    server.serve_forever()


# ===================== Main =====================
def main():
    try:
        load_knowledge()
    except Exception as e:
        logger.error(f"Preload failed: {e}")

    threading.Thread(target=run_http_server, daemon=True).start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reload", reload_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("test", test_cmd))
    application.add_handler(CommandHandler("mode", mode_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"🤖 Bot starting | Model: {MODEL_NAME}")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
