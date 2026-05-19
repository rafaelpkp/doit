import os
import flask
import secrets
import psycopg2
import threading
import datetime
import json
import asyncio
from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
import google.generativeai as genai
import zoneinfo
from werkzeug.middleware.proxy_fix import ProxyFix

# Load environment variables from .env
load_dotenv()

app = flask.Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
# A secret key is required to use Flask sessions securely
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super_secret_dev_key")

# IMPORTANT: Allow OAuth2 to work over HTTP for local development. 
# Remove or set to '0' in a production environment with HTTPS.
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Read Google OAuth 2.0 Client credentials from the environment
# (Render deployment)

# Scopes specify the level of access requested.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.settings.readonly"
]

# --- Database Setup ---
def get_db_connection():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                refresh_token TEXT,
                timezone TEXT DEFAULT 'UTC'
            )
        ''')
        conn.commit()
        cursor.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Failed to initialize database: {e}")

# Run initialization automatically on module import (for WSGI servers like Gunicorn)
init_db()

def get_user_data(telegram_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT refresh_token, timezone FROM users WHERE telegram_id = %s', (telegram_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        return {"refresh_token": row[0], "timezone": row[1] if row[1] else "UTC"}
    return None

def get_user_token(telegram_id):
    data = get_user_data(telegram_id)
    return data["refresh_token"] if data else None

def save_user_token(telegram_id, refresh_token, timezone="UTC"):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = '''
        INSERT INTO users (telegram_id, refresh_token, timezone)
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_id)
        DO UPDATE SET refresh_token = EXCLUDED.refresh_token, timezone = EXCLUDED.timezone
    '''
    cursor.execute(query, (telegram_id, refresh_token, timezone))
    conn.commit()
    cursor.close()
    conn.close()

# --- Flask Routes ---
@app.route('/')
def index():
    return 'Welcome! Send /start to the Telegram bot.'

@app.route('/privacy')
def privacy_policy():
    return "<h1>Privacy Policy</h1><p>This application integrates with Google Calendar to help you manage your events. We only access your calendar to create, read, update, and delete events as requested by you. We do not store your personal conversations, event details, or any other personal data on our servers, other than your authentication tokens necessary to provide the service.</p>"

@app.route('/terms')
def terms_of_service():
    return "<h1>Terms of Service</h1><p>By using this bot, you agree that it is provided 'as is' for scheduling support. The developers are not responsible for any damages or issues caused by the use or misuse of this bot, nor are they responsible for any calendar events that are created, modified, or deleted through its usage.</p>"

@app.route('/login')
def login():
    telegram_id = flask.request.args.get('telegram_id')
    
    # Generate a random CSRF token
    csrf_token = secrets.token_urlsafe(16)
    
    # Pass the telegram_id alongside the CSRF token in the OAuth state parameter
    state_str = f"{csrf_token}||{telegram_id}" if telegram_id else csrf_token

    # Initialize the OAuth flow using the client config from ENV
    client_config = json.loads(os.environ.get('GOOGLE_CLIENT_SECRET_JSON', '{}'))
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES
    )

    # Set the redirect URI to match the /callback route on our server
    base_url = os.environ.get('BASE_URL', 'https://ai-assistant-3740.onrender.com').rstrip('/')
    flow.redirect_uri = f"{base_url}/callback"

    # Generate the authorization URL
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true',
        state=state_str # Inject our custom state string containing the telegram_id
    )

    # Store the state so the callback can verify the auth server response
    flask.session['state'] = state
    flask.session['code_verifier'] = flow.code_verifier

    return flask.redirect(authorization_url)

@app.route('/callback')
def callback():
    # Specify the state when creating the flow to verify the server response
    state = flask.session.get('state')
    if not state:
        return "State missing from session. Please try logging in again.", 400

    client_config = json.loads(os.environ.get('GOOGLE_CLIENT_SECRET_JSON', '{}'))
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state
    )
    base_url = os.environ.get('BASE_URL', 'https://ai-assistant-3740.onrender.com').rstrip('/')
    flow.redirect_uri = f"{base_url}/callback"
    flow.code_verifier = flask.session.get('code_verifier')

    # Use the authorization server's response to fetch the OAuth 2.0 tokens
    authorization_response = flask.request.url
    flow.fetch_token(authorization_response=authorization_response)

    # Extract credentials which contain the access and refresh tokens
    credentials = flow.credentials
    
    # Extract the telegram_id from the state parameter
    parts = state.split('||')
    telegram_id = None
    if len(parts) > 1 and parts[1]:
        telegram_id = int(parts[1])
        
        # Fetch the timezone from Google Calendar API
        user_timezone = "UTC"
        try:
            temp_service = build('calendar', 'v3', credentials=credentials, static_discovery=False)
            tz_setting = temp_service.settings().get(setting='timezone').execute()
            user_timezone = tz_setting.get('value', 'UTC')
        except Exception as e:
            print(f"Failed to fetch timezone: {e}")
            
        # Save the refresh token and timezone to our PostgreSQL database
        conn = get_db_connection()
        cursor = conn.cursor()
        query = '''
            INSERT INTO users (telegram_id, refresh_token, timezone)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id)
            DO UPDATE SET refresh_token = EXCLUDED.refresh_token, timezone = EXCLUDED.timezone
        '''
        cursor.execute(query, (telegram_id, credentials.refresh_token, user_timezone))
        conn.commit()
        cursor.close()
        conn.close()
        
        # Proactively message the user
        bot_token = os.environ.get("TELEGRAM_TOKEN")
        if bot_token:
            try:
                bot = Bot(token=bot_token)
                msg = "Success! I'm now connected to your Google Calendar. You can ask me things like 'Book lunch with Lucas on Tuesday at 1pm' or use /agenda to see your schedule."
                asyncio.run(bot.send_message(chat_id=telegram_id, text=msg))
            except Exception as e:
                print(f"Failed to send proactive Telegram message: {e}")

    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Authentication Successful</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                height: 100vh;
                margin: 0;
                background-color: #f0f2f5;
                color: #1c1e21;
                text-align: center;
            }
            .container {
                background: white;
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            }
            h1 { color: #4CAF50; margin-top: 0; }
            p { font-size: 16px; color: #606770; }
        </style>
        <script>
            setTimeout(function() {
                window.location.href = "https://t.me/david_hamoui_bot";
            }, 3000);
        </script>
    </head>
    <body>
        <div class="container">
            <h1>✅ Authenticated!</h1>
            <p>You can now close this window and return to Telegram.</p>
            <p><small>Redirecting to Telegram in 3 seconds...</small></p>
        </div>
    </body>
    </html>
    """
    return html_content

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    token = get_user_token(telegram_id)
    
    if not token:
        base_url = os.environ.get('BASE_URL', 'https://ai-assistant-3740.onrender.com').rstrip('/')
        login_url = f"{base_url}/login?telegram_id={telegram_id}"
        
        # We'll ditch the <a> tag for now so Telegram doesn't block it
        reply_text = (
            f"Welcome! Please connect your Google Calendar by clicking the link below:\n\n"
            f"{login_url}"
        )

        await update.message.reply_text(reply_text) # No ParseMode needed for raw links
    else:
        await update.message.reply_text("You're all set! I've already got your calendar connected.")

async def agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    token = get_user_token(telegram_id)
    
    if not token:
        await update.message.reply_text("Please /start and log in first!")
        return

    # Load client config to initialize Credentials
    client_config = json.loads(os.environ.get('GOOGLE_CLIENT_SECRET_JSON', '{}'))
    
    # client_secret.json structure downloaded from Google usually has 'web' or 'installed'
    creds_data = client_config.get('web', client_config.get('installed', {}))
    client_id = creds_data.get('client_id')
    client_secret = creds_data.get('client_secret')

    creds = Credentials(
        None, # No access token available right now
        refresh_token=token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret
    )

    try:
        service = build('calendar', 'v3', credentials=creds)
        
        # Get the current time in UTC
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        
        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=5, singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])

        if not events:
            await update.message.reply_text("Your calendar is clear!")
            return

        message = "Here are your upcoming events:\n\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'Untitled Event')
            
            # Format date/time
            if 'T' in start:
                # Specific time
                dt = datetime.datetime.fromisoformat(start)
                start_str = dt.strftime('%B %d at %I:%M %p').replace(' 0', ' ')
            else:
                # All day event
                dt = datetime.datetime.strptime(start, '%Y-%m-%d')
                start_str = dt.strftime('%B %d (All day)').replace(' 0', ' ')
                
            message += f"📅 {summary}\n🕒 {start_str}\n\n"
            
        await update.message.reply_text(message)
    except Exception as e:
        await update.message.reply_text(f"An error occurred while fetching your calendar: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = None
    is_voice = bool(update.message.voice)
    try:
        telegram_id = update.effective_user.id
        user_data = get_user_data(telegram_id)
        
        if not user_data or "refresh_token" not in user_data:
            await update.message.reply_text("Please /start and log in first!")
            return
            
        token = user_data["refresh_token"]
        user_timezone = user_data.get("timezone", "UTC")

        if is_voice:
            status_msg = await update.message.reply_text('🎙️ Listening & Processing...')
        else:
            status_msg = await update.message.reply_text('⏳ Processing...')

        gemini_key = os.environ.get("GEMINI_API_KEY")
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-3.1-flash-lite')
        
        # Get current time in User's Timezone
        try:
            tz = zoneinfo.ZoneInfo(user_timezone)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")
            user_timezone = "UTC"
            
        now_tz = datetime.datetime.now(tz)
        current_time = now_tz.strftime("%A, %B %d, %Y at %I:%M %p")

        # --- GOOGLE CALENDAR INITIALIZATION ---
        print("DEBUG: Loading client secrets and rebuilding credentials...")
        client_config = json.loads(os.environ.get('GOOGLE_CLIENT_SECRET_JSON', '{}'))
        
        creds_data = client_config.get('web', client_config.get('installed', {}))
        
        creds = Credentials(
            None,
            refresh_token=token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=creds_data.get('client_id'),
            client_secret=creds_data.get('client_secret')
        )

        print("DEBUG: Connecting to Google Calendar Service...")
        service = build('calendar', 'v3', credentials=creds, static_discovery=False)
        
        # --- FETCH CALENDAR CONTEXT ---
        try:
            events_result = service.events().list(
                calendarId='primary', 
                timeMin=now_tz.isoformat(), 
                maxResults=15, 
                singleEvents=True, 
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            
            calendar_context = "Upcoming Events:\n"
            if not events:
                calendar_context += "No upcoming events found.\n"
            else:
                for event in events:
                    event_id = event.get('id')
                    summary = event.get('summary', 'Untitled Event')
                    start = event['start'].get('dateTime', event['start'].get('date'))
                    end = event['end'].get('dateTime', event['end'].get('date'))
                    calendar_context += f"- ID: {event_id} | Summary: {summary} | Start: {start} | End: {end}\n"
        except Exception as e:
            print(f"Failed to fetch calendar context: {e}")
            calendar_context = "Could not fetch upcoming events.\n"

        prompt = f"""
        Today is {current_time} (Timezone: {user_timezone}).
        
        {calendar_context}
        
        You are a strict calendar assistant. Based on the user's message and the upcoming events, determine if the user wants to create, update, delete, or query an event.
        CRITICAL: Do NOT answer general knowledge questions. If the user greets you or asks what you can do, return action "query" and briefly explain that you can help them create, update, delete, and check events on their calendar. If the user asks something completely unrelated, return {{"action": "error"}}.
        
        Return a JSON object with exactly these fields:
        {{
          "action": "create" | "update" | "delete" | "clarify" | "query" | "error",
          "target_event_id": "string_or_null", // REQUIRED for update or delete. Must exactly match an ID from the Upcoming Events list above. null otherwise.
          "summary": "string_or_null",
          "start_time": "ISO_8601_or_null",
          "end_time": "ISO_8601_or_null", // CRITICAL: If no end time stated, estimate duration (coffee=30m, lunch=1.5h, party=3+h) and calculate exact end_time.
          "location": "string_or_null", // Do not use timezones for this value
          "attendees": ["emails"], // Array of email address strings, or empty array
          "message": "string_or_null" // REQUIRED if action is 'clarify' OR 'query' (e.g., answering schedule questions, or explaining your features). null otherwise.
        }}
        
        If no date or time is found and it's a create/update, return {{"action": "error"}}.
        """

        try:
            if is_voice:
                file = await update.message.voice.get_file()
                await file.download_to_drive("temp_voice.ogg")
                audio_media = genai.upload_file("temp_voice.ogg")
                gemini_input = [prompt, audio_media]
            else:
                text = update.message.text
                gemini_input = prompt + f'\nUser Message: "{text}"'

            response = model.generate_content(
                gemini_input,
                generation_config={"response_mime_type": "application/json", "temperature": 0.1}
            )
            
            response_text = response.text.strip()
            print(f"DEBUG: Gemini JSON: {response_text}")
            
            event_data = json.loads(response_text)
            
            action = event_data.get('action', 'error')
            
            if action == 'error' or "error" in event_data:
                await status_msg.edit_text("I couldn't find enough information. Try saying 'Lunch tomorrow at 1pm'.")
                return
                
            if action == 'clarify':
                clarification_msg = event_data.get('message', "Could you please clarify your request?")
                await status_msg.edit_text(clarification_msg)
                return
                
            elif action == 'query':
                query_msg = event_data.get('message', "I checked your calendar, but I'm not sure.")
                await status_msg.edit_text(query_msg)
                return
                
            target_event_id = event_data.get('target_event_id')
            summary = event_data.get('summary', 'Untitled Event')
            start_time = event_data.get('start_time')
            end_time = event_data.get('end_time')
            
            if action == 'create' and not start_time:
                await status_msg.edit_text("I couldn't find a clear date or time. Try saying 'Lunch tomorrow at 1pm'.")
                return
            
            # --- TIMESTAMP SANITIZATION ---
            # If Gemini only returned a date for end_time (or null), make it 1 hour after start_time
            if start_time and (not end_time or 'T' not in str(end_time)):
                try:
                    # Clean any trailing timezone offsets for parsing local math
                    clean_start = start_time[:19] if start_time else ""
                    if 'T' in clean_start:
                        start_dt = datetime.datetime.fromisoformat(clean_start)
                        end_dt = start_dt + datetime.timedelta(hours=1)
                        end_time = end_dt.strftime('%Y-%m-%dT%H:%M:%S')
                    else:
                        end_time = f"{start_time}T15:00:00" # Fallback if start is also weird
                except Exception as tz_err:
                    print(f"Timestamp normalization fallback triggered: {tz_err}")
                    end_time = f"{start_time[:10] if start_time else '2026-05-17'}T15:00:00"

            location = event_data.get('location')
            attendees_emails = event_data.get('attendees') or []
            
            attendees = [{'email': email} for email in attendees_emails]
            
        except Exception as e:
            print(f"CRITICAL ERROR in Gemini block: {e}")
            await status_msg.edit_text("Something went wrong while processing your request.")
            return

        # --- GOOGLE CALENDAR LOGIC BELOW ---
        try:
            if action == 'delete':
                if not target_event_id:
                    await status_msg.edit_text("I know you want to delete an event, but I'm not sure which one.")
                    return
                service.events().delete(calendarId='primary', eventId=target_event_id).execute()
                await status_msg.edit_text("✅ Event successfully deleted.")
                
            elif action == 'update':
                if not target_event_id:
                    await status_msg.edit_text("I know you want to update an event, but I'm not sure which one.")
                    return
                    
                event_body = {}
                if event_data.get('summary'): # Only patch if provided
                    event_body['summary'] = event_data['summary']
                if start_time:
                    event_body['start'] = {
                        'dateTime': start_time,
                        'timeZone': user_timezone
                    }
                if end_time:
                    event_body['end'] = {
                        'dateTime': end_time,
                        'timeZone': user_timezone
                    }
                if location:
                    event_body['location'] = location
                if attendees:
                    event_body['attendees'] = attendees
                    
                print(f"DEBUG: Attempting to PATCH event: {target_event_id}")
                service.events().patch(calendarId='primary', eventId=target_event_id, body=event_body).execute()
                await status_msg.edit_text(f"✅ Successfully updated the event.")
                
            elif action == 'create':
                event_body = {
                    'summary': summary,
                    'start': {
                        'dateTime': start_time,
                        'timeZone': user_timezone
                    },
                    'end': {
                        'dateTime': end_time,
                        'timeZone': user_timezone
                    }
                }
                
                if location:
                    event_body['location'] = location
                    
                if attendees:
                    event_body['attendees'] = attendees
                
                print(f"DEBUG: Attempting to INSERT event: {summary}")
                service.events().insert(calendarId='primary', body=event_body).execute()
                
                print("DEBUG: Success! Event added.")
                
                # Check if we can safely format the time strings
                try:
                    start_time_str = start_time.split('T')[1][0:5] if start_time and 'T' in start_time else str(start_time)
                    end_time_str = end_time.split('T')[1][0:5] if end_time and 'T' in end_time else str(end_time)
                    await status_msg.edit_text(f"✅ All set! I've added '{summary}' from {start_time_str} to {end_time_str} to your calendar.")
                except Exception:
                    await status_msg.edit_text(f"✅ All set! I've added '{summary}' to your calendar.")
                
        except Exception as e:
            print(f"CRITICAL ERROR in Google block: {e}")
            await status_msg.edit_text(f"I understood the request, but something went wrong updating your calendar. Please try again.")

    except Exception as e:
        # This is your "safety net" for the entire function
        import traceback
        print("\n!!! THE BOT CRASHED !!!")
        print(traceback.format_exc())
        if status_msg:
            await status_msg.edit_text("I hit a snag and couldn't process that message.")
        else:
            await update.message.reply_text("I hit a snag and couldn't process that message.")
    finally:
        if is_voice and os.path.exists("temp_voice.ogg"):
            try:
                os.remove("temp_voice.ogg")
            except Exception:
                pass

# --- Telegram Bot Webhook Integration ---
bot_token = os.environ.get("TELEGRAM_TOKEN")
if bot_token:
    application = ApplicationBuilder().token(bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("agenda", agenda))
    application.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & ~filters.COMMAND, handle_message))
    
    base_url = os.environ.get('BASE_URL', 'https://ai-assistant-3740.onrender.com').rstrip('/')
    webhook_url = f"{base_url}/telegram-webhook"
    
    try:
        asyncio.run(application.bot.set_webhook(url=webhook_url))
        print(f"Webhook set to {webhook_url}")
    except Exception as e:
        print(f"Failed to set webhook: {e}")
else:
    application = None
    print("Warning: TELEGRAM_TOKEN not found in .env file.")

bot_initialized = False

@app.route('/telegram-webhook', methods=['POST'])
async def telegram_webhook():
    global bot_initialized
    if application:
        try:
            if not bot_initialized:
                await application.initialize()
                bot_initialized = True
                
            update = Update.de_json(flask.request.get_json(force=True), application.bot)
            await application.process_update(update)
        except Exception as e:
            print(f"Error processing update: {e}")
    return 'ok'

if __name__ == '__main__':
    # Start Flask server
    port = int(os.environ.get('PORT', 8080))
    print(f"Starting Flask server on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
