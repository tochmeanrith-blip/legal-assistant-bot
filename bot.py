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

# ===================== Main (Fixed for Render) =====================
import asyncio
from aiohttp import web

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # === Dummy Port for Render ===
    async def handle(request):
        return web.Response(text="Bot is running")

    async def start_web_server():
        app = web.Application()
        app.router.add_get("/", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000)))
        await site.start()
        print(f"HTTP server started on port {os.environ.get('PORT', 10000)}")

    async def run_bot():
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        print("Bot started with Polling mode")

    async def main_async():
        await start_web_server()
        await run_bot()
        await asyncio.Event().wait()  # Keep running

    asyncio.run(main_async())

if __name__ == "__main__":
    main()
