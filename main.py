import os
import logging
from typing import List, Tuple, Optional
from fastapi import FastAPI, Request, HTTPException, Header, Depends
import psycopg2
from psycopg2 import sql
import requests
from typing import List

# 1. Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 2. Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ALLOWED_USER_IDS = [int(uid) for uid in os.getenv("ALLOWED_USER_IDS", "12345678,87654321").split(",")]
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "your_random_secret_string_123")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
USER_SESSIONS: Dict[int, Dict[str, Any]] = {}

app = FastAPI(title="Closet Tracker Bot")

def verify_telegram_secret(x_telegram_bot_api_secret_token: str = Header(None)):
    """Validates that incoming requests originate directly from Telegram."""
    if x_telegram_bot_api_secret_token != TELEGRAM_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Unauthorized request, wrong secret token")

def is_authorized_user(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS

# 3. Database Connection Helper
def get_db_connection():
    """Establishes connection to PostgreSQL (Supabase/Neon)."""
    return psycopg2.connect(DATABASE_URL)

# 4. Helper Functions for Telegram & Database
def send_telegram_message(chat_id: int, text: str):
    """Sends a formatted text message back to the Telegram user."""
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send message to Telegram: {e}")

def send_single_photo_with_buttons(chat_id: int, photo_file_id: str, caption: str, buttons: list):
    """Sends an item photo with action buttons attached directly under it."""
    url = f"{TELEGRAM_API}/sendPhoto"
    keyboard = {
        "inline_keyboard": [[{"text": b_text, "callback_data": b_data}] for b_text, b_data in buttons]
    }
    payload = {
        "chat_id": chat_id,
        "photo": photo_file_id,
        "caption": caption,
        "reply_markup": keyboard,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def send_media_group(chat_id: int, media_list: list):
    """Sends multiple photos as a single Telegram photo album (up to 10 photos)."""
    url = f"{TELEGRAM_API}/sendMediaGroup"
    payload = {"chat_id": chat_id, "media": media_list}
    requests.post(url, json=payload)

def answer_callback_query(callback_id: str):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_id})

def save_item_to_db(session_data: dict) -> bool:
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        query = """
            INSERT INTO clothes_item (item_name, category, location, image_id, status)
            VALUES (%s, %s, %s, %s, %s);
        """
        cur.execute(query, (
            session_data.get("item_name"),
            session_data.get("category"),
            session_data.get("location"),
            session_data.get("file_id"),
            session_data.get("status", "Clean")
        ))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"DB Error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

def update_item_field(item_id: int, field_name: str, new_value: str) -> bool:
    """Dynamically updates a specific column (e.g., 'name', 'category', 'location') for an item."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Use sql.Identifier for safe column naming
                query = sql.SQL("UPDATE clothes_item SET {} = %s WHERE id = %s;").format(
                    sql.Identifier(field_name)
                )
                cur.execute(query, (new_value, item_id))
                conn.commit()
                return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to update {field_name} for item {item_id}: {e}")
        return False

def delete_item_from_db(item_id: int) -> bool:
    """Deletes an item from the closet table by ID."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM clothes_item WHERE id = %s;", (item_id,))
                conn.commit()
                return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to delete item {item_id}: {e}")
        return False
    
def get_items_by_location_and_category(location_name: str, category_name: Optional[str] = None) -> List[Tuple]:
    """Retrieves clothes filtered by location and optionally by category."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    if category_name and category_name != "ALL":
        query = """
            SELECT id, item_name, category, location, image_id, status, comments
            FROM clothes_item
            WHERE LOWER(location) = LOWER(%s) 
              AND LOWER(category) = LOWER(%s);
        """
        cur.execute(query, (location_name, category_name))
    else:
        query = """
            SELECT id, item_name, category, location, image_id, status, comments
            FROM clothes_item
            WHERE LOWER(location) = LOWER(%s);
        """
        cur.execute(query, (location_name,))
        
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_item_by_id(item_id: int) -> Optional[Tuple]:
    """Fetches complete information for a single item."""
    conn = get_db_connection()
    cur = conn.cursor()
    query = """
        SELECT id, item_name, category, location, image_id, status, comments
        FROM clothes_item
        WHERE id = %s;
    """
    cur.execute(query, (item_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

# Helper to fetch dynamic list options from DB
def fetch_options_from_db(table_name: str) -> List[str]:
    """Queries PostgreSQL to get dynamic option lists for buttons."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                query = sql.SQL("SELECT name FROM {} ORDER BY id ASC;").format(
                    sql.Identifier(table_name)
                )
                cur.execute(query)
                return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching from {table_name}: {e}")
        return []
