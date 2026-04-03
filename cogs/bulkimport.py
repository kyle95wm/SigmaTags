import re
import time
import discord
from discord import app_commands
from discord.ext import commands

TICKETS_BOT_ID_DEFAULT = 1459032967660703801

bulkimport = app_commands.Group(name="bulkimport", description="Bulk import tags from another bot (automatic)")

# --- helpers ---

def normalize_tag_name(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^a-z0-9_\-]", "", name)
    return name[:64]

def payload_from_embed(embed: discord.Embed) -> dict:
    d = embed.to_dict()

    payload: dict = {}
    if d.get("title"):
        payload["title"] = d["title"]
    if d.get("description"):
        payload["description"] = d["description"]
    if isinstance(d.get("color"), int):
        payload["color"] = d["color"]

    img = d.get("image")
    if isinstance(img, dict) and img.get("url"):
        payload["image"] = img["url"]

    thumb = d.get("thumbnail")
    if isinstance(thumb, dict) and thumb.get("url"):
        payload["thumbnail"] = thumb["url"]

    footer = d.get("footer")
    if isinstance(footer, dict):
        ft = {}
        if footer.get("text"):
            ft["text"] = footer["text"]
        if footer.get("icon_url"):
            ft["icon_url"] = footer["icon_url"]
        if ft:
            payload["footer"] = ft

    author = d.get("author")
    if isinstance(author, dict):
        au = {}
        if author.get("name"):
            au["name"] = author["name"]
        if author.get("url"):
            au["url"] = author["url"]
        if author.get("icon_url"):
            au["icon_url"] = author["icon_url"]
        if au:
            payload["author"] = au

    fields = d.get("fields")
    if isinstance(fields, list) and fields:
        cleaned = []
        for f in fields[:25]:
            if not isinstance(f, dict):
                continue
            cleaned.append(
                {
                    "name": f.get("name", ""),
                    "value": f.get("value", ""),
                    "inline": bool(f.get("inline", False)),
                }
            )
        if cleaned:
            payload["fields"] = cleaned

    return payload

NAME_PATTERNS = [
    # name: after-signup / tag: after-signup
    re.compile(r"(?im)^\s*(?:name|tag)\s*[:=]\s*`?([a-z0-9_\- ]{1,64})`?\s*$"),
    # `after-signup`
    re.compile(r"`([a-z0-9_\-]{1,64})`", re.I),
    # "after-signup" (quoted)
    re.compile(r"(?i)\"([a-z0-9_\-]{1,64})\""),
]

# command-ish patterns in the message you type to the Tickets bot:
# !tag after-signup / /tag after-signup / t!tag after-signup / tag after-signup
CMD_PATTERNS = [
    re.compile(r"(?i)\b(?:/|!|t!|\.|\$)?tag\s+([a-z0-9_\-]{1,64})\b"),
    re.compile(r"(?i)\b(?:/|!|t!|\.|\$)?tags?\s+view\s+([a-z0-9_\-]{1,64})\b"),
]

def extract_name_from_text(text: str) -> str | None:
    if not text:
        return None
    for rx in NAME_PATTERNS:
        m = rx.search(text)
        if m:
            nm = normalize_tag_name(m.group(1))
            if nm:
                return nm
    return None

def extract_name_from_commandish(text: str) -> str | None:
    if not text:
        return None
    for rx in CMD_PATTERNS:
        m = rx.search(text)
        if m:
            nm = normalize_tag_name(m.group(1))
            if nm:
                return nm
    return None


class AutoImportState:
    __slots__ = ("active", "owner_id", "tickets_bot_id", "imported", "last_name", "last_kind")

    def __init__(self, owner_id: int, tickets_bot_id: int):
        self.active = True
        self.owner_id = owner_id
        self.tickets_bot_id = tickets_bot_id
        self.imported = 0
        self.last_name: str | None = None
        self.last_kind: str | None = None


