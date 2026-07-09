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
                id         TEXT        PRIMARY KEY,
                name       TEXT        NOT NULL,
                data       JSONB       NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


async def _pg_list() -> list:
    async with _pool.connection() as conn:
        cur = await conn.execute("SELECT id, name FROM profiles ORDER BY name")
        rows = await cur.fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]


async def _pg_load(pid: str) -> dict:
    async with _pool.connection() as conn:
        cur = await conn.execute("SELECT data FROM profiles WHERE id = %s", (pid,))
        row = await cur.fetchone()
    if row is None:
        raise FileNotFoundError(f"Profile '{pid}' not found")
    return row[0]  # psycopg3 decodes JSONB → dict


async def _pg_save(profile: dict) -> dict:
    if not profile.get("id"):
        profile["id"] = uuid.uuid4().hex[:8]
    async with _pool.connection() as conn:
        await conn.execute(
            """INSERT INTO profiles (id, name, data)
               VALUES (%s, %s, %s::jsonb)
               ON CONFLICT (id) DO UPDATE
                 SET name       = EXCLUDED.name,
                     data       = EXCLUDED.data,
                     updated_at = NOW()""",
            (profile["id"], profile.get("name", profile["id"]), json.dumps(profile)),
        )
    return profile


async def _pg_delete(pid: str) -> None:
    async with _pool.connection() as conn:
        await conn.execute("DELETE FROM profiles WHERE id = %s", (pid,))


# ── Filesystem backend (fallback / local dev) ──────────────────────────────────

def _path(pid: str) -> Path:
    return PROFILES_DIR / f"{pid}.json"


def _fs_list() -> list:
    result = []
    for f in sorted(PROFILES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result.append({"id": data["id"], "name": data.get("name", data["id"])})
        except Exception:
            pass
    return result


def _fs_load(pid: str) -> dict:
    p = _path(pid)
    if not p.exists():
        raise FileNotFoundError(f"Profile '{pid}' not found")
    return json.loads(p.read_text(encoding="utf-8"))


def _fs_save(profile: dict) -> dict:
    if not profile.get("id"):
        profile["id"] = uuid.uuid4().hex[:8]
    _path(profile["id"]).write_text(
        json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return profile


def _fs_delete(pid: str) -> None:
    p = _path(pid)
    if p.exists():
        p.unlink()


# ── Public API ─────────────────────────────────────────────────────────────────

async def setup():
    """Call once on startup when Postgres is available."""
    if _pool:
        await _pg_setup()


async def list_profiles() -> list:
    return await _pg_list() if _pool else _fs_list()


async def load_profile(pid: str) -> dict:
    return await _pg_load(pid) if _pool else _fs_load(pid)


async def save_profile(profile: dict) -> dict:
    return await _pg_save(profile) if _pool else _fs_save(profile)


async def delete_profile(pid: str) -> None:
    if _pool:
        await _pg_delete(pid)
    else:
        _fs_delete(pid)
