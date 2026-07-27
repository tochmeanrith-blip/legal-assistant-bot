import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Load environment
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-flash-lite-latest")

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====================== Google Drive Functions ======================
def get_google_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    return build('drive', 'v3', credentials=creds)

def get_all_docs_from_folder(folder_id):
    """អាន Google Docs ទាំងអស់ក្នុង Folder (រួមទាំង Subfolder)"""
    service = get_google_drive_service()
    all_files = []

    def get_files_recursive(parent_id):
        query = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.document'"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        
        for file in results.get('files', []):
            all_files.append(file)
        
        # Subfolder
        subfolders = service.files().list(
            q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder'",
            fields="files(id)"
        ).execute()
        
        for folder in subfolders.get('files', []):
            get_files_recursive(folder['id'])

    get_files_recursive(folder_id)
    return all_files

def extract_text_from_docs(files):
    """ដកស្រង់អត្ថបទពី Google Docs"""
    service = get_google_drive_service()
    combined_text = ""
    
    for file in files:
        try:
            doc = service.files().export(
                fileId=file['id'],
                mimeType='text/plain'
            ).execute()
            text = doc.decode('utf-8')
            combined_text += f"\n\n=== ឯកសារ: {file['name']} ===\n{text}"
        except Exception as e:
            logger.error(f"Error reading {file['name']}: {e}")
    
    return combined_text

# ====================== Gemini Function ======================
def ask_gemini(question, legal_text):
    system_prompt = """អ្នកគឺជាអ្នកជំនាញច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា។
ឆ្លើយតាមឯកសារច្បាប់ដែលមាននៅក្នុងទិន្នន័យតែប៉ុណ្ណោះ។
បើគ្មានទិន្នន័យពាក់ព័ន្ធ សូមជម្រាបដោយគួរសម។
ត្រូវបញ្ជាក់ឈ្មោះឯកសារ និងមាត្រា។"""

    prompt = f"""{system_prompt}

--- បណ្តុំឯកសារច្បាប់ ---
{legal_text[:120000]}

--- សំណួរ ---
{question}

ចម្លើយ៖"""

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return "⚠️ មានបញ្ហាជាមួយ Gemini API។ សូមសាកល្បងម្តងទៀត។"

# ====================== Telegram Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "សួស្តី! ខ្ញុំគឺជា Telegram Bot ជំនួយការប្រព័ន្ធច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា 🇰🇭\n\n"
        "សូមវាយសំណួររបស់លោកអ្នក។"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_question = update.message.text
    chat_id = update.message.chat_id

    await update.message.reply_text("កំពុងស្វែងរកឯកសារ...")

    try:
        files = get_all_docs_from_folder(DRIVE_FOLDER_ID)
        if not files:
            await update.message.reply_text("មិនអាចរកឃើញឯកសារនៅក្នុង Folder ទេ។")
            return

        legal_text = extract_text_from_docs(files)
        
        answer = ask_gemini(user_question, legal_text)
        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("មានបញ្ហាកើតឡើង។ សូមសាកល្បងម្តងទៀត។")

# ====================== Main ======================
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Webhook for Render.com
    port = int(os.environ.get("PORT", 8443))
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        webhook_url=os.environ.get("RENDER_EXTERNAL_URL")
    )

if __name__ == "__main__":
    main()