# --- Telegram API Helpers ---

def edit_message_text(chat_id: int, message_id: int, text: str, buttons: list = None):
    """Edits an existing message with updated inline buttons."""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": b_text, "callback_data": b_data}] for b_text, b_data in buttons]
        }
    requests.post(f"{TELEGRAM_API}/editMessageText", json=payload)


def send_inline_keyboard(chat_id: int, text: str, buttons: list):
    """Sends a new message with inline buttons."""
    keyboard = {
        "inline_keyboard": [[{"text": b_text, "callback_data": b_data}] for b_text, b_data in buttons]
    }
    payload = {"chat_id": chat_id, "text": text, "reply_markup": keyboard, "parse_mode": "Markdown"}
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)


def answer_callback_query(callback_id: str):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_id})

@app.post("/webhook", dependencies=[Depends(verify_telegram_secret)])
async def telegram_webhook(request: Request):
    data = await request.json()
    # Get user_id from message or callback_query
    user_id = None
    if "message" in data:
        user_id = data["message"]["from"]["id"]
        chat_id = data["message"]["chat"]["id"]
    elif "callback_query" in data:
        user_id = data["callback_query"]["from"]["id"]
        chat_id = data["callback_query"]["message"]["chat"]["id"]

    # Reject unauthorized users
    if not user_id or not is_authorized_user(user_id):
        logger.warning(f"Unauthorized access attempt by user_id: {user_id}")
        send_telegram_message(chat_id, "⛔ *Access Denied:* You are not authorized to access this closet database.")
        return {"status": "forbidden"}
    # -------------------------------------------------------------
    # BRANCH A: Standard Messages (Text Commands & Photo Uploads)
    # -------------------------------------------------------------
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        # 1. PHOTO UPLOAD -> Start "Add Clothes" Flow
        if "photo" in msg:
            file_id = msg["photo"][-1]["file_id"]
            item_name = msg.get("caption", "Unnamed Item").strip()

            # Store state in memory
            USER_SESSIONS[chat_id] = {
                "file_id": file_id,
                "item_name": item_name
            }

            # Fetch categories dynamically from database
            categories = fetch_options_from_db("category_list") or ["Tops", "Bottoms", "Outerwear"]
            buttons = [(cat, f"cat:{cat}") for cat in categories]
            send_inline_keyboard(chat_id, f"🖼️ Received **{item_name}**!\n\nSelect a **Category**:", buttons)

        # 2. COMMAND: /list [Location] -> Trigger "List Clothes" Flow
        elif text.startswith("/list"):
            # Check if user typed direct location (e.g., /list Manor) or wants interactive selection
            param = text.replace("/list", "", 1).strip()
            
            if param:
                # Direct location provided -> Prompt for category right away
                categories = fetch_options_from_db("category_list") or ["Tops", "Bottoms", "Outerwear"]
                buttons = [(cat, f"listcat:{param}:{cat}") for cat in categories]
                buttons.append(("📦 All Categories", f"listcat:{param}:ALL"))
                
                send_inline_keyboard(chat_id, f"📍 Location: **{param}**\n\nSelect a **Category** to filter by:", buttons)
            else:
                # Step 1: Prompt for Location first
                locations = fetch_options_from_db("location_list") or ["Manor", "GC"]
                buttons = [(loc, f"listloc:{loc}") for loc in locations]
                
                send_inline_keyboard(chat_id, "🔍 **Browse Closet**\n\nPlease select a **Location** first:", buttons)

        # update item name
        elif chat_id in USER_SESSIONS and USER_SESSIONS[chat_id]["state"] == "awaiting_new_name":
            USER_SESSIONS[chat_id]["state"] = ""
            item_id = USER_SESSIONS[chat_id]["item_id"]
            success = update_item_field(item_id, "item_name", text)
            reply = f"✅ Name updated to **{text}**!" if success else "❌ Failed to update name."
            send_telegram_message(chat_id, reply)
            return {"status": "ok"}

        # update item comments
        elif chat_id in USER_SESSIONS and USER_SESSIONS[chat_id]["state"] == "awaiting_new_comment":
            USER_SESSIONS[chat_id]["state"] = ""
            item_id = USER_SESSIONS[chat_id]["item_id"]
            success = update_item_field(item_id, "comments", text)
            reply = f"✅ Comment updated to **{text}**!" if success else "❌ Failed to update comment."
            send_telegram_message(chat_id, reply)
            return {"status": "ok"}


        # 3. COMMAND: /start -- default
        else:
            welcome_text = (
                "👋 **Welcome to Closet Tracker!**\n\n"
                "• **To Add Clothes:** Simply send a **photo** with a caption (e.g., *Black Hoodie*).\n"
                "• **To List Clothes:** Type `/list [Location]` (e.g., `/list Manor` or `/list GC`)."
            )
            send_inline_keyboard(chat_id, welcome_text, [])

    # -------------------------------------------------------------
    # BRANCH B: Callback Queries (Inline Button Click Events)
    # -------------------------------------------------------------
    elif "callback_query" in data:
        cb = data["callback_query"]
        callback_id = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        callback_data = cb["data"]

        answer_callback_query(callback_id)

        # --- SUB-FLOW 1: ADD ITEM STEPS ---
        
        # Step 1: Category Selected -> Prompt for Location
        if callback_data.startswith("cat:"):
            session = USER_SESSIONS.get(chat_id)
            if not session:
                edit_message_text(chat_id, msg_id, "⚠️ Session expired. Please re-upload the photo.")
                return {"status": "ok"}

            category = callback_data.split(":", 1)[1]
            session["category"] = category

            locations = fetch_options_from_db("location_list") or ["Manor", "GC"]
            buttons = [(loc, f"loc:{loc}") for loc in locations]
            edit_message_text(chat_id, msg_id, f"🏷️ Category: **{category}**\n\nSelect a **Location**:", buttons)

        # Step 2: Location Selected -> Finalize & Save to DB, status default to clean
        elif callback_data.startswith("loc:"):
            session = USER_SESSIONS.get(chat_id)
            if not session:
                edit_message_text(chat_id, msg_id, "⚠️ Session expired. Please re-upload the photo.")
                return {"status": "ok"}

            location = callback_data.split(":", 1)[1]
            session["location"] = location
            session["status"] = "Clean"

            success = save_item_to_db(session)
            if success:
                summary = (
                    f"✅ **Item Saved!**\n\n"
                    f"• **Item:** {session['item_name']}\n"
                    f"• **Category:** {session['category']}\n"
                    f"• **Location:** {session['location']}\n"
                    f"• **Status:** {session['status']}"
                )
                edit_message_text(chat_id, msg_id, summary)
            else:
                edit_message_text(chat_id, msg_id, "❌ Error saving item to database.")

            USER_SESSIONS.pop(chat_id, None)

        # --- SUB-FLOW 2: INSPECT ITEM DETAILS ---
        # STEP 1: Location Selected for Listing -> Prompt for Category
        elif callback_data.startswith("listloc:"):
            location = callback_data.split(":", 1)[1]
            
            categories = fetch_options_from_db("category_list") or ["Tops", "Bottoms", "Outerwear"]
            buttons = [(cat, f"listcat:{location}:{cat}") for cat in categories]
            buttons.append(("📦 All Categories", f"listcat:{location}:ALL"))
            
            edit_message_text(
                chat_id, msg_id, 
                f"📍 Location: **{location}**\n\nSelect a **Category** to filter by:", 
                buttons
            )

        # STEP 2: Category Selected -> Fetch & Render Items
        elif callback_data.startswith("listcat:"):
            _, location, category = callback_data.split(":", 2)
            
            items = get_items_by_location_and_category(location, category)
            cat_display = "All Categories" if category == "ALL" else category

            if not items:
                edit_message_text(chat_id, msg_id, f"🧥 No items found under **{cat_display}** at **{location}**.")
                return {"status": "ok"}

            # Update the selection message so user knows what's loaded
            edit_message_text(chat_id, msg_id, f"🔍 Showing **{cat_display}** at **{location}** ({len(items)} items found):")

            media_group = []
            detail_buttons = []

            for item_id, item_name, cat, loc, image_id, status, comments in items:
                # Add up to 10 photos to Telegram Media Album
                if image_id and len(media_group) < 10:
                    media_group.append({
                        "type": "photo",
                        "media": image_id,
                        "caption": f"#{item_id}: {item_name} ({cat})"
                    })
                # Add inspection button for every item
                detail_buttons.append((f"ℹ️ Details: #{item_id} {item_name}", f"detail:{item_id}"))

            # Send photo album if images exist
            if media_group:
                send_media_group(chat_id, media_group)

            # Send inspection buttons list
            send_inline_keyboard(
                chat_id,
                f"👕 **Tap an item below for full details:**",
                detail_buttons
            )
        elif callback_data.startswith("detail:"):
            item_id = int(callback_data.split(":", 1)[1])
            item = get_item_by_id(item_id)

            if not item:
                send_inline_keyboard(chat_id, "❌ Item not found.", [])
                return {"status": "ok"}

            item_id, item_name, category, location, image_id, status, comments = item

            caption = (
                f"🧥 **{item_name}** (ID: #{item_id})\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏷️ **Category:** {category or 'N/A'}\n"
                f"📍 **Location:** {location or 'N/A'}\n"
                f"🧼 **Status:** {status or 'Clean'}\n"
                f"📝 **Comments:** {comments or 'None'}"
            )

            action_buttons = [
                ("✏️ Change Name", f"editname:{item_id}"), 
                ("📝 Change comments", f"editcomment:{item_id}"),
                ("🏷️ Change Category", f"changecat:{item_id}"),
                ("📍 Change Location", f"changeloc:{item_id}"),
                ("🧼 Change Status", f"changest:{item_id}"),
                ("🗑️ Delete Item", f"delete:{item_id}")

            ]

            if image_id:
                send_single_photo_with_buttons(chat_id, image_id, caption, action_buttons)
            else:
                send_inline_keyboard(chat_id, caption, action_buttons)
        
        # Edit flow
        # --- 1. CHANGE NAME ACTION ---
        elif callback_data.startswith("editname:"):
            item_id = int(callback_data.split(":")[1])
            USER_SESSIONS[chat_id] = {"state": "awaiting_new_name", "item_id": item_id}
            send_telegram_message(chat_id, "Please type the new name for this item:")

        elif callback_data.startswith("editcomment:"):
            item_id = int(callback_data.split(":")[1])
            USER_SESSIONS[chat_id] = {"state": "awaiting_new_comment", "item_id": item_id}
            send_telegram_message(chat_id, "Please type a comment for this item:")

        # --- 2.1 CHANGE ACTION ---
        elif callback_data.startswith("change"):
            parts = callback_data.split(":")
            change_list = {
                # action: [list db table name, message, ]
                "changeloc": ["location_list", "Select a new location:", "updateloc"],
                "changecat": ["category_list", "Select a new category:", "updatecat"],
                "changest": ["status_list", "Select a new status:", "updatest"]
            }
            param = change_list[parts[0]]
            options = fetch_options_from_db(param[0])
            item_id = int(parts[1])

            # Build inline buttons for available options
            buttons = [
                (opt, f"callback_data:{param[2]}:{item_id}:{opt}") for opt in options
            ]
            send_inline_keyboard(chat_id, param[1], buttons)


        # --- 2.2 SAVE SELECTION ---
        elif callback_data.startswith("update"):
            parts = callback_data.split(":")
            item_id = int(parts[2])
            new_param = parts[3]
            field = {
                "updateloc": "location",
                "updatecat": "category",
                "updatest": "status"
            }

            success = update_item_field(item_id, field[parts[1]], new_param)
            
            msg = f"✅ {field[parts[1]]} updated to **{new_param}**!" if success else f"❌ Failed to update {field[parts[1]]}."
            send_telegram_message(chat_id, msg)


        # --- 4. DELETE ITEM ACTION ---
        elif callback_data.startswith("delete"):
            item_id = int(callback_data.split(":")[1])

            success = delete_item_from_db(item_id)
            msg = "🗑️ Item deleted successfully!" if success else "❌ Failed to delete item."
            send_telegram_message(chat_id, msg)
        

    return {"status": "ok"}

# 5. Webhook Endpoint
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Clothes Tracker API is live"}    