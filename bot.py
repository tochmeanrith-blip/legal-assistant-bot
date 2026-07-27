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

# ⭐ ប្រើ model ដែលអ្នកមាន
MODEL_NAME = "gemini-flash-lite-latest"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===================== Cache =====================
KNOWLEDGE_CACHE = {"text": None, "chunks": None}

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


# ===================== Chunking =====================
def build_chunks(knowledge, chunk_size=1500, overlap=200):
    """បំបែក knowledge ជា chunks តូចៗ ដោយរក្សា document boundary"""
    chunks = []
    documents = knowledge.split("=== ឯកសារ:")
    
    for doc in documents:
        if not doc.strip():
            continue
        doc = "=== ឯកសារ:" + doc
        
        # យកឈ្មោះឯកសារ
        doc_name = doc.split("\n")[0].replace("=== ឯកសារ:", "").strip()
        
        # បំបែកជា chunks ជាមួយ overlap
        i = 0
        while i < len(doc):
            chunk_text = doc[i:i + chunk_size]
            chunks.append({
                "doc_name": doc_name,
                "text": chunk_text
            })
            i += chunk_size - overlap
    
    return chunks


def load_knowledge():
    """Load knowledge base + build chunks"""
    logger.info("📚 Loading knowledge base from Google Drive...")
    files = get_all_google_docs(DRIVE_FOLDER_ID)
    text = extract_all_text(files)
    chunks = build_chunks(text)
    
    KNOWLEDGE_CACHE["text"] = text
    KNOWLEDGE_CACHE["chunks"] = chunks
    
    logger.info(f"✅ Total: {len(files)} files | {len(text)} chars | {len(chunks)} chunks")


def get_chunks():
    if KNOWLEDGE_CACHE["chunks"] is None:
        load_knowledge()
    return KNOWLEDGE_CACHE["chunks"]


# ===================== RAG Search =====================
def score_chunk(chunk_text, keywords):
    """ដាក់ពិន្ទុ chunk តាមចំនួន keyword match"""
    score = 0
    text_lower = chunk_text.lower()
    for kw in keywords:
        kw_lower = kw.lower()
        # រាប់ចំនួនដង
        count = text_lower.count(kw_lower)
        # ពាក្យវែងជាង ២ តួ ទទួលបានពិន្ទុច្រើនជាង
        weight = len(kw) if len(kw) > 2 else 1
        score += count * weight
    return score


def extract_keywords(question):
    """ដក keyword ចេញពីសំណួរ"""
    # Words ដែលមិនសំខាន់ (stop words ខ្មែរ)
    stop_words = {"ចូរ", "ជា", "នេះ", "នោះ", "ដែល", "និង", "ឬ", "តើ", "អ្វី", 
                  "អី", "ខ្ញុំ", "ចង់", "ដឹង", "សូម", "ប្រាប់", "អំពី", "នៅ", "មាន"}
    
    words = question.split()
    keywords = [w.strip("។៖?!.,") for w in words if w.strip("។៖?!.,") not in stop_words]
    keywords = [w for w in keywords if len(w) >= 2]
    return keywords


def find_relevant_chunks(question, chunks, top_k=6):
    """រកchunks ដែលពាក់ព័ន្ធបំផុត"""
    keywords = extract_keywords(question)
    logger.info(f"Keywords: {keywords}")
    
    scored = []
    for chunk in chunks:
        score = score_chunk(chunk["text"], keywords)
        if score > 0:
            scored.append((score, chunk))
    
    # តម្រៀបយក top_k
    scored.sort(reverse=True, key=lambda x: x[0])
    top = scored[:top_k]
    
    logger.info(f"Found {len(scored)} relevant chunks, using top {len(top)}")
    
    # បើគ្មាន match → យក chunk ដំបូងពីឯកសារនីមួយៗ
    if not top:
        seen_docs = set()
        fallback = []
        for chunk in chunks:
            if chunk["doc_name"] not in seen_docs:
                seen_docs.add(chunk["doc_name"])
                fallback.append((0, chunk))
                if len(fallback) >= 3:
                    break
        top = fallback
    
    return [chunk for _, chunk in top]


def build_context(relevant_chunks):
    """បង្កើត context text ពី chunks"""
    # ដាក់ក្រុម chunks តាមឯកសារ
    by_doc = {}
    for chunk in relevant_chunks:
        doc_name = chunk["doc_name"]
        if doc_name not in by_doc:
            by_doc[doc_name] = []
        by_doc[doc_name].append(chunk["text"])
    
    # បង្កើត context
    context = ""
    for doc_name, texts in by_doc.items():
        context += f"\n\n### ឯកសារ: {doc_name} ###\n"
        context += "\n...\n".join(texts)
    
    return context


