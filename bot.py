import os
import discord
import asyncio
import hashlib
import time
import sqlite3
from discord.ext import commands, tasks
from discord.ui import Button, View
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Thread
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    print("❌ ERROR: BOT_TOKEN not found!")
    exit(1)

ROLE_ID = int(os.getenv('ROLE_ID', 1281782820074688542))
CHANNEL_ID = int(os.getenv('CHANNEL_ID', 1411206861499400192))
LOG_CHANNEL_ID = 1411710161868820671
DB_FILE = "keys.db"
PORT = int(os.environ.get('PORT', 10000))

# Simple rate limiting
user_last_keygen = {}

# Flask app
app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return "✅ Bot is alive!"

@app.route('/verify_key', methods=['POST', 'OPTIONS'])
def verify_key():
    try:
        if request.method == 'OPTIONS':
            response = jsonify({"status": "ok"})
            response.headers.add('Access-Control-Allow-Origin', '*')
            response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
            response.headers.add('Access-Control-Allow-Methods', 'POST')
            return response
            
        data = request.get_json()
        if not data or 'key' not in data:
            return jsonify({"valid": False, "error": "No key provided"})
        
        key = data['key'].strip().upper()
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM keys WHERE key = ? AND active = 1', (key,))
        result = c.fetchone()
        conn.close()
        
        if result:
            print(f"✅ Key valid: {key}")
            response = jsonify({"valid": True})
            response.headers.add('Access-Control-Allow-Origin', '*')
            return response
        else:
            print(f"❌ Key invalid: {key}")
            response = jsonify({"valid": False, "error": "Invalid key"})
            response.headers.add('Access-Control-Allow-Origin', '*')
            return response
            
    except Exception as e:
        print(f"Error verifying key: {e}")
        response = jsonify({"valid": False, "error": str(e)})
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response

# Discord bot
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

