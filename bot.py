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

genai.configure(api_key=GEMINI_API_KEY)
# ⭐ ប្តូរទៅ model ដែលមាន context ធំ
model = genai.GenerativeModel("gemini-1.5-flash")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===================== Cache =====================
KNOWLEDGE_CACHE = {"text": None}

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
    combined = ""
    for file in files:
        try:
            content = service.files().export(
                fileId=file["id"], mimeType="text/plain"
            ).execute()
            text = content.decode("utf-8")
            combined += f"\n\n=== ឯកសារ: {file['name']} ===\n{text}"
            logger.info(f"  ✓ Loaded: {file['name']} ({len(text)} chars)")
        except Exception as e:
            logger.error(f"  ✗ Error reading {file['name']}: {e}")
    return combined

def load_knowledge():
    logger.info("📚 Loading knowledge base from Google Drive...")
    files = get_all_google_docs(DRIVE_FOLDER_ID)
    text = extract_all_text(files)
    KNOWLEDGE_CACHE["text"] = text
    logger.info(f"✅ Total: {len(files)} files, {len(text)} characters")

def get_knowledge():
    if KNOWLEDGE_CACHE["text"] is None:
        load_knowledge()
    return KNOWLEDGE_CACHE["text"]

# ===================== RAG Search =====================
def find_relevant_sections(question, knowledge, chunk_size=2000, top_k=8):
    """ស្វែងរក chunks ដែលពាក់ព័ន្ធនឹងសំណួរ"""
    # បំបែក knowledge base ជា chunks (រក្សា document boundary)
    chunks = []
    documents = knowledge.split("=== ឯកសារ:")
    for doc in documents:
        if not doc.strip():
            continue
        doc = "=== ឯកសារ:" + doc
        for i in range(0, len(doc), chunk_size):
            chunks.append(doc[i:i+chunk_size])

    # ពាក្យគន្លឹះ
    keywords = [w.strip() for w in question.split() if len(w.strip()) > 1]

    # ដាក់ពិន្ទុ
    scored = []
    for chunk in chunks:
        score = sum(chunk.count(kw) for kw in keywords)
        scored.append((score, chunk))

    # តម្រៀបយក top_k
    scored.sort(reverse=True, key=lambda x: x[0])
    top_chunks = [chunk for score, chunk in scored[:top_k] if score > 0]

    # បើគ្មាន match → យក top 3 មកមួយចៅ
    if not top_chunks:
        top_chunks = [chunk for _, chunk in scored[:3]]

    return "\n\n---\n\n".join(top_chunks)

# ===================== Gemini =====================
def ask_gemini(question, context_text):
    prompt = f"""អ្នកគឺជាអ្នកជំនាញច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា។
ឆ្លើយតាមឯកសារច្បាប់ដែលមាននៅក្នុងទិន្នន័យតែប៉ុណ្ណោះ។
បើគ្មានទិន្នន័យពាក់ព័ន្ធ សូមជម្រាបដោយគួរសម។
ត្រូវបញ្ជាក់ឈ្មោះឯកសារ និងមាត្រា។

--- បណ្តុំឯកសារច្បាប់ ---
{context_text}

--- សំណួរ ---
{question}

ចម្លើយ៖"""

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return "⚠️ មានបញ្ហាជាមួយ Gemini API។ សូមសាកល្បងម្តងទៀត។"

# ===================== Telegram Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "សួស្តី! ខ្ញុំគឺជា Telegram Bot ជំនួយការប្រព័ន្ធច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា 🇰🇭\n\n"
        "សូមវាយសំណួររបស់លោកអ្នក។"
    )

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command /reload ដើម្បី reload knowledge base"""
    await update.message.reply_text("កំពុង reload ឯកសារ...")
    try:
        load_knowledge()
        await update.message.reply_text("✅ Reload រួចរាល់")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text
    await update.message.reply_text("កំពុងគិត... 🤔")

    try:
        knowledge = get_knowledge()
        relevant = find_relevant_sections(question, knowledge)
        logger.info(f"Question: {question[:50]}... | Relevant chars: {len(relevant)}")
        answer = ask_gemini(question, relevant)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(e)
        await update.message.reply_text(f"មានបញ្ហាកើតឡើង៖ {e}")

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
    logger.info(f"HTTP server running on port {port}")
    server.serve_forever()

# ===================== Main =====================
def main():
    # Preload knowledge
    try:
        load_knowledge()
    except Exception as e:
        logger.error(f"Failed to preload knowledge: {e}")

    # HTTP server
    threading.Thread(target=run_http_server, daemon=True).start()

    # Telegram
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reload", reload_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Telegram Bot is starting...")
    application.run_polling(drop_pending_updates=True)  # ⭐ clear old updates

if __name__ == "__main__":
    main()