# ===================== Gemini =====================
def ask_gemini(question, context_text):
    prompt = f"""អ្នកគឺជាអ្នកជំនាញច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា។
ឆ្លើយសំណួរដោយផ្អែកលើឯកសារច្បាប់ដែលបានផ្តល់ជូនខាងក្រោមតែប៉ុណ្ណោះ។

**គោលការណ៍៖**
- ឆ្លើយច្បាស់លាស់ ត្រង់ចំណុច
- តែងតែបញ្ជាក់ឈ្មោះឯកសារ និងលេខមាត្រា
- បើគ្មានទិន្នន័យពាក់ព័ន្ធ សូមជម្រាបថា "ខ្ញុំមិនមានព័ត៌មានពាក់ព័ន្ធនឹងសំណួរនេះនៅក្នុងឯកសារទេ"
- មិនត្រូវប្រឌិតព័ត៌មានឡើយ

--- ឯកសារច្បាប់ពាក់ព័ន្ធ ---
{context_text}

--- សំណួរ ---
{question}

--- ចម្លើយ ---
"""

    logger.info(f"Sending to Gemini: {len(prompt)} chars")

    try:
        response = model.generate_content(prompt)
        
        if not response.candidates:
            logger.error(f"No candidates: {response.prompt_feedback}")
            return "⚠️ Gemini មិនអាចឆ្លើយបានទេ (content blocked)។"
        
        if not response.text:
            reason = response.candidates[0].finish_reason
            logger.error(f"Empty response. Finish reason: {reason}")
            return f"⚠️ ចម្លើយទទេ (reason: {reason})។"
        
        return response.text
        
    except Exception as e:
        import traceback
        logger.error(f"Gemini Error: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        return f"⚠️ Gemini API Error: {str(e)[:200]}"


# ===================== Telegram Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "សួស្តី! ខ្ញុំគឺជា Telegram Bot ជំនួយការប្រព័ន្ធច្បាប់នៃព្រះរាជាណាចក្រកម្ពុជា 🇰🇭\n\n"
        "សូមវាយសំណួររបស់លោកអ្នក។\n\n"
        "Commands:\n"
        "/reload - Reload ឯកសារពី Google Drive\n"
        "/status - មើលស្ថានភាព\n"
        "/test - សាកល្បង Gemini"
    )


async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("កំពុង reload ឯកសារ...")
    try:
        load_knowledge()
        chunks = get_chunks()
        await update.message.reply_text(
            f"✅ Reload រួចរាល់\n"
            f"Chunks: {len(chunks)}\n"
            f"Total chars: {len(KNOWLEDGE_CACHE['text'])}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chunks = KNOWLEDGE_CACHE.get("chunks")
    text = KNOWLEDGE_CACHE.get("text")
    
    if not chunks:
        await update.message.reply_text("⚠️ Knowledge base មិនទាន់ load")
        return
    
    # រាប់ចំនួនឯកសារ
    docs = set(c["doc_name"] for c in chunks)
    
    msg = f"📊 **Status**\n"
    msg += f"Model: {MODEL_NAME}\n"
    msg += f"Documents: {len(docs)}\n"
    msg += f"Chunks: {len(chunks)}\n"
    msg += f"Total chars: {len(text):,}\n\n"
    msg += "**Documents:**\n"
    for d in docs:
        msg += f"  • {d}\n"
    
    await update.message.reply_text(msg)


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = model.generate_content("សូមឆ្លើយថា 'ដំណើរការ'")
        await update.message.reply_text(f"✅ {MODEL_NAME}\nResponse: {response.text}")
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text
    await update.message.reply_text("កំពុងគិត... 🤔")

    try:
        chunks = get_chunks()
        relevant = find_relevant_chunks(question, chunks)
        context_text = build_context(relevant)
        
        logger.info(f"Question: {question[:80]}")
        logger.info(f"Context length: {len(context_text)} chars")
        
        answer = ask_gemini(question, context_text)
        
        # Telegram limit = 4096 chars
        if len(answer) > 4000:
            for i in range(0, len(answer), 4000):
                await update.message.reply_text(answer[i:i+4000])
        else:
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
    logger.info(f"HTTP server on port {port}")
    server.serve_forever()


# ===================== Main =====================
def main():
    # Preload knowledge
    try:
        load_knowledge()
    except Exception as e:
        logger.error(f"Failed to preload: {e}")

    # HTTP server
    threading.Thread(target=run_http_server, daemon=True).start()

    # Telegram bot
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reload", reload_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("test", test_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"🤖 Bot starting with model: {MODEL_NAME}")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
