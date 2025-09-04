from bot import app
import threading
import time

def run_bot():
    """Run the Discord bot in a separate thread"""
    # Import here to avoid voice-related imports in the main thread
    from bot import bot, BOT_TOKEN
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")

# Start the bot in a background thread
bot_thread = threading.Thread(target=run_bot)
bot_thread.daemon = True
bot_thread.start()

# Give the bot time to start
time.sleep(5)

# This is the WSGI application that Render will use
application = app
