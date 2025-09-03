from bot import app, bot
import threading
import asyncio

def run_bot():
    """Run the Discord bot in a separate thread"""
    try:
        # Create a new event loop for the thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot.loop = loop
        loop.run_until_complete(bot.start(bot.token))
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")

# Start the bot in a background thread when imported
bot_thread = threading.Thread(target=run_bot)
bot_thread.daemon = True
bot_thread.start()

# This is the WSGI application that Render will use
application = app
