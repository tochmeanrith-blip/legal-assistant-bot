# ============================================================
# Cambodia Legal Assistant Bot - v9
# Python Telegram Bot
# Updated: July 2026
# ============================================================

import os
import re
import asyncio
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ============================================================
# ⚙️ CONFIGURATION
# ============================================================

TOKEN   = os.getenv("TELEGRAM_TOKEN")
GAS_URL = os.getenv("GAS_URL")

VERSION = "v9"

# Khmer digit range for regex
KHMER_DIGITS = "០-៩"


# ============================================================
# 🌐 GAS API CALL
# ============================================================

def call_gas(payload: dict) -> dict:
    """
    POST to Google Apps Script Web App.
    Returns parsed JSON or error dict.
    Timeout = 60s (Gemini needs more time).
    """
    try:
        response = requests.post(
            GAS_URL,
            json=payload,
            timeout=60
        )
        return response.json()

    except requests.exceptions.Timeout:
        return {
            "status": "error",
            "formatted": "⏱️ API timeout — សូមសាកល្បងម្តងទៀត"
        }
    except Exception as e:
        return {
            "status": "error",
            "formatted": f"❌ API Error: {str(e)}"
        }


# ============================================================
# 🔍 API HELPERS
# ============================================================

def api_search(query: str) -> dict:
    return call_gas({
        "action": "search",
        "query": query
    })


def api_article(number: str, doc_filter: str = "") -> dict:
    return call_gas({
        "action": "article",
        "number": number,
        "filter": doc_filter
    })


def api_list_docs() -> dict:
    return call_gas({"action": "list_docs"})


def api_ping() -> dict:
    return call_gas({"action": "ping"})


# ============================================================
# ✂️ SMART MESSAGE SPLITTER
# ============================================================

def smart_split(text: str, max_length: int = 4000) -> list[str]:
    """
    Split long messages intelligently:
    Priority: split at --- > \n\n > \n > char limit
    """
    if len(text) <= max_length:
        return [text]

    parts = []
    current = ""

    # Try splitting by "---" sections
    sections = text.split("\n---\n")

    for section in sections:
        candidate = (current + "\n---\n" + section) if current else section

        if len(candidate) <= max_length:
            current = candidate
        else:
            # Save current part
            if current:
                parts.append(current.strip())

            # If single section > max_length, split by paragraphs
            if len(section) > max_length:
                paragraphs = section.split("\n\n")
                current = ""
                for para in paragraphs:
                    candidate2 = (current + "\n\n" + para) if current else para
                    if len(candidate2) <= max_length:
                        current = candidate2
                    else:
                        if current:
                            parts.append(current.strip())
                        # Last resort: hard split
                        if len(para) > max_length:
                            for i in range(0, len(para), max_length):
                                parts.append(para[i:i+max_length])
                        else:
                            current = para
            else:
                current = section

    if current:
        parts.append(current.strip())

    return parts if parts else [text]


# ============================================================
# 📨 SEND LONG MESSAGE
# ============================================================

async def send_long_message(
    update: Update,
    text: str,
    parse_mode: str = "Markdown"
) -> None:
    """
    Send message with auto-pagination for long texts.
    Falls back to plain text if Markdown parse fails.
    """
    MAX = 4000
    parts = smart_split(text, MAX)
    total = len(parts)

    for i, part in enumerate(parts):
        # Add page indicator if multiple parts
        if total > 1:
            header = f"📄 *ទំព័រ {i+1}/{total}*\n\n"
            content = header + part
        else:
            content = part

        # Try with Markdown first
        try:
            await update.message.reply_text(
                content,
                parse_mode=parse_mode
            )
        except Exception:
            # Fallback: plain text (strip markdown)
            try:
                plain = content \
                    .replace("*", "") \
                    .replace("_", "") \
                    .replace("`", "")
                await update.message.reply_text(plain)
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Error sending message: {str(e)}"
                )

        # Rate limit protection
        if total > 1:
            await asyncio.sleep(0.5)


