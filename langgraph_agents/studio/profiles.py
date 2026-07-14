import json
import os
import uuid
from pathlib import Path

PROFILES_DIR = Path(os.getenv("PROFILES_DIR", str(Path(__file__).parent / "profiles")))
PROFILES_DIR.mkdir(exist_ok=True)

_pool = None  # set by app.py lifespan when Postgres is ready


def set_pool(pool):
    global _pool
    _pool = pool


# ── Postgres backend ───────────────────────────────────────────────────────────

async def _pg_setup():
    async with _pool.connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id         TEXT        NOT NULL,
                owner      TEXT        NOT NULL DEFAULT '',
                name       TEXT        NOT NULL,
                data       JSONB       NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (id, owner)
            )
        """)
        # Migration for existing tables created without owner column
        await conn.execute(
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT ''"
        )


async def _pg_list(owner: str) -> list:
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name FROM profiles WHERE owner = %s ORDER BY name", (owner,)
        )
        rows = await cur.fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]


async def _pg_load(pid: str, owner: str) -> dict:
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "SELECT data FROM profiles WHERE id = %s AND owner = %s", (pid, owner)
        )
        row = await cur.fetchone()
    if row is None:
        raise FileNotFoundError(f"Profile '{pid}' not found")
    return row[0]  # psycopg3 decodes JSONB → dict


async def _pg_save(profile: dict, owner: str) -> dict:
    if not profile.get("id"):
        profile["id"] = uuid.uuid4().hex[:8]
    async with _pool.connection() as conn:
        await conn.execute(
            """INSERT INTO profiles (id, owner, name, data)
               VALUES (%s, %s, %s, %s::jsonb)
               ON CONFLICT (id, owner) DO UPDATE
                 SET name       = EXCLUDED.name,
                     data       = EXCLUDED.data,
                     updated_at = NOW()""",
            (profile["id"], owner, profile.get("name", profile["id"]), json.dumps(profile)),
        )
    return profile


async def _pg_delete(pid: str, owner: str) -> None:
    async with _pool.connection() as conn:
        await conn.execute(
            "DELETE FROM profiles WHERE id = %s AND owner = %s", (pid, owner)
        )


# ── Filesystem backend (fallback / local dev) ──────────────────────────────────

def _user_dir(owner: str) -> Path:
    d = PROFILES_DIR / (owner or "_anonymous")
    d.mkdir(exist_ok=True)
    return d


def _path(pid: str, owner: str) -> Path:
    return _user_dir(owner) / f"{pid}.json"


def _fs_list(owner: str) -> list:
    result = []
    for f in sorted(_user_dir(owner).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result.append({"id": data["id"], "name": data.get("name", data["id"])})
        except Exception:
            pass
    return result


def _fs_load(pid: str, owner: str) -> dict:
    p = _path(pid, owner)
    if not p.exists():
        raise FileNotFoundError(f"Profile '{pid}' not found")
    return json.loads(p.read_text(encoding="utf-8"))


def _fs_save(profile: dict, owner: str) -> dict:
    if not profile.get("id"):
        profile["id"] = uuid.uuid4().hex[:8]
    _path(profile["id"], owner).write_text(
        json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return profile


def _fs_delete(pid: str, owner: str) -> None:
    p = _path(pid, owner)
    if p.exists():
        p.unlink()


# ── Public API ─────────────────────────────────────────────────────────────────

async def setup():
    """Call once on startup when Postgres is available."""
    if _pool:
        await _pg_setup()


async def list_profiles(owner: str = "") -> list:
    return await _pg_list(owner) if _pool else _fs_list(owner)


async def load_profile(pid: str, owner: str = "") -> dict:
    return await _pg_load(pid, owner) if _pool else _fs_load(pid, owner)


async def save_profile(profile: dict, owner: str = "") -> dict:
    return await _pg_save(profile, owner) if _pool else _fs_save(profile, owner)


async def delete_profile(pid: str, owner: str = "") -> None:
    if _pool:
        await _pg_delete(pid, owner)
    else:
        _fs_delete(pid, owner)
