import os
import time
import json
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))

# Must match cogs/tags.py (or set TAG_SHORTCUTS_FILE env to override)
TAG_SHORTCUTS_FILE = os.getenv("TAG_SHORTCUTS_FILE", "/app/data/tag_shortcuts.json").strip()
_CMD_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


# -------------------------
# Staff checks + helpers
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
        return "This command only works in a server (not DMs)."
    if STAFF_ROLE_ID == 0:
        return "STAFF_ROLE_ID isn’t set correctly in .env."

    try:
        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            member = await interaction.guild.fetch_member(interaction.user.id)
    except discord.Forbidden:
        return "I can’t fetch your member info (bot is missing permissions in this server)."
    except Exception as e:
        return f"I couldn’t fetch your member info: {type(e).__name__}"

    role_ids = [r.id for r in member.roles]
    if STAFF_ROLE_ID not in role_ids:
        return f"You don’t have the configured staff role in this server. (Looking for {STAFF_ROLE_ID})"

    return "Staff check failed for an unknown reason (but you appear to have the role)."


def _is_valid_shortcut_name(name: str) -> bool:
    return bool(_CMD_NAME_RE.match(name))


def _load_shortcuts_file() -> dict[str, bool]:
    """
    Stored as {"tag-name": true, ...}
    Missing/invalid file -> {}
    """
    if not TAG_SHORTCUTS_FILE:
        return {}

    try:
        with open(TAG_SHORTCUTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

    if isinstance(data, dict):
        out: dict[str, bool] = {}
        for k, v in data.items():
            nm = str(k).strip().lower()
            if nm and isinstance(v, bool):
                out[nm] = v
        return out

    if isinstance(data, list):
        out = {}
        for x in data:
            nm = str(x).strip().lower()
            if nm:
                out[nm] = True
        return out

    return {}


def _save_shortcuts_file(data: dict[str, bool]) -> None:
    if not TAG_SHORTCUTS_FILE:
        raise RuntimeError("TAG_SHORTCUTS_FILE is not set")

    parent = os.path.dirname(TAG_SHORTCUTS_FILE) or "."
    os.makedirs(parent, exist_ok=True)

    tmp = TAG_SHORTCUTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, TAG_SHORTCUTS_FILE)


def parse_hex_color(value: str | None) -> int | None:
    if not value:
        return None
    s = value.strip().lower()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None


def parse_extras(extras: str | None) -> dict:
    """
    Parse key=value pairs from a single text input.

    Supported keys:
      color=#RRGGBB (or RRGGBB)
      image=<url>
      thumbnail=<url>
      footer=<text>   (footer can contain spaces; put it last)

    Example:
      color=#5865F2 image=https://... thumbnail=https://... footer=hello there
    """
    if not extras:
        return {}

    out: dict[str, str] = {}
    raw = extras.strip()
    if not raw:
        return out

    parts = raw.split()
    i = 0
    while i < len(parts):
        p = parts[i]

        # footer consumes remainder (so it can contain spaces)
        if p.lower().startswith("footer="):
            footer_val = " ".join(parts[i:])[len("footer="):].strip()
            if footer_val:
                out["footer"] = footer_val
            break

        if "=" in p:
            k, v = p.split("=", 1)
            k = k.lower().strip()
            v = v.strip()
            if k in ("image", "thumbnail", "color") and v:
                out[k] = v

        i += 1

    return out


def embed_from_payload(payload: dict) -> discord.Embed:
    color_value = payload.get("color")
    color_obj = discord.Color(color_value) if isinstance(color_value, int) else None
    emb = discord.Embed(
        title=payload.get("title") or None,
        description=payload.get("description") or None,
        color=color_obj,
    )

    if payload.get("thumbnail"):
        emb.set_thumbnail(url=payload["thumbnail"])
    if payload.get("image"):
        emb.set_image(url=payload["image"])

    footer = payload.get("footer")
    if isinstance(footer, dict):
        emb.set_footer(text=footer.get("text") or None, icon_url=footer.get("icon_url") or None)

    author = payload.get("author")
    if isinstance(author, dict):
        emb.set_author(
            name=author.get("name") or "",
            url=author.get("url") or None,
            icon_url=author.get("icon_url") or None,
        )

    for f in (payload.get("fields") or [])[:25]:
        try:
            emb.add_field(
                name=str(f.get("name", ""))[:256] or "\u200b",
                value=str(f.get("value", ""))[:1024] or "\u200b",
                inline=bool(f.get("inline", False)),
            )
        except Exception:
            continue

    return emb


