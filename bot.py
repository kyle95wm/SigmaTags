import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

from db import TagDB

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "/app/data/tags.db")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in environment.")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Shared DB instance available to cogs via bot.db
bot.db = TagDB(DB_PATH)  # type: ignore[attr-defined]


@bot.event
async def on_ready():
    await bot.db.init()  # type: ignore[attr-defined]

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"[sync] Synced slash commands to guild {GUILD_ID}")
        else:
            await bot.tree.sync()
            print("[sync] Synced slash commands globally")
    except Exception as e:
        print("[sync] Command sync failed:", repr(e))

    print(f"Logged in as {bot.user} (id={bot.user.id})")


async def load_extensions():
    # discord.py 2.4+ supports setup_hook for async loading
    await bot.load_extension("cogs.tags")
    await bot.load_extension("cogs.tagmanage")
    await bot.load_extension("cogs.bulkimport")
    await bot.load_extension("cogs.debugtools")
    await bot.load_extension("cogs.import_from_message")

@bot.event
async def setup_hook():
    await load_extensions()


bot.run(TOKEN)
