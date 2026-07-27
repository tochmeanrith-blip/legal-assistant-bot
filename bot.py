import os
import logging
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

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-flash-lite-latest")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===================== Google Drive =====================
def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
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
            content = service.files().export(fileId=file["id"], mimeType="text/plain").execute()
            text = content.decode("utf-8")
            combined += f"\n\n=== ឯកសារ: {file['name']} ===\n{text}"
        except Exception as e:
            logger.error(f"Error reading {file['name']}: {e}")
    return combined

# ===================== Gemini =====================
def ask_gemini(question, knowledge_base):
    prompt = f"""អ្នកគឺជាអ្នកជំនាញច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា។
ឆ្លើយតាមឯកសារច្បាប់ដែលមាននៅក្នុងទិន្នន័យតែប៉ុណ្ណោះ។
បើគ្មានទិន្នន័យពាក់ព័ន្ធ សូមជម្រាបដោយគួរសម។
ត្រូវបញ្ជាក់ឈ្មោះឯកសារ និងមាត្រា។

--- បណ្តុំឯកសារច្បាប់ ---
{knowledge_base[:130000]}

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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text
    await update.message.reply_text("កំពុងស្វែងរកឯកសារ...")

    try:
        files = get_all_google_docs(DRIVE_FOLDER_ID)
        if not files:
            await update.message.reply_text("មិនអាចរកឃើញឯកសារនៅក្នុង Folder ទេ។")
            return

        knowledge = extract_all_text(files)
        answer = ask_gemini(question, knowledge)
        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(e)
        await update.message.reply_text("មានបញ្ហាកើតឡើង។ សូមសាកល្បងម្តងទៀត។")

# ===================== Main (Simple Port for Render) =====================
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # === Dummy HTTP Server (ដើម្បី Render មិនបង្ហាញ error) ===
    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Bot is running")

    def run_http_server():
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), SimpleHandler)
        print(f"HTTP server running on port {port}")
        server.serve_forever()

    # បើក HTTP Server នៅ thread ដាច់ដោយឡែក
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    # បើក Bot (Polling)
    print("Telegram Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()

# ===================== TEST SERVICE ACCOUNT =====================
def test_service_account():
    """សាកល្បងថា service-account.json ដំណើរការឬអត់"""
    try:
        print("=== កំពុងសាកល្បង Service Account ===")
        
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        
        service = build("drive", "v3", credentials=creds)
        
        # សាកល្បងអាន Folder
        results = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents",
            pageSize=5,
            fields="files(id, name)"
        ).execute()
        
        files = results.get('files', [])
        print(f"✅ Service Account ដំណើរការបានត្រឹមត្រូវ!")
        print(f"រកឃើញ {len(files)} ឯកសារ/ឯកសារក្នុង Folder")
        
        for f in files:
            print(f"  - {f['name']}")
            
        return True
        
    except Exception as e:
        print(f"❌ Service Account មានបញ្ហា: {e}")
        return False

# ===================== របៀបសាកល្បង =====================
if __name__ == "__main__":
    # បើអ្នក run ឯកសារ bot.py ដោយផ្ទាល់ វានឹងសាកល្បង Service Account
    test_service_account()
