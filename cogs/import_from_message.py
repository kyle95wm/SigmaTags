import os
import time
import discord
from discord import app_commands
from discord.ext import commands

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))


# -------------------------
# Staff checks
# -------------------------
async def is_staff(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or interaction.user is None:
        return False
    if STAFF_ROLE_ID == 0:
        return False

    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            return False

    return any(role.id == STAFF_ROLE_ID for role in member.roles)


async def staff_fail_reason(interaction: discord.Interaction) -> str:
    if interaction.guild is None:
        return "Use this in a server (not DMs)."
    if STAFF_ROLE_ID == 0:
        return "STAFF_ROLE_ID isn’t set correctly in your .env."

    try:
        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            member = await interaction.guild.fetch_member(interaction.user.id)
    except discord.Forbidden:
        return "I can’t fetch your member info (missing permissions)."
    except Exception as e:
        return f"I couldn’t fetch your member info: {type(e).__name__}"

    if STAFF_ROLE_ID not in [r.id for r in member.roles]:
        return "You don’t have permission to use this."

    return "Staff check failed for an unknown reason."


# -------------------------
# Embed payload conversion
# -------------------------
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


# -------------------------
# Modal
# -------------------------
class _TagNameModal(discord.ui.Modal, title="Import message as tag"):
    tag_name = discord.ui.TextInput(
        label="Tag name",
        placeholder="after-signup",
        min_length=1,
        max_length=64,
    )

    def __init__(self, bot: commands.Bot, message: discord.Message):
        super().__init__()
        self.bot = bot
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        # staff-only (again, in case someone somehow bypasses opening the modal)
        if not await is_staff(interaction):
            await interaction.response.send_message(await staff_fail_reason(interaction), ephemeral=True)
            return

        name = str(self.tag_name.value).strip().lower()

        if " " in name:
            await interaction.response.send_message("Tag names can’t contain spaces.", ephemeral=True)
            return

        # Fetch full message (embeds/content more reliable)
        try:
            channel = self.message.channel
            if isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
                full_msg = await channel.fetch_message(self.message.id)
            else:
                full_msg = self.message
        except Exception:
            full_msg = self.message

        text = (full_msg.content or "").strip()
        embeds = list(full_msg.embeds or [])

        if not text and not embeds:
            await interaction.response.send_message("That message has no content or embeds to import.", ephemeral=True)
            return

        ts = int(time.time())
        user_id = interaction.user.id

        try:
            if embeds and text:
                payload = payload_from_embed(embeds[0])
                await self.bot.db.upsert_hybrid(name, text, payload, user_id, ts)  # type: ignore[attr-defined]
                kind = "hybrid (text+embed)"
            elif embeds:
                payload = payload_from_embed(embeds[0])
                await self.bot.db.upsert_embed(name, payload, user_id, ts)  # type: ignore[attr-defined]
                kind = "embed"
            else:
                await self.bot.db.upsert_text(name, text, user_id, ts)  # type: ignore[attr-defined]
                kind = "text"

        except Exception as e:
            await interaction.response.send_message(f"Failed to import `{name}`: {type(e).__name__}", ephemeral=True)
            return

        await interaction.response.send_message(f"Imported `{name}` ({kind}).", ephemeral=True)


# -----
# Cog (optional)
# -----
class ImportFromMessage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


# -----
# Context menu (module-level)
# -----
@app_commands.context_menu(name="Import as tag")
async def import_as_tag(interaction: discord.Interaction, message: discord.Message):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
        return

    if not await is_staff(interaction):
        await interaction.response.send_message(await staff_fail_reason(interaction), ephemeral=True)
        return

    await interaction.response.send_modal(_TagNameModal(interaction.client, message))  # type: ignore[arg-type]


async def setup(bot: commands.Bot):
    await bot.add_cog(ImportFromMessage(bot))

    # Remove if it already exists (avoids duplicates on reload)
    existing = bot.tree.get_command("Import as tag", type=discord.AppCommandType.message)
    if existing is not None:
        bot.tree.remove_command(existing)

    bot.tree.add_command(import_as_tag)
