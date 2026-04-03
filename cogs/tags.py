import json
import os
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


TAG_SHORTCUTS_FILE = os.getenv("TAG_SHORTCUTS_FILE", "/app/data/tag_shortcuts.json").strip()
_CMD_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def embed_from_dict(d: dict) -> discord.Embed:
    color_value = d.get("color")
    color_obj = discord.Color(color_value) if isinstance(color_value, int) else None

    emb = discord.Embed(
        title=d.get("title") or None,
        description=d.get("description") or None,
        color=color_obj,
    )

    for f in d.get("fields", [])[:25]:
        emb.add_field(
            name=str(f.get("name", ""))[:256] or "\u200b",
            value=str(f.get("value", ""))[:1024] or "\u200b",
            inline=bool(f.get("inline", False)),
        )

    footer = d.get("footer")
    if isinstance(footer, dict):
        emb.set_footer(
            text=footer.get("text") or None,
            icon_url=footer.get("icon_url") or None,
        )

    thumb = d.get("thumbnail")
    if isinstance(thumb, str) and thumb:
        emb.set_thumbnail(url=thumb)

    img = d.get("image")
    if isinstance(img, str) and img:
        emb.set_image(url=img)

    author = d.get("author")
    if isinstance(author, dict):
        emb.set_author(
            name=author.get("name") or "",
            url=author.get("url") or None,
            icon_url=author.get("icon_url") or None,
        )

    return emb


async def tag_name_autocomplete(interaction: discord.Interaction, current: str):
    names = await interaction.client.db.list_names(limit=100)  # type: ignore[attr-defined]
    cur = (current or "").lower()
    filtered = [n for n in names if cur in n.lower()] if cur else names
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


def _is_valid_shortcut_name(name: str) -> bool:
    return bool(_CMD_NAME_RE.match(name))


def _load_shortcuts_allowlist() -> list[str]:
    """
    Returns a list of tag names that should get slash shortcuts.
    File is optional; if missing or invalid, returns [].
    """
    if not TAG_SHORTCUTS_FILE:
        return []

    try:
        with open(TAG_SHORTCUTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        return []

    if isinstance(data, list):
        names = [str(x).strip().lower() for x in data]
        return [n for n in names if n]

    if isinstance(data, dict):
        out: list[str] = []
        for k, v in data.items():
            if v is True:
                n = str(k).strip().lower()
                if n:
                    out.append(n)
        return out

    return []


class Tags(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._registered_shortcuts_by_guild: dict[int, set[str]] = {}
        self._startup_done = False

    async def _send_tag(self, interaction: discord.Interaction, tag_name: str, ephemeral: bool = False):
        nm = tag_name.lower().strip()
        row = await self.bot.db.get(nm)  # type: ignore[attr-defined]
        if not row:
            if interaction.response.is_done():
                await interaction.followup.send(f"Tag `{tag_name}` not found.", ephemeral=True)
            else:
                await interaction.response.send_message(f"Tag `{tag_name}` not found.", ephemeral=True)
            return

        _, content, is_embed, embed_json = row
        text = (content or "").strip()

        if not is_embed:
            if interaction.response.is_done():
                await interaction.followup.send(text or "", ephemeral=ephemeral)
            else:
                await interaction.response.send_message(text or "", ephemeral=ephemeral)
            return

        try:
            data = json.loads(embed_json or "{}")
            emb = embed_from_dict(data)
        except Exception as e:
            print(f"[tag] failed to render embed tag '{nm}': {repr(e)}")
            msg = "I saved that embed, but couldn't render it. Try `/tagmanage raw` then `/tagmanage edit`."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

        if interaction.response.is_done():
            if text:
                await interaction.followup.send(content=text, embed=emb, ephemeral=ephemeral)
            else:
                await interaction.followup.send(embed=emb, ephemeral=ephemeral)
        else:
            if text:
                await interaction.response.send_message(content=text, embed=emb, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(embed=emb, ephemeral=ephemeral)

    @app_commands.command(name="tag", description="Show a tag")
    @app_commands.describe(name="Tag name", ephemeral="Only you can see the response")
    @app_commands.autocomplete(name=tag_name_autocomplete)
    async def tag(self, interaction: discord.Interaction, name: str, ephemeral: bool = False):
        await self._send_tag(interaction, name, ephemeral=ephemeral)

    def _make_shortcut_handler(self, tag_name: str):
        async def _handler(interaction: discord.Interaction, ephemeral: bool = False):
            await self._send_tag(interaction, tag_name, ephemeral=ephemeral)

        return _handler

    async def register_shortcuts_from_file(self, guild: Optional[discord.abc.Snowflake] = None):
        """
        Creates per-tag slash commands (opt-in via TAG_SHORTCUTS_FILE).
        Guild-scoped only (fast).
        """
        names = _load_shortcuts_allowlist()
        if not names or guild is None:
            return

        guild_id = int(getattr(guild, "id", 0) or 0)
        if guild_id == 0:
            return

        bucket = self._registered_shortcuts_by_guild.setdefault(guild_id, set())

        for nm in names:
            nm = nm.lower().strip()
            if not nm:
                continue
            if not _is_valid_shortcut_name(nm):
                continue
            if nm in bucket:
                continue

            existing = self.bot.tree.get_command(nm, guild=guild)
            if existing is not None:
                continue

            cmd = app_commands.Command(
                name=nm,
                description=f"Shortcut for tag '{nm}'",
                callback=self._make_shortcut_handler(nm),
            )
            self.bot.tree.add_command(cmd, guild=guild)
            bucket.add(nm)

    async def remove_shortcut(self, name: str, guild: discord.abc.Snowflake):
        guild_id = int(getattr(guild, "id", 0) or 0)
        if guild_id == 0:
            return

        nm = (name or "").strip().lower()
        if not nm:
            return

        try:
            self.bot.tree.remove_command(nm, guild=guild)
        except Exception:
            pass

        bucket = self._registered_shortcuts_by_guild.get(guild_id)
        if bucket is not None:
            bucket.discard(nm)
            if not bucket:
                self._registered_shortcuts_by_guild.pop(guild_id, None)

    async def _restore_shortcuts_for_guild(self, guild: discord.Guild):
        await self.register_shortcuts_from_file(guild=guild)
        try:
            await self.bot.tree.sync(guild=guild)
        except Exception as e:
            print(f"[shortcuts] guild sync failed for {guild.id}: {type(e).__name__}: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        # Only run once per process
        if self._startup_done:
            return
        self._startup_done = True

        # Restore shortcuts for every guild we're currently in
        for g in list(self.bot.guilds):
            await self._restore_shortcuts_for_guild(g)

        print(f"[shortcuts] restored from {TAG_SHORTCUTS_FILE} for {len(self.bot.guilds)} guild(s)")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        # If the bot joins a new guild, also restore there
        await self._restore_shortcuts_for_guild(guild)

    async def cog_unload(self):
        # Best-effort cleanup: remove whatever we think we registered (guild-scoped).
        for guild_id, names in list(self._registered_shortcuts_by_guild.items()):
            guild_obj = discord.Object(id=guild_id)
            for nm in list(names):
                try:
                    self.bot.tree.remove_command(nm, guild=guild_obj)
                except Exception:
                    pass
        self._registered_shortcuts_by_guild.clear()


async def setup(bot: commands.Bot):
    await bot.add_cog(Tags(bot))