def init_db():
    """Initialize the database with a simple schema"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            guild_id TEXT NOT NULL
        )
    ''')
    
    # Create index for faster lookups
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON keys(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_active ON keys(active)')
    
    conn.commit()
    conn.close()
    print("✅ Database initialized")

def generate_key(user_id):
    """Generate a unique key"""
    timestamp = str(time.time())
    unique_string = f"{user_id}{timestamp}{os.urandom(16).hex()}"
    return hashlib.sha256(unique_string.encode()).hexdigest()[:16].upper()

def get_user_key(user_id):
    """Get user's active key - SIMPLE AND RELIABLE"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT key, created_at FROM keys 
        WHERE user_id = ? AND active = 1 
        ORDER BY created_at DESC 
        LIMIT 1
    ''', (str(user_id),))
    result = c.fetchone()
    conn.close()
    
    if result:
        return {'key': result[0], 'created_at': result[1]}
    return None

def create_user_key(user_id, username, guild_id):
    """Create a new key for user - ATOMIC AND SAFE"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Start transaction
    c.execute('BEGIN TRANSACTION')
    
    try:
        # Deactivate any existing keys
        c.execute('UPDATE keys SET active = 0 WHERE user_id = ? AND active = 1', (str(user_id),))
        
        # Generate and insert new key
        new_key = generate_key(user_id)
        c.execute('''
            INSERT INTO keys (key, user_id, username, created_at, active, guild_id)
            VALUES (?, ?, ?, ?, 1, ?)
        ''', (new_key, str(user_id), username, datetime.now().isoformat(), str(guild_id)))
        
        # Commit transaction
        conn.commit()
        conn.close()
        
        print(f"✅ Created key for {username} ({user_id}): {new_key}")
        return new_key
        
    except Exception as e:
        # Rollback on error
        conn.rollback()
        conn.close()
        print(f"❌ Error creating key: {e}")
        return None

async def has_subscriber_role(user_id, guild):
    try:
        member = await guild.fetch_member(user_id)
        return any(role.id == ROLE_ID for role in member.roles)
    except:
        return False

@tasks.loop(hours=4)
async def check_subscriber_roles():
    """Check if users still have subscriber role"""
    print("🔍 Checking subscriber roles...")
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT key, user_id, guild_id FROM keys WHERE active = 1')
    active_keys = c.fetchall()
    conn.close()
    
    for key, user_id, guild_id in active_keys:
        try:
            guild = bot.get_guild(int(guild_id))
            if guild:
                has_role = await has_subscriber_role(int(user_id), guild)
                if not has_role:
                    # Deactivate key
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute('UPDATE keys SET active = 0 WHERE key = ?', (key,))
                    conn.commit()
                    conn.close()
                    print(f"❌ Deactivated key for user {user_id} (lost role)")
        except Exception as e:
            print(f"Error checking role for {user_id}: {e}")

@bot.event
async def on_member_remove(member):
    """Deactivate keys when member leaves"""
    user_id = str(member.id)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE keys SET active = 0 WHERE user_id = ? AND active = 1', (user_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    
    if affected > 0:
        print(f"🔒 Deactivated {affected} keys for user {user_id} who left")

class KeyManager(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔑 Get My Key", style=discord.ButtonStyle.primary, custom_id="get_key")
    async def get_key(self, interaction: discord.Interaction, button: Button):
        user_id = str(interaction.user.id)
        username = str(interaction.user)
        guild_id = str(interaction.guild.id)
        
        print(f"🎯 Button clicked by {username} ({user_id})")
        
        # Check role
        if not await has_subscriber_role(interaction.user.id, interaction.guild):
            await interaction.response.send_message("❌ You need the subscriber role!", ephemeral=True)
            return
        
        # Check rate limit (6 hours)
        current_time = time.time()
        if user_id in user_last_keygen:
            time_since_last = current_time - user_last_keygen[user_id]
            if time_since_last < 21600:  # 6 hours
                hours_left = (21600 - time_since_last) / 3600
                await interaction.response.send_message(
                    f"❌ You can generate a new key in {hours_left:.1f} hours.", 
                    ephemeral=True
                )
                return
        
        # Get existing key or create new one
        existing_key = get_user_key(user_id)
        
        if existing_key:
            # User already has a key
            user_last_keygen[user_id] = current_time  # Update rate limit even for viewing
            await interaction.response.send_message(
                f"🔑 **Your Existing Key:**\n"
                f"`{existing_key['key']}`\n\n"
                f"**Created:** {existing_key['created_at'][:16]}\n"
                f"**Status:** ✅ Active\n\n"
                f"Use this key in your software.",
                ephemeral=True
            )
            print(f"✅ Showed existing key to {username}")
        else:
            # Create new key
            new_key = create_user_key(user_id, username, guild_id)
            
            if new_key:
                user_last_keygen[user_id] = current_time
                await interaction.response.send_message(
                    f"🔑 **Your New Key:**\n"
                    f"`{new_key}`\n\n"
                    f"**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"**Status:** ✅ Active\n\n"
                    f"Use this key in your software. Save it safely!",
                    ephemeral=True
                )
                print(f"✅ Created new key for {username}: {new_key}")
                
                # Log to log channel
                try:
                    log_channel = bot.get_channel(LOG_CHANNEL_ID)
                    if log_channel:
                        embed = discord.Embed(
                            title="🔑 Key Generated",
                            color=0x00ff00,
                            timestamp=datetime.now()
                        )
                        embed.add_field(name="User", value=interaction.user.mention, inline=True)
                        embed.add_field(name="Key", value=f"`{new_key}`", inline=True)
                        await log_channel.send(embed=embed)
                except Exception as e:
                    print(f"Could not log to channel: {e}")
            else:
                await interaction.response.send_message(
                    "❌ Error creating key. Please try again.",
                    ephemeral=True
                )

@bot.event
async def on_ready():
    print(f'✅ {bot.user} is online!')
    init_db()
    bot.add_view(KeyManager())
    check_subscriber_roles.start()
    
    # Send key manager message
    try:
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            # Delete old bot messages to avoid duplicates
            async for message in channel.history(limit=10):
                if message.author == bot.user and message.components:
                    await message.delete()
                    break  # Only delete one message
            
            # Send new message
            await channel.send(
                "🔑 **Activation Key Manager**\n\n"
                "Click the button below to get your activation key:\n"
                "• **One key per user** - No duplicates\n"  
                "• **Subscriber role required**\n"
                "• **6-hour cooldown** for new keys\n\n"
                "Your key will be shown here securely.",
                view=KeyManager()
            )
            print("✅ Key manager message sent")
    except Exception as e:
        print(f"❌ Error sending message: {e}")

# Admin commands
@bot.command()
@commands.has_permissions(administrator=True)
async def keycount(ctx):
    """Check how many active keys exist"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT COUNT(*) FROM keys WHERE active = 1')
    active_keys = c.fetchone()[0]
    
    c.execute('SELECT COUNT(*) FROM keys')
    total_keys = c.fetchone()[0]
    
    conn.close()
    
    embed = discord.Embed(title="🔑 Key Statistics", color=0x00ff00)
    embed.add_field(name="Active Keys", value=active_keys, inline=True)
    embed.add_field(name="Total Keys", value=total_keys, inline=True)
    embed.add_field(name="Inactive Keys", value=total_keys - active_keys, inline=True)
    
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def keyinfo(ctx, user: discord.Member = None):
    """Get key information for a user"""
    if not user:
        user = ctx.author
    
    user_key = get_user_key(str(user.id))
    
    if user_key:
        embed = discord.Embed(title=f"🔑 Key Info for {user}", color=0x00ff00)
        embed.add_field(name="Key", value=f"`{user_key['key']}`", inline=False)
        embed.add_field(name="Created", value=user_key['created_at'][:16], inline=True)
        embed.add_field(name="Status", value="✅ Active", inline=True)
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"❌ {user.mention} doesn't have an active key.")

@bot.command()
@commands.has_permissions(administrator=True)
async def deactivatekey(ctx, user: discord.Member):
    """Deactivate a user's key"""
    user_id = str(user.id)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE keys SET active = 0 WHERE user_id = ? AND active = 1', (user_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    
    if affected > 0:
        await ctx.send(f"✅ Deactivated key for {user.mention}")
    else:
        await ctx.send(f"❌ {user.mention} doesn't have an active key.")

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ User not found.")
    else:
        print(f"Command error: {error}")

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    # Start Flask
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    print(f"🚀 Starting bot on port {PORT}...")
    
    # Start bot
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