def parse_message_link(link: str) -> tuple[int, int, int] | None:
    if not link:
        return None

    s = link.strip().lstrip("<").rstrip(">")
    m = re.search(r"(?:ptb\.|canary\.)?discord\.com/channels/(\d+)/(\d+)/(\d+)", s)
    if not m:
        return None

    return int(m.group(1)), int(m.group(2)), int(m.group(3))


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
# Autocomplete
# -------------------------
async def tag_name_autocomplete(interaction: discord.Interaction, current: str):
    names = await interaction.client.db.list_names(limit=100)  # type: ignore[attr-defined]
    cur = (current or "").lower()
    filtered = [n for n in names if cur in n.lower()] if cur else names
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


# -------------------------
# Views (pagination / confirm / preview)
# -------------------------
class OwnerOnlyView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user is not None and interaction.user.id == self.owner_id


class ListPagerView(OwnerOnlyView):
    def __init__(self, owner_id: int, items: list[str], per_page: int = 25):
        super().__init__(owner_id, timeout=180)
        self.items = items
        self.per_page = per_page
        self.page = 0

    def _max_page(self) -> int:
        if not self.items:
            return 0
        return max(0, (len(self.items) - 1) // self.per_page)

    def make_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        chunk = self.items[start:end]
        desc = "\n".join(f"• `{n}`" for n in chunk) if chunk else "No tags."
        emb = discord.Embed(title="Tags", description=desc)
        emb.set_footer(text=f"Page {self.page + 1}/{self._max_page() + 1} • {len(self.items)} total")
        return emb

    def _update_buttons(self):
        self.prev.disabled = self.page <= 0  # type: ignore
        self.next.disabled = self.page >= self._max_page()  # type: ignore

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self._max_page(), self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


class ConfirmDeleteView(OwnerOnlyView):
    def __init__(self, owner_id: int, tag_name: str, bot: commands.Bot):
        super().__init__(owner_id, timeout=60)
        self.tag_name = tag_name
        self.bot = bot

    @discord.ui.button(label="Confirm delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Acknowledge the button click immediately so we don't hit the 3s interaction timeout
        await interaction.response.defer()

        ok = await self.bot.db.delete(self.tag_name)  # type: ignore[attr-defined]

        # If this tag had a shortcut enabled, remove it from the file and from Discord.
        if ok and interaction.guild is not None:
            removed_from_file = False
            try:
                data = _load_shortcuts_file()
                if self.tag_name in data:
                    data.pop(self.tag_name, None)
                    _save_shortcuts_file(data)
                    removed_from_file = True
            except Exception:
                removed_from_file = False

            # Remove from the in-memory command tree and sync to this guild.
            bot = self.bot
            try:
                bot.tree.remove_command(self.tag_name, guild=interaction.guild)
            except Exception:
                pass

            tags_cog = bot.get_cog("Tags")
            if tags_cog and hasattr(tags_cog, "remove_shortcut"):
                try:
                    await tags_cog.remove_shortcut(self.tag_name, interaction.guild)  # type: ignore[attr-defined]
                except Exception:
                    pass

            # Sync so Discord actually drops the command.
            # (This is the slow bit; we deferred above to avoid timing out.)
            try:
                await bot.tree.sync(guild=interaction.guild)
            except Exception:
                # If sync fails, the file/tree changes still stand; a restart + sync later will fix it.
                pass

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        await interaction.edit_original_response(
            content=(f"Deleted `{self.tag_name}`." if ok else f"Tag `{self.tag_name}` not found."),
            view=self,
        )


    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)


class SaveEmbedView(OwnerOnlyView):
    """
    Save an embed payload. Optionally also saves plaintext content (hybrid tag).
    FIXED:
      - hybrid save uses upsert_hybrid (so text doesn't wipe embed)
      - edit-rename support via original_name
    """
    def __init__(
        self,
        owner_id: int,
        tag_name: str,
        payload: dict,
        bot: commands.Bot,
        content: str = "",
        original_name: str | None = None,
    ):
        super().__init__(owner_id, timeout=180)
        self.tag_name = tag_name
        self.payload = payload
        self.bot = bot
        self.content = (content or "").strip()
        self.original_name = (original_name or "").strip() or None

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_staff(interaction):
            await interaction.response.send_message(await staff_fail_reason(interaction), ephemeral=True)
            return

        ts = int(time.time())
        user_id = interaction.user.id

        # Rename if needed (prevents old name from sticking around)
        if self.original_name and self.original_name != self.tag_name:
            try:
                ok = await self.bot.db.rename(self.original_name, self.tag_name, user_id, ts)  # type: ignore[attr-defined]
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            if not ok:
                await interaction.response.send_message("Original tag not found (rename failed).", ephemeral=True)
                return

        # Save
        if self.content:
            await self.bot.db.upsert_hybrid(  # type: ignore[attr-defined]
                self.tag_name,
                self.content,
                self.payload,
                user_id,
                ts,
            )
            saved_msg = f"Saved hybrid tag `{self.tag_name}` (text + embed)."
        else:
            await self.bot.db.upsert_embed(  # type: ignore[attr-defined]
                self.tag_name,
                self.payload,
                user_id,
                ts,
            )
            saved_msg = f"Saved embed tag `{self.tag_name}`."

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        await interaction.response.edit_message(content=saved_msg, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(content="Cancelled (not saved).", view=self)


# -------------------------
# Modals
# -------------------------
class StaffOnlyModal(discord.ui.Modal):
    def __init__(self, title: str):
        super().__init__(title=title)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await is_staff(interaction)


class TextTagModal(StaffOnlyModal):
    def __init__(
        self,
        *,
        bot: commands.Bot,
        mode: str,
        name_default: str = "",
        content_default: str = "",
        original_name: str | None = None,
    ):
        super().__init__(title=f"{mode} Text Tag")
        self.bot = bot
        self.original_name = (original_name or "").strip() or None

        self.name = discord.ui.TextInput(
            label="Tag name",
            placeholder="e.g. rules, faq, appeal",
            default=name_default,
            max_length=64,
        )
        self.content = discord.ui.TextInput(
            label="Content",
            style=discord.TextStyle.paragraph,
            placeholder="Type the tag response here...",
            default=content_default,
            max_length=2000,
        )
        self.add_item(self.name)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction):
        nm = self.name.value.lower().strip()
        if not nm:
            await interaction.response.send_message("Tag name can't be empty.", ephemeral=True)
            return

        ts = int(time.time())
        user_id = interaction.user.id

        # If editing and name changed: rename instead of creating a new row
        if self.original_name and self.original_name != nm:
            try:
                ok = await self.bot.db.rename(self.original_name, nm, user_id, ts)  # type: ignore[attr-defined]
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            if not ok:
                await interaction.response.send_message(f"Tag `{self.original_name}` not found (rename failed).", ephemeral=True)
                return

        await self.bot.db.upsert_text(nm, self.content.value, user_id, ts)  # type: ignore[attr-defined]
        await interaction.response.send_message(f"Saved text tag `{nm}`.", ephemeral=True)


class EmbedTagModal(StaffOnlyModal):
    """
    Modals max 5 inputs.

    Supports "text above embed" via plaintext field.
    """
    def __init__(
        self,
        *,
        bot: commands.Bot,
        mode: str,
        name_default: str = "",
        title_default: str = "",
        desc_default: str = "",
        content_default: str = "",
        extras_default: str = "",
        original_name: str | None = None,
    ):
        super().__init__(title=f"{mode} Embed Tag")
        self.bot = bot
        self.original_name = (original_name or "").strip() or None

        self.name = discord.ui.TextInput(
            label="Tag name",
            placeholder="e.g. welcome, report, support",
            default=name_default,
            max_length=64,
        )
        self.etitle = discord.ui.TextInput(
            label="Embed title (optional)",
            required=False,
            default=title_default,
            max_length=256,
        )
        self.desc = discord.ui.TextInput(
            label="Embed description (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            default=desc_default,
            max_length=4000,
        )
        self.plain = discord.ui.TextInput(
            label="Plaintext above embed (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            default=content_default,
            max_length=2000,
        )
        self.extras = discord.ui.TextInput(
            label="Extras (optional)",
            placeholder="color=#5865F2 image=<url> thumbnail=<url> footer=<text>",
            required=False,
            default=extras_default,
            max_length=1000,
        )

        self.add_item(self.name)
        self.add_item(self.etitle)
        self.add_item(self.desc)
        self.add_item(self.plain)
        self.add_item(self.extras)

    async def on_submit(self, interaction: discord.Interaction):
        nm = self.name.value.lower().strip()
        if not nm:
            await interaction.response.send_message("Tag name can't be empty.", ephemeral=True)
            return

        extras = parse_extras(self.extras.value)

        color_int = parse_hex_color(extras.get("color"))
        payload = {
            "title": self.etitle.value or None,
            "description": self.desc.value or None,
            "color": color_int,
            "image": extras.get("image"),
            "thumbnail": extras.get("thumbnail"),
            "footer": {"text": extras.get("footer")} if extras.get("footer") else None,
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        preview_embed = embed_from_payload(payload)
        content_text = (self.plain.value or "").strip()

        view = SaveEmbedView(
            owner_id=interaction.user.id,
            tag_name=nm,
            payload=payload,
            bot=self.bot,
            content=content_text,
            original_name=self.original_name,  # key for rename-on-save
        )

        note = " (will save text + embed)" if content_text else ""
        await interaction.response.send_message(
            content=f"Preview for `{nm}`{note} — hit **Save** to store it, or Cancel.",
            embed=preview_embed,
            view=view,
            ephemeral=True,
        )


# -------------------------
# Command group
# -------------------------
tagmanage = app_commands.Group(name="tagmanage", description="Manage tags (staff tools)")


@tagmanage.command(name="list", description="List tags (paginated)")
async def list_tags(interaction: discord.Interaction):
    names = await interaction.client.db.list_names(limit=500)  # type: ignore[attr-defined]
    names = sorted(names)
    if not names:
        await interaction.response.send_message("No tags yet.", ephemeral=True)
        return

    view = ListPagerView(owner_id=interaction.user.id, items=names, per_page=25)
    view._update_buttons()
    await interaction.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)


@tagmanage.command(name="create", description="Create a tag using a modal (staff only)")
@app_commands.check(is_staff)
@app_commands.describe(kind="text or embed")
@app_commands.choices(kind=[
    app_commands.Choice(name="text", value="text"),
    app_commands.Choice(name="embed", value="embed"),
])
async def create_tag(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    if kind.value == "embed":
        await interaction.response.send_modal(
            EmbedTagModal(
                bot=interaction.client,  # type: ignore[arg-type]
                mode="Create",
            )
        )
    else:
        await interaction.response.send_modal(TextTagModal(bot=interaction.client, mode="Create"))  # type: ignore[arg-type]


@tagmanage.command(name="edit", description="Edit an existing tag using a modal (staff only)")
@app_commands.check(is_staff)
@app_commands.describe(name="Tag name")
@app_commands.autocomplete(name=tag_name_autocomplete)
async def edit_tag(interaction: discord.Interaction, name: str):
    nm = name.lower().strip()
    row = await interaction.client.db.get(nm)  # type: ignore[attr-defined]
    if not row:
        await interaction.response.send_message(f"Tag `{nm}` not found.", ephemeral=True)
        return

    _, content, is_embed, embed_json = row
    if is_embed:
        try:
            data = json.loads(embed_json or "{}")
        except Exception:
            data = {}

        # build extras default from stored embed data
        extras_parts: list[str] = []
        if isinstance(data.get("color"), int):
            extras_parts.append(f"color=#{data['color']:06x}")
        if isinstance(data.get("image"), str) and data.get("image"):
            extras_parts.append(f"image={data['image']}")
        if isinstance(data.get("thumbnail"), str) and data.get("thumbnail"):
            extras_parts.append(f"thumbnail={data['thumbnail']}")
        footer = data.get("footer")
        if isinstance(footer, dict) and footer.get("text"):
            extras_parts.append(f"footer={footer['text']}")
        extras_default = " ".join(extras_parts).strip()

        await interaction.response.send_modal(
            EmbedTagModal(
                bot=interaction.client,  # type: ignore[arg-type]
                mode="Edit",
                name_default=nm,
                title_default=data.get("title") or "",
                desc_default=data.get("description") or "",
                content_default=(content or "").strip(),
                extras_default=extras_default,
                original_name=nm,  # key for rename tracking
            )
        )
    else:
        await interaction.response.send_modal(
            TextTagModal(
                bot=interaction.client,  # type: ignore[arg-type]
                mode="Edit",
                name_default=nm,
                content_default=content or "",
                original_name=nm,  # key for rename tracking
            )
        )


@tagmanage.command(name="delete", description="Delete a tag (staff only, with confirmation)")
@app_commands.check(is_staff)
@app_commands.describe(name="Tag name")
@app_commands.autocomplete(name=tag_name_autocomplete)
async def delete_tag(interaction: discord.Interaction, name: str):
    nm = name.lower().strip()
    view = ConfirmDeleteView(owner_id=interaction.user.id, tag_name=nm, bot=interaction.client)  # type: ignore[arg-type]
    await interaction.response.send_message(
        content=f"Delete `{nm}`? This can’t be undone.",
        view=view,
        ephemeral=True,
    )


@tagmanage.command(name="raw", description="Show stored JSON/text for a tag (staff only)")
@app_commands.check(is_staff)
@app_commands.describe(name="Tag name")
@app_commands.autocomplete(name=tag_name_autocomplete)
async def raw_tag(interaction: discord.Interaction, name: str):
    nm = name.lower().strip()
    row = await interaction.client.db.get(nm)  # type: ignore[attr-defined]
    if not row:
        await interaction.response.send_message("Not found.", ephemeral=True)
        return

    _, content, is_embed, embed_json = row
    if is_embed:
        body = embed_json or "{}"
        msg = f"**{nm}** (embed)\n```json\n{body}\n```"
        if (content or "").strip():
            msg += f"\n\n**content:**\n```txt\n{(content or '').strip()}\n```"
    else:
        body = content or ""
        msg = f"**{nm}** (text)\n```txt\n{body}\n```"

    if len(msg) > 1900:
        msg = msg[:1900] + "\n```…```"

    await interaction.response.send_message(msg, ephemeral=True)


@tagmanage.command(name="import_link", description="Import a tag from a Discord message link (staff only)")
@app_commands.check(is_staff)
@app_commands.describe(
    name="New tag name to save as",
    link="Copy Message Link from Discord (right click message → Copy Message Link)",
)
async def import_link(interaction: discord.Interaction, name: str, link: str):
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
        return

    parsed = parse_message_link(link)
    if not parsed:
        await interaction.response.send_message("That doesn’t look like a valid Discord message link.", ephemeral=True)
        return

    guild_id, channel_id, message_id = parsed
    if guild_id != interaction.guild.id:
        await interaction.response.send_message("That message link is from a different server.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(channel_id)
        except Exception:
            await interaction.response.send_message("I can’t access that channel (missing perms or it doesn’t exist).", ephemeral=True)
            return

    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
        await interaction.response.send_message("That link isn’t to a text channel message I can read.", ephemeral=True)
        return

    try:
        msg = await channel.fetch_message(message_id)
    except discord.Forbidden:
        await interaction.response.send_message("I can’t read that message (need View Channel + Read Message History).", ephemeral=True)
        return
    except discord.NotFound:
        await interaction.response.send_message("Message not found (deleted or wrong link).", ephemeral=True)
        return
    except Exception as e:
        await interaction.response.send_message(f"Failed to fetch message: {type(e).__name__}", ephemeral=True)
        return

    new_name = name.lower().strip()
    if not new_name:
        await interaction.response.send_message("Tag name can’t be empty.", ephemeral=True)
        return

    text = (msg.content or "").strip()

    if msg.embeds:
        payload = payload_from_embed(msg.embeds[0])
        if not payload:
            await interaction.response.send_message("I found an embed, but couldn’t parse anything useful from it.", ephemeral=True)
            return

        preview = embed_from_payload(payload)
        view = SaveEmbedView(
            owner_id=interaction.user.id,
            tag_name=new_name,
            payload=payload,
            bot=interaction.client,  # type: ignore[arg-type]
            content=text,
        )

        note = "embed + text" if text else "embed"
        await interaction.response.send_message(
            content=f"Imported {note} from message — preview for `{new_name}`. Hit **Save** to store it.",
            embed=preview,
            view=view,
            ephemeral=True,
        )
        return

    if not text:
        await interaction.response.send_message("That message has no content and no embeds to import.", ephemeral=True)
        return

    await interaction.client.db.upsert_text(new_name, text, interaction.user.id, int(time.time()))  # type: ignore[attr-defined]
    await interaction.response.send_message(f"Imported text tag `{new_name}` from that message.", ephemeral=True)


# -------------------------
# Shortcut commands (staff only)
# -------------------------
@tagmanage.command(name="shortcut_set", description="Enable/disable a tag slash shortcut (e.g. /after-signup)")
@app_commands.check(is_staff)
@app_commands.describe(name="Tag name", enabled="Enable or disable the shortcut")
@app_commands.autocomplete(name=tag_name_autocomplete)
async def shortcut_set(interaction: discord.Interaction, name: str, enabled: bool):
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
        return

    nm = (name or "").strip().lower()
    if not nm:
        await interaction.response.send_message("Name can’t be empty.", ephemeral=True)
        return

    # Must be a valid slash command name
    if not _is_valid_shortcut_name(nm):
        await interaction.response.send_message(
            "That tag name can’t be a slash command. Use only lowercase letters, numbers, - or _, max 32 chars.",
            ephemeral=True,
        )
        return

    # Must exist (only required when enabling)
    if enabled:
        row = await interaction.client.db.get(nm)  # type: ignore[attr-defined]
        if not row:
            await interaction.response.send_message(f"Tag `{nm}` not found.", ephemeral=True)
            return

    data = _load_shortcuts_file()
    if enabled:
        data[nm] = True
    else:
        data.pop(nm, None)

    try:
        _save_shortcuts_file(data)
    except Exception as e:
        await interaction.response.send_message(f"Failed to write shortcuts file: {type(e).__name__}", ephemeral=True)
        return

    # Apply to the in-memory command tree
    bot = interaction.client  # type: ignore[assignment]

    if enabled:
        tags_cog = bot.get_cog("Tags")
        if tags_cog and hasattr(tags_cog, "register_shortcuts_from_file"):
            try:
                # Register as a GUILD command (fast propagation)
                await tags_cog.register_shortcuts_from_file(interaction.guild)  # type: ignore[attr-defined]
            except Exception:
                pass
    else:
        # Disable: remove even if the tag itself no longer exists.
        tags_cog = bot.get_cog("Tags")
        if tags_cog and hasattr(tags_cog, "remove_shortcut"):
            try:
                await tags_cog.remove_shortcut(nm, interaction.guild)  # type: ignore[attr-defined]
            except Exception:
                pass
        else:
            try:
                bot.tree.remove_command(nm, guild=interaction.guild)
            except Exception:
                pass

    # Fast-ish: sync to this guild so it appears quickly
    try:
        await bot.tree.sync(guild=interaction.guild)
    except Exception as e:
        await interaction.response.send_message(
            f"Updated file, but sync failed: {type(e).__name__}. Restarting the bot will also apply it.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        (f"Enabled `/{nm}`." if enabled else f"Disabled `/{nm}`."),
        ephemeral=True,
    )


@tagmanage.command(name="shortcut_list", description="List enabled tag slash shortcuts")
@app_commands.check(is_staff)
async def shortcut_list(interaction: discord.Interaction):
    data = _load_shortcuts_file()
    enabled = sorted([k for k, v in data.items() if v is True])
    if not enabled:
        await interaction.response.send_message("No shortcuts enabled.", ephemeral=True)
        return
    body = "\n".join(f"• `/{n}`" for n in enabled[:200])
    await interaction.response.send_message(f"Enabled shortcuts:\n{body}", ephemeral=True)


@tagmanage.command(name="shortcut_sync", description="Re-register shortcuts from file and guild-sync")
@app_commands.check(is_staff)
async def shortcut_sync(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
        return

    bot = interaction.client  # type: ignore[assignment]
    tags_cog = bot.get_cog("Tags")
    if tags_cog and hasattr(tags_cog, "register_shortcuts_from_file"):
        try:
            await tags_cog.register_shortcuts_from_file(interaction.guild)  # type: ignore[attr-defined]
        except Exception as e:
            await interaction.response.send_message(f"Register failed: {type(e).__name__}", ephemeral=True)
            return

    try:
        await bot.tree.sync(guild=interaction.guild)
    except Exception as e:
        await interaction.response.send_message(f"Sync failed: {type(e).__name__}", ephemeral=True)
        return

    await interaction.response.send_message("Synced shortcuts for this server.", ephemeral=True)


@tagmanage.command(name="debug", description="Debug staff role detection (shows what the bot sees)")
async def debug_staff(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Run this in a server, not DMs.", ephemeral=True)
        return

    member_cached = interaction.guild.get_member(interaction.user.id)

    try:
        member_fetched = await interaction.guild.fetch_member(interaction.user.id)
        fetch_error = None
    except Exception as e:
        member_fetched = None
        fetch_error = repr(e)

    def role_ids(m):
        return [r.id for r in getattr(m, "roles", [])] if m else []

    msg = (
        f"STAFF_ROLE_ID={STAFF_ROLE_ID}\n"
        f"cached_member={'yes' if member_cached else 'no'}\n"
        f"cached_role_ids={role_ids(member_cached)}\n"
        f"fetched_member={'yes' if member_fetched else 'no'}\n"
        f"fetched_role_ids={role_ids(member_fetched)}\n"
        f"fetch_error={fetch_error}\n"
    )
    await interaction.response.send_message(f"```txt\n{msg}\n```", ephemeral=True)


# -------------------------
# Cog + error handler
# -------------------------
class TagManage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(tagmanage)

    async def cog_unload(self):
        self.bot.tree.remove_command(tagmanage.name, type=discord.AppCommandType.chat_input)


async def setup(bot: commands.Bot):
    await bot.add_cog(TagManage(bot))

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)

        if isinstance(original, app_commands.CheckFailure):
            msg = await staff_fail_reason(interaction)
        elif isinstance(original, app_commands.MissingPermissions):
            msg = "Discord says you're missing permissions for this command in this channel."
        else:
            msg = f"Error: {type(original).__name__}: {original}"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            # Interaction expired (usually because we hit rate limits / long sync). Nothing to do.
            return

        if not isinstance(original, (app_commands.CheckFailure, app_commands.MissingPermissions)):
            raise error
