import os
import discord
import asyncio
import json
import hashlib
import time
import sqlite3
from discord.ext import commands, tasks
from discord.ui import Button, View
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Thread, Lock
from dotenv import load_dotenv
from collections import defaultdict

# Load environment variables
load_dotenv()

# Disable voice support to avoid audioop import issues
os.environ["DISCORD_INTERACTIONS"] = "false"
os.environ["DISCORD_VOICE"] = "false"

# Your Discord bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Check if token exists
if not BOT_TOKEN:
    print("❌ ERROR: BOT_TOKEN not found in environment variables!")
    print("Please check your .env file and make sure it contains BOT_TOKEN=your_token_here")
    exit(1)

ROLE_ID = int(os.getenv('ROLE_ID', 1281782820074688542))
CHANNEL_ID = int(os.getenv('CHANNEL_ID', 1411206861499400192))
LOG_CHANNEL_ID = 1411710161868820671
DB_FILE = "keys.db"

# Get port from Render environment variable or use default
PORT = int(os.environ.get('PORT', 10000))
print(f"Using port: {PORT}")

# Create a lock for thread-safe database operations
db_lock = Lock()

# Rate limiting - one generation per 6 hours
class RateLimiter:
    def __init__(self, rate, per):
        self.rate = rate
        self.per = per
        self.allowances = defaultdict(list)
    
    def is_limited(self, user_id):
        now = time.time()
        self.allowances[user_id] = [t for t in self.allowances[user_id] if now - t < self.per]
        
        if len(self.allowances[user_id]) < self.rate:
            self.allowances[user_id].append(now)
            return False
        return True

# Create rate limiter (1 generation per 6 hours)
key_limiter = RateLimiter(1, 21600)  # 1 generation per 6 hours

# Flask app for keeping the bot alive and handling verification
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

@app.route('/')
def home():
    return "✅ Bot is alive and running!"

# Add logging function to bot
def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Print to console (Render will capture this)
    print(f"[{timestamp}] {message}")
    # Also write to file for local debugging
    try:
        with open("bot_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except:
        pass

@app.route('/verify_key', methods=['POST', 'OPTIONS'])
def verify_key():
    try:
        # Handle preflight request
        if request.method == 'OPTIONS':
            response = jsonify({"status": "ok"})
            response.headers.add('Access-Control-Allow-Origin', '*')
            response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
            response.headers.add('Access-Control-Allow-Methods', 'POST')
            return response
            
        data = request.get_json()
        if not data or 'key' not in data:
            log_message("No key provided in request")
            return jsonify({"valid": False, "error": "No key provided"})
        
        key = data['key'].strip().upper()
        
        # Check key in database
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT key, user_id, active FROM activation_keys WHERE key = ?', (key,))
        result = c.fetchone()
        conn.close()
        
        if result:
            key_found, user_id, active = result
            if active == 1:
                log_message(f"✅ Key found and active: {key} for user {user_id}")
                response = jsonify({"valid": True})
                response.headers.add('Access-Control-Allow-Origin', '*')
                return response
            else:
                log_message(f"❌ Key found but inactive: {key} for user {user_id}")
                response = jsonify({"valid": False, "error": "Key is inactive"})
                response.headers.add('Access-Control-Allow-Origin', '*')
                return response
        else:
            log_message(f"❌ Key not found: {key}")
            response = jsonify({"valid": False, "error": "Invalid key"})
            response.headers.add('Access-Control-Allow-Origin', '*')
            return response
            
    except Exception as e:
        log_message(f"Error verifying key: {e}")
        response = jsonify({"valid": False, "error": str(e)})
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response

# Set up intents
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Database initialization
def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS activation_keys (
                key TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                discriminator TEXT NOT NULL,
                creation_date TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                discord_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                deactivation_date TEXT,
                deactivation_reason TEXT
            )
        ''')
        conn.commit()
        
        # Debug: List all current keys
        c.execute('SELECT key, user_id, active FROM activation_keys')
        all_keys = c.fetchall()
        log_message(f"📊 Database initialized. Current keys: {len(all_keys)}")
        for key, user_id, active in all_keys:
            log_message(f"   Key: {key}, User: {user_id}, Active: {active}")
        
        conn.close()

def debug_user_keys(user_id):
    """Debug function to see all keys for a user"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT key, active, creation_date FROM activation_keys WHERE user_id = ? ORDER BY creation_date DESC', (str(user_id),))
        user_keys = c.fetchall()
        conn.close()
        
        log_message(f"🔍 DEBUG: User {user_id} has {len(user_keys)} keys:")
        for key, active, date in user_keys:
            log_message(f"   Key: {key}, Active: {active}, Date: {date}")
        
        return user_keys

def get_user_active_key(user_id):
    """Get active key for a user - SINGLE SOURCE OF TRUTH"""
    user_id_str = str(user_id)
    
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            SELECT key, user_id, username, discriminator, creation_date, active, discord_id, guild_id 
            FROM activation_keys 
            WHERE user_id = ? AND active = 1
            ORDER BY creation_date DESC
            LIMIT 1
        ''', (user_id_str,))
        result = c.fetchone()
        conn.close()
        
        if result:
            log_message(f"✅ get_user_active_key: Found active key for user {user_id_str}: {result[0]}")
            return {
                'key': result[0],
                'user_id': result[1],
                'username': result[2],
                'discriminator': result[3],
                'creation_date': result[4],
                'active': bool(result[5]),
                'discord_id': result[6],
                'guild_id': result[7]
            }
        else:
            log_message(f"❌ get_user_active_key: No active key found for user {user_id_str}")
            return None

