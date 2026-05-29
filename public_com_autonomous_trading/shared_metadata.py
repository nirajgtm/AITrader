"""Watchlist sharing via the private AiTrader-sharedMetadata GitHub repo.

The repo is a flat directory of usernames; each username dir holds watchlist.json.
A local clone lives ONLY in the gitignored state/shared_metadata/ (never committed
to this repo). Access is gated by whether a clone-or-pull succeeds, and that result
is cached in state/mind/shared_metadata.json so the dashboard's hot render path
doesn't shell out to git on every request.

What ships to the shared repo is SCRUBBED: a watch entry keeps ticker, target,
thesis/hypothesis, stop, trigger (condition+level), kind, watch_for, and expected
trend. It strips dollar sizing, account references, and any owner name/email -- the
autonomous watchlist doesn't carry sizing today, but the scrub is defensive so PII
can never leak even if the schema grows.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_DIR = Path(__file__).resolve().parent
CLONE = _DIR / "state" / "shared_metadata"
CACHE = _DIR / "state" / "mind" / "shared_metadata.json"
REPO = "https://github.com/nirajgtm/AiTrader-sharedMetadata.git"
BRANCH = "main"
PULL_TTL_SECONDS = 120  # how long a clone/pull is considered fresh for the browse

# Fields that survive the scrub. Everything else (sizing, account, owner) is dropped.
_KEEP_FIELDS = ("ticker", "kind", "condition", "level", "expected_trend",
                "watch_for", "hypothesis", "target", "stop", "added", "notes")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _git(*args: str, timeout: float = 25.0) -> subprocess.CompletedProcess:
    """Run a git command inside the clone dir. Never raises on non-zero; callers
    inspect returncode. Times out so a hung network call can't block a render."""
    return subprocess.run(
        ["git", "-C", str(CLONE), *args],
        capture_output=True, text=True, timeout=timeout)


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(**fields) -> None:
    data = _load_cache()
    data.update(fields)
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _clone_or_pull() -> bool:
    """Clone the repo if absent, else pull. Returns True on success (= access).
    The empty-repo case (no commits / no main branch yet) still counts as access:
    a fresh clone of an empty repo succeeds, and a pull that fails only because
    there's nothing upstream yet is not a loss of access."""
    try:
        if (CLONE / ".git").exists():
            r = _git("pull", "--ff-only", "origin", BRANCH)
            if r.returncode == 0:
                return True
            # An empty upstream (no main branch yet) makes pull fail though we do
            # have access. Treat a reachable-but-empty remote as access.
            ls = _git("ls-remote", REPO, timeout=25.0)
            return ls.returncode == 0
        CLONE.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["git", "clone", REPO, str(CLONE)],
            capture_output=True, text=True, timeout=60.0)
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def refresh(force: bool = False) -> bool:
    """Clone-or-pull (honoring a short TTL unless forced) and cache the access result
    with a timestamp. Returns whether access is granted. This is the network call;
    keep it OFF the hot render path -- call has_access() there instead."""
    cache = _load_cache()
    last = cache.get("last_pull")
    if not force and last:
        try:
            age = (datetime.now(timezone.utc).astimezone()
                   - datetime.fromisoformat(last)).total_seconds()
            if age < PULL_TTL_SECONDS and cache.get("access"):
                return True
        except ValueError:
            pass
    ok = _clone_or_pull()
    _save_cache(access=bool(ok), last_pull=_now_iso())
    return ok


def has_access() -> bool:
    """Cheap, cache-only access check for the render path. The cache is populated by
    refresh(). Falls back to the presence of a clone if the cache is missing."""
    cache = _load_cache()
    if "access" in cache:
        return bool(cache["access"])
    return (CLONE / ".git").exists()


def my_username() -> str:
    """The owner's username = their GitHub login (the repo's root dirs are usernames).
    Cached so the render path doesn't shell out. Empty string if it can't be resolved
    (no gh / not logged in), in which case sharing is unavailable."""
    cache = _load_cache()
    if cache.get("username"):
        return str(cache["username"])
    try:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True, timeout=15.0)
        name = r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        name = ""
    if name:
        _save_cache(username=name)
    return name


def share_mode() -> str:
    """The owner's sharing setting: 'friends' or 'only_me' (default). Persisted in the
    same gitignored cache; never leaves this machine."""
    return str(_load_cache().get("share_mode") or "only_me")


def set_share_mode(mode: str) -> None:
    _save_cache(share_mode="friends" if mode == "friends" else "only_me")


def usernames() -> list[str]:
    """Root-level directory names in the clone = the usernames sharing a watchlist.
    Empty list if the clone is absent or the repo has no users yet."""
    if not CLONE.exists():
        return []
    out = []
    for p in sorted(CLONE.iterdir()):
        if p.is_dir() and not p.name.startswith("."):
            out.append(p.name)
    return out


def read_watchlist(username: str) -> list[dict]:
    """The shared (already-scrubbed) entries for one username, from the clone.
    Tolerates either a bare list or a {entries:[...]} wrapper. Never raises."""
    if not username or "/" in username or "\\" in username or ".." in username:
        return []
    path = CLONE / username / "watchlist.json"
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        data = data.get("entries") or []
    return [e for e in data if isinstance(e, dict)]


def scrub_entry(entry: dict) -> dict:
    """Keep only the shareable fields of a watch entry; drop everything else (so any
    sizing/account/owner field, present or future, is removed). Returns a new dict."""
    return {k: entry[k] for k in _KEEP_FIELDS if k in entry}


def scrub_watchlist(entries: list) -> list[dict]:
    return [scrub_entry(e) for e in entries if isinstance(e, dict) and e.get("ticker")]


def share(username: str, entries: list) -> bool:
    """Scrub the entries, write them to <username>/watchlist.json in the clone, and
    push directly to main. Returns True on a successful push. Requires access (a
    working clone). Creates an initial commit if the repo was empty."""
    if not username:
        return False
    if not refresh():
        return False
    scrubbed = scrub_watchlist(entries)
    user_dir = CLONE / username
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "watchlist.json").write_text(
            json.dumps({"updated": _now_iso(), "entries": scrubbed}, indent=2))
    except OSError:
        return False
    # Ensure we're on main even when the repo started empty (detached/unborn HEAD).
    _git("checkout", "-B", BRANCH)
    if _git("add", f"{username}/watchlist.json").returncode != 0:
        return False
    commit = _git("commit", "-m", f"Update {username} watchlist")
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
        return False
    return _git("push", "-u", "origin", BRANCH, timeout=60.0).returncode == 0


def unshare(username: str) -> bool:
    """Remove the user's shared watchlist from the repo and push. Used when the owner
    switches sharing back to 'Only me'. A missing file is treated as success."""
    if not username:
        return False
    if not refresh():
        return False
    path = CLONE / username / "watchlist.json"
    if not path.exists():
        return True
    _git("checkout", "-B", BRANCH)
    _git("rm", f"{username}/watchlist.json")
    commit = _git("commit", "-m", f"Stop sharing {username} watchlist")
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
        return False
    return _git("push", "-u", "origin", BRANCH, timeout=60.0).returncode == 0
