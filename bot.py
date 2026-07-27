import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
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

MAX_TOTAL_CHARS = 200000
USE_FULL_KNOWLEDGE = True

# ⭐ Configuration ថ្មី
generation_config = {
    "temperature": 0.3,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    MODEL_NAME,
    generation_config=generation_config,
    safety_settings=safety_settings,
)

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

    fetch_recursive(folder_id)
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

    combined = ""
    for d in docs:
        combined += f"\n\n=== ឯកសារ: {d['name']} ===\n{d['text']}"

    KNOWLEDGE_CACHE["docs"] = docs
    KNOWLEDGE_CACHE["text"] = combined
    logger.info(f"✅ Total: {len(docs)} docs, {len(combined)} chars")


def build_full_context(docs, max_chars=200000):
    if not docs:
        return ""
    per_doc = max_chars // len(docs)
    result = ""
    for doc in docs:
        text = doc["text"][:per_doc]
        result += f"\n\n=== ឯកសារ: {doc['name']} ===\n{text}"
    return result


# ===================== Gemini =====================
def ask_gemini(question, context_text):
    prompt = f"""អ្នកគឺជាមេធាវី និងអ្នកជំនាញច្បាប់ជាន់ខ្ពស់នៃព្រះរាជាណាចក្រកម្ពុជា។
ភារកិច្ចរបស់អ្នកគឺពន្យល់ច្បាប់ជាភាសាខ្មែរបែបផ្លូវការ លម្អិត និងសមស្របសម្រាប់ការស្រាវជ្រាវ។

## គោលការណ៍នៃការឆ្លើយ ##

១. **រចនាសម្ព័ន្ធនៃចម្លើយ** ត្រូវរួមមាន៖
   - **សេចក្តីផ្តើម**: ពន្យល់ខ្លឹមសារខ្លីៗ អំពីប្រធានបទ
   - **មូលដ្ឋានច្បាប់**: ដកស្រង់មាត្រាពាក់ព័ន្ធ (ខ្លឹមសារពេញ) ព្រមទាំងបញ្ជាក់ឈ្មោះឯកសារ និងលេខមាត្រា
   - **ការវិភាគ និងពន្យល់លម្អិត**: ពន្យល់អត្ថន័យ សារៈសំខាន់ លក្ខខណ្ឌ និងករណីអនុវត្ត
   - **ឧទាហរណ៍ (បើមាន)**: លើកឧទាហរណ៍ជាក់ស្តែងដើម្បីធ្វើឲ្យយល់ច្បាស់
   - **សេចក្តីសន្និដ្ឋាន**: សរុបចំណុចសំខាន់ៗ

២. **ភាសា និងស្ទីល**:
   - ប្រើភាសាខ្មែរបែបផ្លូវការ ច្បាស់លាស់ សមស្របតាមស្តង់ដារច្បាប់
   - ប្រើពាក្យបច្ចេកទេសច្បាប់ត្រឹមត្រូវ
   - ចម្លើយត្រូវលម្អិត និងគ្រប់ជ្រុងជ្រោយ (យ៉ាងតិច ៣០០-៥០០ ពាក្យ)

៣. **ការដកស្រង់ឯកសារ**:
   - តែងតែបញ្ជាក់ឈ្មោះឯកសារពេញ និងលេខមាត្រា
   - ដកស្រង់ខ្លឹមសារមាត្រាឲ្យពេញលេញ
   - ប្រើទម្រង់៖ **"យោងតាមមាត្រា ... នៃ [ឈ្មោះឯកសារ]៖"**

៤. **ភាពសុចរិត**:
   - ឆ្លើយផ្អែកលើឯកសារដែលបានផ្តល់ជូនតែប៉ុណ្ណោះ
   - មិនប្រឌិតព័ត៌មាន
   - បើគ្មានទិន្នន័យ សូមឆ្លើយថា៖ "សូមអភ័យទោស ខ្ញុំមិនមានព័ត៌មានពាក់ព័ន្ធនឹងសំណួរនេះនៅក្នុងឯកសារច្បាប់ដែលបានផ្តល់ជូនទេ។"

═══════════════════════════════════
📚 ឯកសារច្បាប់សម្រាប់យោង
═══════════════════════════════════
{context_text}

═══════════════════════════════════
❓ សំណួរ
═══════════════════════════════════
{question}

═══════════════════════════════════
✍️ ចម្លើយលម្អិត (សូមឆ្លើយឲ្យបានក្បោះក្បាយ ជាផ្លូវការ និងគ្រប់ជ្រុងជ្រោយ)
═══════════════════════════════════
"""

    logger.info(f"📤 Sending to Gemini: {len(prompt)} chars")

    try:
        response = model.generate_content(prompt)
        
        if not response.candidates:
            logger.error(f"No candidates: {response.prompt_feedback}")
            return "⚠️ Gemini មិនអាចឆ្លើយបានទេ។"
        
        if not response.text:
            reason = response.candidates[0].finish_reason
            logger.error(f"Empty response. Reason: {reason}")
            return f"⚠️ ចម្លើយទទេ (reason: {reason})។"
        
        logger.info(f"📥 Response: {len(response.text)} chars")
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
        "/test - test Gemini"
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
    
    msg = f"📊 Status\n"
    msg += f"Model: {MODEL_NAME}\n"
    msg += f"Max output tokens: {generation_config['max_output_tokens']}\n"
    msg += f"Temperature: {generation_config['temperature']}\n"
    msg += f"Documents: {len(docs)}\n"
    msg += f"Total chars: {len(text):,}\n\n"
    for d in docs:
        msg += f"  • {d['name']}: {len(d['text']):,}\n"
    
    await update.message.reply_text(msg)


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = model.generate_content("សូមឆ្លើយថា 'ដំណើរការ'")
        await update.message.reply_text(f"✅ {response.text}")
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text
    await update.message.reply_text("កំពុងស្រាវជ្រាវឯកសារច្បាប់... 🔍")

    try:
        docs = KNOWLEDGE_CACHE.get("docs")
        if not docs:
            load_knowledge()
            docs = KNOWLEDGE_CACHE["docs"]
        
        context_text = build_full_context(docs, MAX_TOTAL_CHARS)
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"🤖 Bot starting | Model: {MODEL_NAME}")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
