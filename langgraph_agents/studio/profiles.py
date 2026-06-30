import json
import uuid
from pathlib import Path

PROFILES_DIR = Path(__file__).parent / "profiles"
PROFILES_DIR.mkdir(exist_ok=True)


def _path(pid: str) -> Path:
    return PROFILES_DIR / f"{pid}.json"


def list_profiles() -> list:
    result = []
    for f in sorted(PROFILES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result.append({"id": data["id"], "name": data.get("name", data["id"])})
        except Exception:
            pass
    return result


def load_profile(pid: str) -> dict:
    p = _path(pid)
    if not p.exists():
        raise FileNotFoundError(f"Profile '{pid}' not found")
    return json.loads(p.read_text(encoding="utf-8"))


def save_profile(profile: dict) -> dict:
    if not profile.get("id"):
        profile["id"] = uuid.uuid4().hex[:8]
    _path(profile["id"]).write_text(
        json.dumps(profile, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return profile


def delete_profile(pid: str) -> None:
    p = _path(pid)
    if p.exists():
        p.unlink()
