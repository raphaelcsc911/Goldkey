import os
import discord
import asyncio
import json
import hashlib
import time
from discord.ext import commands, tasks
from discord.ui import Button, View
from datetime import datetime
from flask import Flask, request, jsonify
from threading import Thread
from dotenv import load_dotenv
from collections import defaultdict

# Load environment variables
load_dotenv()

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
KEYS_FILE = "activation_keys.json"

# Get port from Render environment variable or use default
PORT = int(os.environ.get('PORT', 10000))
print(f"Using port: {PORT}")

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

# Create rate limiters
button_limiter = RateLimiter(3, 60)  # 3 clicks per minute per user

# Flask app for keeping the bot alive and handling verification
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Bot is alive and running!"

# Add logging function to bot
def log_message(message):
    log_file = "bot_debug.log"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except:
        print(f"[{timestamp}] {message}")

@app.route('/verify_key', methods=['POST'])
def verify_key():
    try:
        data = request.get_json()
        if not data or 'key' not in data:
            log_message("No key provided in request")
            return jsonify({"valid": False})
        
        key = data['key'].strip().upper()
        keys = load_keys()
        
        log_message(f"Verifying key: {key}")
        
        # Check if key exists and is active
        if key in keys and isinstance(keys[key], dict) and keys[key].get('active', False):
            log_message(f"Key found and active: {key}")
            
            # Additional check: verify user still has subscriber role
            user_id = keys[key].get('user_id')
            guild_id = keys[key].get('guild_id')
            
            if guild_id and user_id:
                # Try to check if user still has the role
                try:
                    guild = bot.get_guild(int(guild_id))
                    if guild:
                        member = guild.get_member(int(user_id))
                        if member:
                            has_role = any(role.id == ROLE_ID for role in member.roles)
                            if not has_role:
                                # User lost role, deactivate key
                                keys[key]['active'] = False
                                keys[key]['deactivation_date'] = str(datetime.now())
                                keys[key]['deactivation_reason'] = "Lost subscriber role (verified)"
                                save_keys(keys)
                                log_message(f"Key deactivated due to lost role: {key}")
                                return jsonify({"valid": False})
                except Exception as e:
                    log_message(f"Error checking role: {e}")
                    # If we can't check the role, assume it's still valid
                    pass
            
            log_message(f"Key validation successful: {key}")
            return jsonify({"valid": True})
        else:
            log_message(f"Key not found or inactive: {key}")
            return jsonify({"valid": False})
            
    except Exception as e:
        log_message(f"Error verifying key: {e}")
        return jsonify({"valid": False})

# Set up intents
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

