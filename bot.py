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
# Create a lock for user-specific operations to prevent race conditions
user_locks = defaultdict(Lock)

# Rate limiting for button interactions
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

# Create rate limiters with your requested settings
get_key_limiter = RateLimiter(1, 21600)  # 1 click per 6 hours (21600 seconds)
view_key_limiter = RateLimiter(2, 3600)   # 2 clicks per hour (3600 seconds)

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
        c.execute('SELECT active FROM activation_keys WHERE key = ?', (key,))
        result = c.fetchone()
        conn.close()
        
        if result and result[0] == 1:
            log_message(f"Key found and active: {key}")
            response = jsonify({"valid": True})
            response.headers.add('Access-Control-Allow-Origin', '*')
            return response
        else:
            log_message(f"Key not found or inactive: {key}")
            response = jsonify({"valid": False, "error": "Invalid or inactive key"})
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
        conn.close()
        log_message("✅ Database initialized")

def get_user_active_key(user_id):
    """Get active key for a user"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            SELECT key, user_id, username, discriminator, creation_date, active, discord_id, guild_id 
            FROM activation_keys 
            WHERE user_id = ? AND active = 1
        ''', (str(user_id),))
        result = c.fetchone()
        conn.close()
        
        if result:
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
        return None

def create_key(key_data):
    """Create a new key and deactivate any existing ones for the user"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Deactivate any existing active keys for this user
        c.execute('''
            UPDATE activation_keys 
            SET active = 0, 
                deactivation_date = ?,
                deactivation_reason = ?
            WHERE user_id = ? AND active = 1
        ''', (str(datetime.now()), "Replaced by new key", key_data['user_id']))
        
        # Insert new key
        c.execute('''
            INSERT INTO activation_keys 
            (key, user_id, username, discriminator, creation_date, active, discord_id, guild_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            key_data['key'],
            key_data['user_id'],
            key_data['username'],
            key_data['discriminator'],
            key_data['creation_date'],
            1,  # active
            key_data['discord_id'],
            key_data['guild_id']
        ))
        
        conn.commit()
        conn.close()
        log_message(f"✅ Key created for user {key_data['user_id']}: {key_data['key']}")
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
                
                # Send log to logging channel
                try:
                    log_channel = bot.get_channel(LOG_CHANNEL_ID)
                    if log_channel:
                        embed = discord.Embed(
                            title="🔑 Key Deactivated",
                            description="User lost subscriber role",
                            color=0xff0000,
                            timestamp=datetime.now()
                        )
                        embed.add_field(name="User", value=f"<@{user_id}>", inline=True)
                        embed.add_field(name="Key", value=f"`{key}`", inline=True)
                        embed.add_field(name="Reason", value="Lost subscriber role", inline=False)
                        embed.set_footer(text="Automatic deactivation")
                        await log_channel.send(embed=embed)
                except Exception as e:
                    log_message(f"Could not send log to channel: {e}")
                    
        except Exception as e:
            log_message(f"Error checking role for user {user_id}: {e}")
    
    if deactivated_count > 0:
        log_message(f"✅ Deactivated {deactivated_count} keys due to lost subscriber roles")
    else:
        log_message("✅ All keys are valid")

@bot.event
async def on_member_remove(member):
    """Automatically deactivate keys when a member leaves the server"""
    try:
        user_id = str(member.id)
        
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM activation_keys WHERE user_id = ? AND active = 1', (user_id,))
            active_count = c.fetchone()[0]
            
            if active_count > 0:
                c.execute('''
                    UPDATE activation_keys 
                    SET active = 0, 
                        deactivation_date = ?,
                        deactivation_reason = ?
                    WHERE user_id = ? AND active = 1
                ''', (str(datetime.now()), "User left the server", user_id))
                conn.commit()
                
                log_message(f"Deactivated {active_count} keys for user {member.name} (ID: {user_id}) who left the server")
                
                # Send log to logging channel
                try:
                    log_channel = bot.get_channel(LOG_CHANNEL_ID)
                    if log_channel:
                        embed = discord.Embed(
                            title="🔑 Key Deactivated",
                            description="User left the server",
                            color=0xff0000,
                            timestamp=datetime.now()
                        )
                        embed.add_field(name="User", value=f"{member.name}#{member.discriminator}", inline=True)
                        embed.add_field(name="Keys Deactivated", value=str(active_count), inline=True)
                        embed.add_field(name="Reason", value="User left the server", inline=False)
                        await log_channel.send(embed=embed)
                except Exception as e:
                    log_message(f"Could not send log to channel: {e}")
            else:
                log_message(f"User {member.name} (ID: {user_id}) left but had no active keys")
                
            conn.close()
            
    except Exception as e:
        log_message(f"Error handling member leave: {e}")

# Admin command to view key status
@bot.command()
@commands.has_permissions(administrator=True)
@commands.cooldown(1, 5, commands.BucketType.user)
async def key_status(ctx):
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM activation_keys WHERE active = 1')
        active = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM activation_keys WHERE active = 0')
        inactive = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM activation_keys')
        total = c.fetchone()[0]
        conn.close()
    
    embed = discord.Embed(title="🔑 Key Status", color=0x00ff00)
    embed.add_field(name="Active Keys", value=str(active), inline=True)
    embed.add_field(name="Inactive Keys", value=str(inactive), inline=True)
    embed.add_field(name="Total Keys", value=str(total), inline=True)
    
    await ctx.send(embed=embed)

# Handle rate limit errors
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"❌ This command is on cooldown. Try again in {error.retry_after:.2f}s.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    else:
        # You might want to log other errors
        log_message(f"Command error: {error}")

class KeyButtons(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔄 Get My Key", style=discord.ButtonStyle.primary, custom_id="get_key")
    async def get_key(self, interaction: discord.Interaction, button: Button):
        # Use user-specific lock to prevent race conditions
        user_id = str(interaction.user.id)
        with user_locks[user_id]:
            # Rate limiting (1 click per 6 hours)
            if get_key_limiter.is_limited(interaction.user.id):
                await interaction.response.send_message(
                    f"❌ You can only request a new key once every 6 hours. Please wait before requesting another key.", 
                    ephemeral=True
                )
                return
                
            if not await has_subscriber_role(interaction.user.id, interaction.guild):
                await interaction.response.send_message("❌ You need the 'subscriber' role!", ephemeral=True)
                return

            # Check for existing active key
            existing_key = get_user_active_key(user_id)
            if existing_key:
                await interaction.response.send_message(
                    f"🔑 You already have an active key: `{existing_key['key']}`\n\n"
                    f"Use this key in the Gold Menu to activate the software.\n"
                    f"Creation date: {existing_key.get('creation_date', 'unknown')}\n\n"
                    f"**Note:** Each user can only have one active key at a time.",
                    ephemeral=True
                )
                return

            # Generate new key
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
                # Update rate limiter after successful key generation
                get_key_limiter.allowances[interaction.user.id].append(time.time())
                
                # Send log to logging channel
                try:
                    log_channel = bot.get_channel(LOG_CHANNEL_ID)
                    if log_channel:
                        embed = discord.Embed(
                            title="🔑 Key Generated",
                            description="New key generated for user",
                            color=0x00ff00,
                            timestamp=datetime.now()
                        )
                        embed.add_field(name="User", value=interaction.user.mention, inline=True)
                        embed.add_field(name="Key", value=f"`{new_key}`", inline=True)
                        embed.add_field(name="Note", value="One key per user enforced", inline=False)
                        await log_channel.send(embed=embed)
                except Exception as e:
                    log_message(f"Could not send log to channel: {e}")
                
                # Send the key to the user
                await interaction.response.send_message(
                    f"🔑 **Your Activation Key:**\n"
                    f"`{new_key}`\n\n"
                    f"Use this key in the Gold Menu to activate the software.\n"
                    f"Creation date: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                    f"**Important:** Save this key somewhere safe!",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Error saving your key. Please try again or contact support.",
                    ephemeral=True
                )

    @discord.ui.button(label="👀 View My Key", style=discord.ButtonStyle.secondary, custom_id="view_key")
    async def view_key(self, interaction: discord.Interaction, button: Button):
        # Use user-specific lock to prevent race conditions
        user_id = str(interaction.user.id)
        with user_locks[user_id]:
            # Rate limiting (2 clicks per hour)
            if view_key_limiter.is_limited(interaction.user.id):
                await interaction.response.send_message(
                    f"❌ You can only view your key twice per hour. Please wait before trying again.", 
                    ephemeral=True
                )
                return
                
            existing_key = get_user_active_key(user_id)
            if existing_key:
                # Update rate limiter after successful view
                view_key_limiter.allowances[interaction.user.id].append(time.time())
                await interaction.response.send_message(
                    f"🔑 **Your Activation Key:**\n"
                    f"`{existing_key['key']}`\n\n"
                    f"Creation date: {existing_key.get('creation_date', 'unknown')}\n"
                    f"Status: ✅ Active\n\n"
                    f"Use this key in the Gold Menu to activate the software.",
                    ephemeral=True
                )
                return
            
            await interaction.response.send_message(
                "❌ You don't have an active key. Use the 'Get My Key' button to generate one.\n\n"
                "**Note:** Each user is limited to one key only.", 
                ephemeral=True
            )

@bot.event
async def on_ready():
    print(f'✅ Bot {bot.user} is online!')
    init_db()  # Initialize database
    bot.add_view(KeyButtons())
    check_subscriber_roles.start()
    
    # Wait for the bot to be fully ready
    await bot.wait_until_ready()
    
    try:
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            # Try fetching if not in cache
            channel = await bot.fetch_channel(CHANNEL_ID)
        
        # Check if a message from the bot already exists
        existing_message = None
        async for message in channel.history(limit=20):
            if message.author == bot.user and message.components:
                existing_message = message
                break
        
        view = KeyButtons()
        
        if existing_message:
            # Edit the existing message
            await existing_message.edit(
                content=(
                    "🔑 **Activation Key Manager**\n\n"
                    "Click below to manage your key:\n"
                    "• **🔄 Get My Key**: Generate a new key (once every 6 hours)\n"
                    "• **👀 View My Key**: Show your current key (twice per hour)\n\n"
                    "**Important Rules:**\n"
                    "• Each user can have only **ONE active key** at a time\n"
                    "• Keys are tied to your Discord account\n"
                    "• Keep your key secure!\n\n"
                    "*Your key will be shown directly in this message*"
                ),
                view=view
            )
            print("✅ Menu updated successfully.")
        else:
            # Send a new message
            await channel.send(
                "🔑 **Activation Key Manager**\n\n"
                "Click below to manage your key:\n"
                "• **🔄 Get My Key**: Generate a new key (once every 6 hours)\n"
                "• **👀 View My Key**: Show your current key (twice per hour)\n\n"
                "**Important Rules:**\n"
                "• Each user can have only **ONE active key** at a time\n"
                "• Keys are tied to your Discord account\n"
                "• Keep your key secure!\n\n"
                "*Your key will be shown directly in this message*",
                view=view
            )
            print("✅ Menu sent successfully.")
    except Exception as e:
        print(f"❌ Failed to send/update menu: {e}")
        print("Check if:")
        print("1. The bot has access to the channel")
        print("2. The CHANNEL_ID is correct")
        print("3. The bot has the necessary permissions (Send Messages, View Channel, etc.)")

def run_flask():
    """Run Flask with the correct port for Render"""
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# This will run when executed directly (for local testing)
if __name__ == "__main__":
    # Start Flask in a separate thread
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    print(f"Flask server starting on port {PORT}")
    
    # Start the Discord bot in the main thread
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