def create_key(key_data):
    """Create a new key and deactivate any existing ones for the user - ATOMIC OPERATION"""
    user_id = key_data['user_id']
    
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Debug before operation
        c.execute('SELECT key, active FROM activation_keys WHERE user_id = ?', (user_id,))
        before_keys = c.fetchall()
        log_message(f"🔍 CREATE_KEY: Before operation - User {user_id} has {len(before_keys)} keys: {before_keys}")
        
        # ATOMIC: Deactivate ALL existing keys for this user and create new one
        c.execute('''
            UPDATE activation_keys 
            SET active = 0, 
                deactivation_date = ?,
                deactivation_reason = ?
            WHERE user_id = ?
        ''', (str(datetime.now()), "Replaced by new key", user_id))
        
        # Insert new key
        c.execute('''
            INSERT OR REPLACE INTO activation_keys 
            (key, user_id, username, discriminator, creation_date, active, discord_id, guild_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            key_data['key'],
            user_id,
            key_data['username'],
            key_data['discriminator'],
            key_data['creation_date'],
            1,  # active
            key_data['discord_id'],
            key_data['guild_id']
        ))
        
        conn.commit()
        
        # Debug after operation
        c.execute('SELECT key, active FROM activation_keys WHERE user_id = ?', (user_id,))
        after_keys = c.fetchall()
        log_message(f"🔍 CREATE_KEY: After operation - User {user_id} has {len(after_keys)} keys: {after_keys}")
        
        conn.close()
        
        log_message(f"✅ CREATE_KEY: Key created for user {user_id}: {key_data['key']}")
        return True

def generate_key(user_id):
    timestamp = str(datetime.now().timestamp())
    raw_key = f"{user_id}{timestamp}"
    # Generate a SHA256 hash and take first 16 characters, convert to uppercase
    return hashlib.sha256(raw_key.encode()).hexdigest()[:16].upper()

async def has_subscriber_role(user_id, guild):
    try:
        member = await guild.fetch_member(user_id)
        return any(role.id == ROLE_ID for role in member.roles)
    except:
        return False

@tasks.loop(seconds=14400)  # Runs every 4 hours
async def check_subscriber_roles():
    """Check if users still have the subscriber role and deactivate keys if not"""
    log_message("🔍 Checking subscriber roles...")
    
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT key, user_id, guild_id FROM activation_keys WHERE active = 1')
        active_keys = c.fetchall()
        conn.close()
    
    deactivated_count = 0
    
    for key, user_id, guild_id in active_keys:
        try:
            guild = bot.get_guild(int(guild_id))
            if not guild:
                continue
                
            has_role = await has_subscriber_role(int(user_id), guild)
            if not has_role:
                # Deactivate the key
                with db_lock:
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute('''
                        UPDATE activation_keys 
                        SET active = 0, 
                            deactivation_date = ?,
                            deactivation_reason = ?
                        WHERE key = ?
                    ''', (str(datetime.now()), "Lost subscriber role", key))
                    conn.commit()
                    conn.close()
                
                deactivated_count += 1
                log_message(f"❌ Deactivated key for user {user_id}")
        except Exception as e:
            log_message(f"Error checking role for user {user_id}: {e}")
    
    if deactivated_count > 0:
        log_message(f"✅ Deactivated {deactivated_count} keys due to lost subscriber roles")
    else:
        log_message("✅ All keys are valid")

# ... (rest of your existing functions like on_member_remove, key_status, etc.)

class KeyButtons(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔑 Get/View My Key", style=discord.ButtonStyle.primary, custom_id="manage_key")
    async def manage_key(self, interaction: discord.Interaction, button: Button):
        user_id = str(interaction.user.id)
        
        log_message(f"🎯 MANAGE_KEY: Button clicked by user: {user_id} ({interaction.user})")
        
        # Debug: Show all keys for this user before any operation
        debug_user_keys(user_id)
        
        if not await has_subscriber_role(interaction.user.id, interaction.guild):
            await interaction.response.send_message("❌ You need the 'subscriber' role!", ephemeral=True)
            return

        # Get current active key using SINGLE SOURCE OF TRUTH function
        existing_key = get_user_active_key(user_id)
        
        if existing_key:
            # User already has an active key - just show it
            log_message(f"✅ MANAGE_KEY: Showing existing key for user {user_id}: {existing_key['key']}")
            await interaction.response.send_message(
                f"🔑 **Your Activation Key:**\n"
                f"`{existing_key['key']}`\n\n"
                f"**Creation date:** {existing_key.get('creation_date', 'unknown')}\n"
                f"**Status:** ✅ Active\n\n"
                f"Use this key in the Gold Menu to activate the software.",
                ephemeral=True
            )
        else:
            # User needs a new key - check rate limit
            if key_limiter.is_limited(interaction.user.id):
                await interaction.response.send_message(
                    f"❌ You can only generate a new key once every 6 hours. Please wait before trying again.", 
                    ephemeral=True
                )
                return
            
            log_message(f"🆕 MANAGE_KEY: Generating new key for user: {user_id}")
            new_key = generate_key(user_id)
            
            key_data = {
                'key': new_key,
                'user_id': user_id,
                'username': str(interaction.user),
                'discriminator': interaction.user.discriminator,
                'creation_date': str(datetime.now()),
                'discord_id': user_id,
                'guild_id': str(interaction.guild.id)
            }

            # Save to database
            if create_key(key_data):
                # Update rate limiter
                key_limiter.allowances[interaction.user.id].append(time.time())
                
                # Verify the key was saved correctly
                verified_key = get_user_active_key(user_id)
                if verified_key and verified_key['key'] == new_key:
                    log_message(f"✅ MANAGE_KEY: Key creation verified: {new_key}")
                else:
                    log_message(f"❌ MANAGE_KEY: Key creation verification FAILED!")
                
                # Debug: Show all keys for this user after operation
                debug_user_keys(user_id)
                
                await interaction.response.send_message(
                    f"🔑 **Your New Activation Key:**\n"
                    f"`{new_key}`\n\n"
                    f"**Creation date:** {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
                    f"**Status:** ✅ Active\n\n"
                    f"Use this key in the Gold Menu to activate the software.\n\n"
                    f"**Important:** Save this key somewhere safe!",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Error saving your key. Please try again or contact support.",
                    ephemeral=True
                )

@bot.event
async def on_ready():
    print(f'✅ Bot {bot.user} is online!')
    init_db()  # Initialize database with debug info
    bot.add_view(KeyButtons())
    check_subscriber_roles.start()
    
    # Wait for the bot to be fully ready
    await bot.wait_until_ready()
    
    try:
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(CHANNEL_ID)
        
        # Check if a message from the bot already exists
        existing_message = None
        async for message in channel.history(limit=20):
            if message.author == bot.user and message.components:
                existing_message = message
                break
        
        view = KeyButtons()
        
        if existing_message:
            await existing_message.edit(
                content=(
                    "🔑 **Activation Key Manager**\n\n"
                    "Click the button below to get or view your activation key.\n\n"
                    "**Rules:**\n"
                    "• One key per user\n"
                    "• Subscriber role required\n"
                    "• Key generation limited to once per 6 hours\n\n"
                    "*Your key will be shown here*"
                ),
                view=view
            )
            print("✅ Menu updated successfully.")
        else:
            await channel.send(
                "🔑 **Activation Key Manager**\n\n"
                "Click the button below to get or view your activation key.\n\n"
                "**Rules:**\n"
                "• One key per user\n"
                "• Subscriber role required\n"
                "• Key generation limited to once per 6 hours\n\n"
                "*Your key will be shown here*",
                view=view
            )
            print("✅ Menu sent successfully.")
    except Exception as e:
        print(f"❌ Failed to send/update menu: {e}")

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    print(f"Flask server starting on port {PORT}")
    
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