def safe_load_keys():
    """Safely load keys with error handling"""
    try:
        with open(KEYS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        # Ensure all values are dictionaries
        cleaned_data = {}
        for key, value in data.items():
            if isinstance(value, dict):
                cleaned_data[key] = value
            else:
                # Convert invalid entries to valid ones
                cleaned_data[key] = {
                    'user_id': str(value) if isinstance(value, int) else "unknown",
                    'username': "unknown",
                    'discriminator': "0000",
                    'creation_date': str(datetime.now()),
                    'active': False,
                    'discord_id': "unknown",
                    'guild_id': "unknown"
                }
                
        return cleaned_data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def safe_save_keys(keys):
    """Safely save keys with error handling"""
    try:
        with open(KEYS_FILE, 'w', encoding='utf-8') as f:
            json.dump(keys, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving keys: {e}")
        return False

def load_keys():
    return safe_load_keys()

def save_keys(keys):
    return safe_save_keys(keys)

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

@tasks.loop(seconds=21600)  # Runs every 6 hours
async def check_subscriber_roles():
    """Check if users still have the subscriber role and deactivate keys if not"""
    print("🔍 Checking subscriber roles...")
    keys = load_keys()
    deactivated_count = 0
    
    for key, info in list(keys.items()):
        if not isinstance(info, dict):
            continue
            
        if 'active' not in info or 'user_id' not in info:
            continue
            
        if info['active']:
            try:
                user_id = int(info['user_id'])
                guild_id = info.get('guild_id')
                
                if not guild_id:
                    continue
                    
                guild = bot.get_guild(int(guild_id))
                if not guild:
                    continue
                    
                has_role = await has_subscriber_role(user_id, guild)
                if not has_role:
                    # Deactivate the key
                    keys[key]['active'] = False
                    keys[key]['deactivation_date'] = str(datetime.now())
                    keys[key]['deactivation_reason'] = "Lost subscriber role"
                    deactivated_count += 1
                    print(f"❌ Deactivated key for user {info.get('username', 'unknown')}")
                    
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
            except (ValueError, TypeError) as e:
                # Invalid user_id format
                keys[key]['active'] = False
                keys[key]['deactivation_date'] = str(datetime.now())
                keys[key]['deactivation_reason'] = f"Invalid user ID format: {e}"
                print(f"❌ Deactivated key due to invalid user ID: {key}")
    
    if deactivated_count > 0:
        save_keys(keys)
        print(f"✅ Deactivated {deactivated_count} keys due to lost subscriber roles")
    else:
        print("✅ All keys are valid")

@bot.event
async def on_member_remove(member):
    """Automatically deactivate keys when a member leaves the server"""
    try:
        user_id = str(member.id)
        keys = load_keys()
        deactivated_count = 0
        
        log_message(f"Member left: {member.name} (ID: {user_id})")
        
        for key, info in keys.items():
            if not isinstance(info, dict):
                continue
                
            if info.get('user_id') == user_id and info.get('active', False):
                info['active'] = False
                info['deactivation_date'] = str(datetime.now())
                info['deactivation_reason'] = "User left the server"
                deactivated_count += 1
                log_message(f"Deactivated key: {key} for user {member.name}")
        
        if deactivated_count > 0:
            save_keys(keys)
            log_message(f"Deactivated {deactivated_count} keys for user {member.name} (ID: {user_id}) who left the server")
            
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
                    embed.add_field(name="Keys Deactivated", value=str(deactivated_count), inline=True)
                    embed.add_field(name="Reason", value="User left the server", inline=False)
                    await log_channel.send(embed=embed)
            except Exception as e:
                log_message(f"Could not send log to channel: {e}")
        else:
            log_message(f"User {member.name} (ID: {user_id}) left but had no active keys")
            
    except Exception as e:
        log_message(f"Error handling member leave: {e}")

# Admin command to revoke a user's key
@bot.command()
@commands.has_permissions(administrator=True)
@commands.cooldown(1, 10, commands.BucketType.user)
async def revoke_key(ctx, user: discord.Member):
    keys = load_keys()
    revoked = 0
    
    for key, info in keys.items():
        if not isinstance(info, dict):
            continue
        if info.get('user_id') == str(user.id) and info.get('active', False):
            info['active'] = False
            info['revocation_date'] = str(datetime.now())
            info['revocation_reason'] = "Manually revoked by admin"
            revoked += 1
    
    if revoked > 0:
        save_keys(keys)
        
        # Send log to logging channel
        try:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                embed = discord.Embed(
                    title="🔑 Key Revoked",
                    description="Admin manually revoked keys",
                    color=0xff0000,
                    timestamp=datetime.now()
                )
                embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
                embed.add_field(name="User", value=user.mention, inline=True)
                embed.add_field(name="Keys Revoked", value=str(revoked), inline=True)
                embed.add_field(name="Reason", value="Manual revocation by admin", inline=False)
                await log_channel.send(embed=embed)
        except Exception as e:
            log_message(f"Could not send log to channel: {e}")
            
        await ctx.send(f"✅ Revoked {revoked} keys from {user.mention}")
    else:
        await ctx.send(f"❌ {user.mention} doesn't have any active keys.")

# Admin command to view key status
@bot.command()
@commands.has_permissions(administrator=True)
@commands.cooldown(1, 5, commands.BucketType.user)
async def key_status(ctx):
    keys = load_keys()
    active = 0
    inactive = 0
    
    for key, info in keys.items():
        if not isinstance(info, dict):
            continue
        if info.get('active', False):
            active += 1
        else:
            inactive += 1
    
    embed = discord.Embed(title="🔑 Key Status", color=0x00ff00)
    embed.add_field(name="Active Keys", value=str(active), inline=True)
    embed.add_field(name="Inactive Keys", value=str(inactive), inline=True)
    embed.add_field(name="Total Keys", value=str(len(keys)), inline=True)
    
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
        # Rate limiting
        if button_limiter.is_limited(interaction.user.id):
            await interaction.response.send_message("❌ Please wait before requesting another key.", ephemeral=True)
            return
            
        if not await has_subscriber_role(interaction.user.id, interaction.guild):
            await interaction.response.send_message("❌ You need the 'subscriber' role!", ephemeral=True)
            return

        keys = load_keys()
        user_id = str(interaction.user.id)

        # Check for existing active key
        for key, info in keys.items():
            if not isinstance(info, dict):
                continue
            if info.get('user_id') == user_id and info.get('active', False):
                await interaction.response.send_message(
                    f"🔑 You already have an active key: `{key}`\n\n"
                    f"Use this key in the Gold Menu to activate the software.\n"
                    f"Creation date: {info.get('creation_date', 'unknown')}",
                    ephemeral=True
                )
                return

        # Generate new key
        new_key = generate_key(user_id)
        keys[new_key] = {
            'user_id': user_id,
            'username': str(interaction.user),
            'discriminator': interaction.user.discriminator,
            'creation_date': str(datetime.now()),
            'active': True,
            'discord_id': str(interaction.user.id),
            'guild_id': str(interaction.guild.id)
        }

        save_keys(keys)
        
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
                await log_channel.send(embed=embed)
        except Exception as e:
            log_message(f"Could not send log to channel: {e}")
        
        # Send the key directly in the ephemeral response
        await interaction.response.send_message(
            f"🔑 **Your Activation Key:**\n"
            f"`{new_key}`\n\n"
            f"Use this key in the Gold Menu to activate the software.\n"
            f"Creation date: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
            f"**Important:** Save this key somewhere safe!",
            ephemeral=True
        )

    @discord.ui.button(label="👀 View My Key", style=discord.ButtonStyle.secondary, custom_id="view_key")
    async def view_key(self, interaction: discord.Interaction, button: Button):
        # Rate limiting
        if button_limiter.is_limited(interaction.user.id):
            await interaction.response.send_message("❌ Please wait before requesting another key.", ephemeral=True)
            return
            
        keys = load_keys()
        user_id = str(interaction.user.id)
        
        for key, info in keys.items():
            if not isinstance(info, dict):
                continue
            if info.get('user_id') == user_id and info.get('active', False):
                await interaction.response.send_message(
                    f"🔑 **Your Activation Key:**\n"
                    f"`{key}`\n\n"
                    f"Creation date: {info.get('creation_date', 'unknown')}\n\n"
                    f"Use this key in the Gold Menu to activate the software.",
                    ephemeral=True
                )
                return
        
        await interaction.response.send_message(
            "❌ You don't have an active key. Use the 'Get My Key' button to generate one.", 
            ephemeral=True
        )

@bot.event
async def on_ready():
    print(f'✅ Bot {bot.user} is online!')
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
                    "• **🔄 Get My Key**: Generate a new key\n"
                    "• **👀 View My Key**: Show your current key\n\n"
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
                "• **🔄 Get My Key**: Generate a new key\n"
                "• **👀 View My Key**: Show your current key\n\n"
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

# This creates a WSGI application for Gunicorn
# Render will use this when deployed
app.wsgi_app = lambda environ, start_response: app.wsgi_app(environ, start_response)