class BulkImport(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # key: (guild_id, channel_id)
        self.state: dict[tuple[int, int], AutoImportState] = {}

    def _key(self, guild_id: int, channel_id: int) -> tuple[int, int]:
        return (guild_id, channel_id)

    async def _get_state(self, message: discord.Message) -> AutoImportState | None:
        if not message.guild:
            return None
        return self.state.get(self._key(message.guild.id, message.channel.id))

    async def _infer_tag_name(self, full_msg: discord.Message) -> str | None:
        # 1) try the Tickets bot message itself
        nm = extract_name_from_text(full_msg.content or "")
        if nm:
            return nm

        # 2) if it replied to something, try the referenced message (your command)
        ref = full_msg.reference
        if ref and ref.message_id:
            try:
                ref_msg = await full_msg.channel.fetch_message(ref.message_id)
                nm = extract_name_from_commandish(ref_msg.content or "")
                if nm:
                    return nm
                nm = extract_name_from_text(ref_msg.content or "")
                if nm:
                    return nm
            except Exception:
                pass

        # 3) embed title fallback (only if it looks like a “name”, not a sentence)
        if full_msg.embeds:
            title = (full_msg.embeds[0].title or "").strip()
            if title and len(title) <= 64 and not re.search(r"\s{2,}", title):
                nm = normalize_tag_name(title)
                if nm:
                    return nm

        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author is None:
            return
        if self.bot.user and message.author.id == self.bot.user.id:
            return

        st = await self._get_state(message)
        if not st or not st.active:
            return

        # only react to Tickets bot
        if message.author.id != st.tickets_bot_id:
            return

        # fetch full message via REST for embeds/content reliability
        try:
            full_msg = await message.channel.fetch_message(message.id)
        except Exception:
            full_msg = message

        text = (full_msg.content or "").strip()
        embeds = list(full_msg.embeds or [])

        if not text and not embeds:
            return

        tag_name = await self._infer_tag_name(full_msg)
        if not tag_name:
            # keep noise low: only reply if it looks like an import attempt (has embed or long text)
            try:
                await message.reply(
                    "Skipped (couldn’t auto-detect the tag name). "
                    "Tip: make Tickets bot reply to your command message like `!tag name` or `/tag name`.",
                    mention_author=False,
                )
            except Exception:
                pass
            return

        try:
            ts = int(time.time())
            user_id = st.owner_id

            # Build payload(s)
            embed_payload = payload_from_embed(embeds[0]) if embeds else None

            if text and embed_payload:
                # Hybrid save (doesn't overwrite itself)
                await self.bot.db.upsert_hybrid(tag_name, text, embed_payload, user_id, ts)  # type: ignore[attr-defined]
                kind = "hybrid (text+embed)"
            elif embed_payload:
                await self.bot.db.upsert_embed(tag_name, embed_payload, user_id, ts)  # type: ignore[attr-defined]
                kind = "embed"
            else:
                await self.bot.db.upsert_text(tag_name, text, user_id, ts)  # type: ignore[attr-defined]
                kind = "text"

            st.imported += 1
            st.last_name = tag_name
            st.last_kind = kind

            try:
                await message.reply(f"Imported `{tag_name}` ({kind}).", mention_author=False)
            except Exception:
                pass

        except Exception as e:
            try:
                await message.reply(f"Failed `{tag_name}`: {type(e).__name__}", mention_author=False)
            except Exception:
                pass


# --- slash commands ---

@bulkimport.command(name="start", description="Start automatic bulk import in this channel")
@app_commands.describe(tickets_bot_id="User ID of the source Tickets bot (optional)")
async def bulk_start(interaction: discord.Interaction, tickets_bot_id: str | None = None):
    cog: BulkImport | None = interaction.client.get_cog("BulkImport")  # type: ignore
    if cog is None:
        await interaction.response.send_message("BulkImport cog not loaded.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server channel, not DMs.", ephemeral=True)
        return

    tid = TICKETS_BOT_ID_DEFAULT
    if tickets_bot_id:
        try:
            tid = int(tickets_bot_id.strip())
        except ValueError:
            await interaction.response.send_message("tickets_bot_id must be a number.", ephemeral=True)
            return

    key = (interaction.guild.id, interaction.channel_id)
    cog.state[key] = AutoImportState(owner_id=interaction.user.id, tickets_bot_id=tid)

    await interaction.response.send_message(
        "Auto bulk import is ON in this channel.\n"
        "Now make the Tickets bot post tags (ideally as a reply to your command like `!tag name` or `/tag name`).\n"
        "Stop with `/bulkimport stop`.",
        ephemeral=True,
    )

@bulkimport.command(name="status", description="Show import status for this channel")
async def bulk_status(interaction: discord.Interaction):
    cog: BulkImport | None = interaction.client.get_cog("BulkImport")  # type: ignore
    if cog is None:
        await interaction.response.send_message("BulkImport cog not loaded.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server channel, not DMs.", ephemeral=True)
        return

    key = (interaction.guild.id, interaction.channel_id)
    st = cog.state.get(key)
    if not st or not st.active:
        await interaction.response.send_message("Auto bulk import is not running in this channel.", ephemeral=True)
        return

    last = f"`{st.last_name}` ({st.last_kind})" if st.last_name else "None yet"
    await interaction.response.send_message(
        f"Running.\nTickets bot ID: `{st.tickets_bot_id}`\nImported: `{st.imported}`\nLast: {last}",
        ephemeral=True,
    )

@bulkimport.command(name="stop", description="Stop automatic bulk import in this channel")
async def bulk_stop(interaction: discord.Interaction):
    cog: BulkImport | None = interaction.client.get_cog("BulkImport")  # type: ignore
    if cog is None:
        await interaction.response.send_message("BulkImport cog not loaded.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server channel, not DMs.", ephemeral=True)
        return

    key = (interaction.guild.id, interaction.channel_id)
    st = cog.state.get(key)
    if not st or not st.active:
        await interaction.response.send_message("Auto bulk import isn’t running in this channel.", ephemeral=True)
        return

    if interaction.user.id != st.owner_id:
        await interaction.response.send_message("Only the person who started import can stop it.", ephemeral=True)
        return

    st.active = False
    await interaction.response.send_message("Auto bulk import stopped for this channel.", ephemeral=True)


async def setup(bot: commands.Bot):
    existing = bot.tree.get_command("bulkimport")
    if existing is not None:
        bot.tree.remove_command("bulkimport")

    await bot.add_cog(BulkImport(bot))
    bot.tree.add_command(bulkimport)
