import aiosqlite
import json
from typing import Optional, Dict, Any, Tuple, List

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
  name TEXT PRIMARY KEY,
  content TEXT,
  is_embed INTEGER NOT NULL DEFAULT 0,
  embed_json TEXT,
  created_by INTEGER,
  updated_by INTEGER,
  updated_at INTEGER
);
"""


def _truthy_sql_text(expr: str) -> str:
    # helper for "is not null and trim(...) != ''" patterns
    return f"({expr} IS NOT NULL AND trim({expr}) != '')"


class TagDB:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(SCHEMA)
            await db.commit()

    # -------------------------
    # Core upserts (legacy behavior)
    # -------------------------

    async def upsert_text(self, name: str, content: str, user_id: int, ts: int):
        """
        Text-only upsert (LEGACY): wipes embed.
        """
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO tags(name, content, is_embed, embed_json, created_by, updated_by, updated_at)
                VALUES(?, ?, 0, NULL, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  content=excluded.content,
                  is_embed=0,
                  embed_json=NULL,
                  updated_by=excluded.updated_by,
                  updated_at=excluded.updated_at
                """,
                (name, content, user_id, user_id, ts),
            )
            await db.commit()

    async def upsert_embed(self, name: str, embed: Dict[str, Any], user_id: int, ts: int):
        """
        Embed-only upsert (LEGACY): wipes text.
        """
        embed_json = json.dumps(embed, ensure_ascii=False)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO tags(name, content, is_embed, embed_json, created_by, updated_by, updated_at)
                VALUES(?, NULL, 1, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  content=NULL,
                  is_embed=1,
                  embed_json=excluded.embed_json,
                  updated_by=excluded.updated_by,
                  updated_at=excluded.updated_at
                """,
                (name, embed_json, user_id, user_id, ts),
            )
            await db.commit()

    async def upsert_hybrid(self, name: str, content: str, embed: Dict[str, Any], user_id: int, ts: int):
        """
        Hybrid upsert: stores BOTH content + embed_json in the same row.
        """
        embed_json = json.dumps(embed, ensure_ascii=False)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO tags(name, content, is_embed, embed_json, created_by, updated_by, updated_at)
                VALUES(?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  content=excluded.content,
                  is_embed=1,
                  embed_json=excluded.embed_json,
                  updated_by=excluded.updated_by,
                  updated_at=excluded.updated_at
                """,
                (name, content, embed_json, user_id, user_id, ts),
            )
            await db.commit()

    # -------------------------
    # Safer upserts (preserve the other half)
    # -------------------------

    async def set_text_preserve_embed(self, name: str, content: str, user_id: int, ts: int):
        """
        Set/insert text WITHOUT wiping an existing embed_json.
        is_embed will reflect whether embed_json exists.
        """
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"""
                INSERT INTO tags(name, content, is_embed, embed_json, created_by, updated_by, updated_at)
                VALUES(?, ?, 0, NULL, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  content=excluded.content,
                  is_embed=CASE WHEN {_truthy_sql_text("tags.embed_json")} THEN 1 ELSE 0 END,
                  updated_by=excluded.updated_by,
                  updated_at=excluded.updated_at
                """,
                (name, content, user_id, user_id, ts),
            )
            await db.commit()

    async def set_embed_preserve_text(self, name: str, embed: Dict[str, Any], user_id: int, ts: int):
        """
        Set/insert embed_json WITHOUT wiping existing text content.
        """
        embed_json = json.dumps(embed, ensure_ascii=False)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"""
                INSERT INTO tags(name, content, is_embed, embed_json, created_by, updated_by, updated_at)
                VALUES(?, NULL, 1, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  is_embed=1,
                  embed_json=excluded.embed_json,
                  updated_by=excluded.updated_by,
                  updated_at=excluded.updated_at
                """,
                (name, embed_json, user_id, user_id, ts),
            )
            await db.commit()

    # -------------------------
    # Rename (fixes your duplicate-name issue)
    # -------------------------

    async def rename(self, old_name: str, new_name: str, user_id: int, ts: int, overwrite: bool = False) -> bool:
        """
        Atomically rename a tag (primary key change) so the old name does NOT remain.

        - If overwrite=False and new_name exists -> raises ValueError
        - If overwrite=True and new_name exists -> deletes new_name then renames old_name into it
        """
        old_name = (old_name or "").strip()
        new_name = (new_name or "").strip()

        if not old_name or not new_name:
            raise ValueError("old_name and new_name are required")
        if old_name == new_name:
            return True

        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN")

            # Ensure old exists
            cur = await db.execute("SELECT 1 FROM tags WHERE name = ? LIMIT 1", (old_name,))
            if await cur.fetchone() is None:
                await db.execute("ROLLBACK")
                return False

            # Handle conflict on new name
            cur = await db.execute("SELECT 1 FROM tags WHERE name = ? LIMIT 1", (new_name,))
            new_exists = (await cur.fetchone()) is not None
            if new_exists and not overwrite:
                await db.execute("ROLLBACK")
                raise ValueError(f"tag '{new_name}' already exists")

            if new_exists and overwrite:
                await db.execute("DELETE FROM tags WHERE name = ?", (new_name,))

            # Rename in-place (this is the key fix)
            cur = await db.execute(
                """
                UPDATE tags
                SET name = ?, updated_by = ?, updated_at = ?
                WHERE name = ?
                """,
                (new_name, user_id, ts, old_name),
            )

            if cur.rowcount != 1:
                await db.execute("ROLLBACK")
                return False

            await db.commit()
            return True

    # -------------------------
    # Reads / lists
    # -------------------------

    async def delete(self, name: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("DELETE FROM tags WHERE name = ?", (name,))
            await db.commit()
            return cur.rowcount > 0

    async def get(self, name: str) -> Optional[Tuple[str, Optional[str], int, Optional[str]]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name, content, is_embed, embed_json FROM tags WHERE name = ?",
                (name,),
            )
            return await cur.fetchone()

    async def list_names(self, limit: int = 100) -> List[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name FROM tags ORDER BY name ASC LIMIT ?",
                (limit,),
            )
            return [r[0] for r in await cur.fetchall()]

    async def list_tags(self, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Handy for debugging/admin screens: returns rows with booleans.
        """
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                f"""
                SELECT name,
                       {_truthy_sql_text("content")} as has_text,
                       {_truthy_sql_text("embed_json")} as has_embed,
                       updated_at
                FROM tags
                ORDER BY name ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            return [
                {
                    "name": name,
                    "has_text": bool(has_text),
                    "has_embed": bool(has_embed),
                    "updated_at": updated_at,
                }
                for (name, has_text, has_embed, updated_at) in rows
            ]