# ============================================================
# 🎯 COMMAND HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        f"🇰🇭 *សួស្តី\\! ខ្ញុំជា Bot ជំនួយការផ្នែកច្បាប់កម្ពុជា*\n\n"
        f"⚖️ *Version {VERSION}*\n\n"
        f"✅ ស្វែងរកមាត្រាជាក់លាក់\n"
        f"✅ ស្វែងរកតាមពាក្យគន្លឹះ\n"
        f"✅ លទ្ធផលរៀបចំស្អាតដោយ Gemini AI\n"
        f"✅ ខ្លឹមសារត្រឹមត្រូវ ១០០%\n\n"
        f"📝 *របៀបប្រើ:*\n"
        f"• វាយពាក្យ: `លួច`\n"
        f"• វាយ: `មាត្រា ១៨១`\n"
        f"• ប្រើ: `/article ១៨១ ព្រហ្មទណ្ឌ`\n\n"
        f"💡 /help — ព័ត៌មានបន្ថែម"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📚 *មគ្គុទ្ទេសក៍ប្រើប្រាស់*\n\n"
        "🔹 *ស្វែងរកតាមពាក្យ:*\n"
        "   វាយ `លួច` → រកមាត្រាពាក់ព័ន្ធ\n"
        "   វាយ `មូលហេតុនៃទោស` → រកមាត្រា\n\n"
        "🔹 *ស្វែងរកមាត្រា:*\n"
        "   `/article ១៨១`\n"
        "   `/article ១៨១ ព្រហ្មទណ្ឌ`\n"
        "   `មាត្រា ៣៥៣` (វាយដោយផ្ទាល់)\n\n"
        "🔹 *Commands:*\n"
        "   /docs — បញ្ជីឯកសារ\n"
        "   /ping — ពិនិត្យ API\n"
        "   /help — ជំនួយ\n\n"
        "⚖️ *ឯកសារបច្ចុប្បន្ន:*\n"
        "   📄 ក្រមព្រហ្មទណ្ឌ ២០០៩\n"
        "   📄 ក្រមនីតិវិធីព្រហ្មទណ្ឌ ២០០៧\n"
        "   📄 ក្រមនីតិវិធីរដ្ឋប្បវេណី ២០០៧"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def article_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    /article [number] [optional: doc filter]
    Examples:
      /article ១៨១
      /article 181 ព្រហ្មទណ្ឌ
    """
    if not context.args:
        await update.message.reply_text(
            "⚠️ *សូមបញ្ជាក់លេខមាត្រា*\n\n"
            "ឧទាហរណ៍:\n"
            "• `/article ១៨១`\n"
            "• `/article ១៨១ ព្រហ្មទណ្ឌ`",
            parse_mode="Markdown"
        )
        return

    number     = context.args[0]
    doc_filter = " ".join(context.args[1:]) if len(context.args) > 1 else ""

    # Show loading indicator
    loading_msg = await update.message.reply_text(
        f"⏳ *កំពុងស្វែងរក មាត្រា {number}...*",
        parse_mode="Markdown"
    )

    # Call API
    result = api_article(number, doc_filter)

    # Delete loading message
    try:
        await loading_msg.delete()
    except Exception:
        pass

    # Handle response
    if result.get("status") == "ok":
        if result.get("found"):
            formatted = result.get("formatted", "")
            await send_long_message(update, formatted)
        else:
            await update.message.reply_text(
                result.get("formatted", f"❌ រកមិនឃើញ *មាត្រា {number}*"),
                parse_mode="Markdown"
            )
    else:
        error_msg = result.get("formatted", "❌ API Error")
        await update.message.reply_text(error_msg, parse_mode="Markdown")


async def docs_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """List all available documents"""
    loading_msg = await update.message.reply_text("⏳ *កំពុងទាញបញ្ជីឯកសារ...*",
                                                   parse_mode="Markdown")

    result = api_list_docs()

    try:
        await loading_msg.delete()
    except Exception:
        pass

    if result.get("status") == "ok":
        docs = result.get("docs", [])
        count = result.get("count", 0)

        text = f"📚 *ឯកសារច្បាប់ ({count} ឯកសារ)*\n\n"
        for i, doc_name in enumerate(docs, 1):
            text += f"{'📄'} {i}\\. {doc_name}\n"

        text += "\n💡 ប្រើ `/article [លេខ] [ឈ្មោះក្រម]` ដើម្បីស្វែងរក"

        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ មិនអាចទាញបញ្ជីឯកសារ")


async def ping_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Test API connection"""
    loading_msg = await update.message.reply_text("⏳ Testing...")

    result = api_ping()

    try:
        await loading_msg.delete()
    except Exception:
        pass

    if result.get("status") == "ok":
        await update.message.reply_text(
            f"✅ *API ដំណើរការល្អ\\!*\n\n"
            f"🤖 Version: `{result.get('version', '?')}`\n"
            f"💬 {result.get('message', '')}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ *API Error*\n{result.get('formatted', 'Unknown error')}",
            parse_mode="Markdown"
        )


# ============================================================
# 💬 TEXT MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handle regular text messages.
    Auto-detect:
    - "មាត្រា ១៨១" → article search
    - other text   → keyword search
    """
    text = update.message.text.strip()

    if not text:
        return

    # ── Auto-detect Article Query ──────────────────────────
    article_pattern = re.compile(
        r"^មាត្រា\s*([0-9" + KHMER_DIGITS + r"]+)(.*)",
        re.UNICODE
    )
    article_match = article_pattern.match(text)

    if article_match:
        number     = article_match.group(1).strip()
        doc_filter = article_match.group(2).strip()

        loading_msg = await update.message.reply_text(
            f"⏳ *ស្វែងរក មាត្រា {number}...*",
            parse_mode="Markdown"
        )

        result = api_article(number, doc_filter)

        try:
            await loading_msg.delete()
        except Exception:
            pass

        if result.get("status") == "ok" and result.get("found"):
            await send_long_message(update, result["formatted"])
        else:
            await update.message.reply_text(
                result.get("formatted", f"❌ រកមិនឃើញ មាត្រា {number}"),
                parse_mode="Markdown"
            )
        return

    # ── Keyword Search ─────────────────────────────────────
    loading_msg = await update.message.reply_text(
        f"🔍 *ស្វែងរក:* `{text}`",
        parse_mode="Markdown"
    )

    result = api_search(text)

    try:
        await loading_msg.delete()
    except Exception:
        pass

    if result.get("status") == "ok":
        count = result.get("count", 0)
        formatted = result.get("formatted", "")

        if count > 0:
            await send_long_message(update, formatted)
        else:
            await update.message.reply_text(
                formatted or f"❌ រកមិនឃើញ: *{text}*",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            result.get("formatted", "❌ API Error"),
            parse_mode="Markdown"
        )


# ============================================================
# 🚀 MAIN
# ============================================================

def main() -> None:
    print(f"🤖 Starting Cambodia Legal Bot {VERSION}...")
    print(f"📡 GAS URL: {GAS_URL[:50]}..." if GAS_URL else "❌ No GAS_URL!")

    app = ApplicationBuilder().token(TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_cmd))
    app.add_handler(CommandHandler("article", article_cmd))
    app.add_handler(CommandHandler("docs",    docs_cmd))
    app.add_handler(CommandHandler("ping",    ping_cmd))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message
    ))

    print("✅ Bot started! Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
