# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Persistent state under ~/.pulsarcode (mode 0700, secrets 0600).

  config.json      profiles (provider + model), active profile, recents
  keys.json        API keys, one per profile, mode 0600
  claude_config/   isolated Claude Code profile (never touches ~/.claude)
  cache/           model catalogs, 6h TTL
  run/             adapter state files and session leases
  logs/            adapter log (only when PULSAR_LOG=1)

v1 files (nim.key, active_model, model_history) are imported once; nim.key is then
kept as nim.key.migrated, so a lost config.json never brings a forgotten key back.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pulsar import providers as P

RECENT_CAP = 20

# Written into every Pulsar home. `pulsarcode uninstall` refuses to delete a folder without a marker,
# so a mistaken PULSAR_HOME (your home directory, /) can never be wiped. The marker is only ever written
# into a folder Pulsar created, found empty, or that already holds Pulsar Code's own files: a mistaken
# PULSAR_HOME pointing at a project or ~/.local is refused instead of being "adopted".
MARKER = ".pulsarcode-home"
MARKER_TEXT = "Pulsar Code home. `pulsarcode uninstall` only removes folders that carry this file.\n"

# Everything Pulsar Code (or its installer) may keep in its home; uninstall removes these and nothing else.
HOME_CHILDREN = ("app", "app.new", "app.old", "venv", "venv.old", "bin", "run", "cache", "logs", "plugin", "claude_config",
                 "stage", "canonical", "config.json", "keys.json", "settings.json", "nim.key", "nim.key.migrated",
                 "active_model", "model_history", "onboarding_complete", "nim_sonar_catalog.json",
                 "claude-install.sh", MARKER)
# What the Python side writes on its own (no installer), plus the files v1 kept there.
_STATE_CHILDREN = frozenset(("run", "cache", "logs", "plugin", "claude_config", "stage", "config.json", "keys.json",
                             "settings.json", "nim.key", "nim.key.migrated", "active_model", "model_history",
                             "onboarding_complete", "nim_sonar_catalog.json", "canonical"))
# Folders every command needs (ensure_home makes them 0700), then everything in the home that must stay
# private (0700 folders, 0600 files) even when an older build, an installer or a dotfiles script made it
# 0755 / 0644. launch.tighten_home covers the same folders on the launch path.
_HOME_DIRS = ("cache", "run", "run/sessions", "logs", "claude_config")
_PRIVATE_DIRS = _HOME_DIRS + ("plugin", "stage")
_PRIVATE_FILES = (MARKER, "config.json", "keys.json", "settings.json", "nim.key", "nim.key.migrated", "active_model", "model_history")
# Folders whose entries Pulsar writes itself (catalog caches, Core's state, session files, the adapter log), with
# how many folder levels below them are walked too (run/sessions/<sid>/settings.json): their files become 0600 and
# their folders 0700, so a home loosened with `chmod -R go+rX` is fixed by any command, not only by the next write.
_PRIVATE_CONTENT_DIRS = (("cache", 0), ("run", 0), ("run/sessions", 1), ("logs", 0))


class HomeError(RuntimeError):
    """PULSAR_HOME points at a folder Pulsar Code must not write into (or delete)."""


def home() -> Path:
    # An empty PULSAR_HOME (`export PULSAR_HOME=` in a CI template or a dotfile) means the default, never
    # the current folder. A relative value is refused by ensure_home (unsafe_home).
    return Path(os.path.expanduser(os.environ.get("PULSAR_HOME") or "~/.pulsarcode"))


def _json_dict(path: Path) -> Optional[Dict[str, Any]]:
    data = _read_json(path)
    return data if isinstance(data, dict) else None


def _pulsar_config(path: Path) -> bool:
    """Pulsar's own config.json: a JSON object with a top-level "profiles" object and an "active" key.
    install.sh (pulsar_config) and install.ps1 ($pulsarCfg) parse the file and apply this same rule."""
    data = _json_dict(path)
    return data is not None and isinstance(data.get("profiles"), dict) and "active" in data


def _temp_of_ours(name: str) -> bool:
    """write_private's temp files: .config.json.ab12cd (left behind only by a crash)."""
    return any(name.startswith("." + child + ".") for child in ("config.json", "keys.json", "settings.json"))


def home_evidence(path: Path) -> str:
    """Why `path` is (or was) a Pulsar Code home, or "": the engine, a Pulsar config, a v1 key, the profile."""
    if (path / "app" / "pulsar" / "__init__.py").is_file():
        return "app"
    if _pulsar_config(path / "config.json"):
        return "config.json"
    if (path / "nim.key").is_file():
        return "nim.key"
    if (path / "claude_config").is_dir():
        return "claude_config"
    return ""


def foreign_entries(path: Path) -> List[str]:
    """Entries of `path` that Pulsar Code never creates (sorted)."""
    try:
        names = [p.name for p in path.iterdir()]
    except OSError:
        return []
    return sorted(n for n in names if n not in HOME_CHILDREN and not _temp_of_ours(n))


# The folders the installers replace whole and uninstall removes whole. One of them holding someone else's
# files keeps a folder from being adopted, whatever else there shows it was Pulsar's.
_REPLACED = ("app", "app.new", "app.old", "venv", "venv.old", "bin")
_UV_FILES = frozenset(("uv", "uvx", "uvw", "uv-install.sh", "uv.exe", "uvx.exe", "uvw.exe", "uv-install.ps1"))


def in_the_way(path: Path) -> List[str]:
    """The folders of _REPLACED in `path` that hold someone else's files: an app without pulsar/__init__.py,
    a venv without pyvenv.cfg, a bin with more than uv. An empty folder has nothing to lose; a file with one
    of these names, or a folder that cannot be listed, is always in the way. (The installers go further and
    adopt an earlier install only next to nothing else.)"""
    found = []
    for n in _REPLACED:
        p = path / n
        if not os.path.lexists(str(p)):
            continue
        try:
            inside = [c.name for c in p.iterdir()] if p.is_dir() else None
        except OSError:
            inside = None
        if inside is not None and (
                not inside
                or (n.startswith("app") and (p / "pulsar" / "__init__.py").is_file())
                or (n.startswith("venv") and ((p / "pyvenv.cfg").is_file()
                                              or os.access(str(p / "bin" / "python"), os.X_OK)))
                or (n == "bin" and set(inside) <= _UV_FILES)):
            continue
        found.append(n)
    return found


def _entries(path: Path) -> Optional[List[str]]:
    """The names in `path` apart from write_private's leftover temp files (sorted), or None if unreadable."""
    try:
        return sorted(p.name for p in path.iterdir() if not _temp_of_ours(p.name))
    except OSError:
        return None


def _only_pulsar_state(path: Path) -> bool:
    """Every entry is something the Python side writes, and the JSON files are Pulsar's own."""
    try:
        names = [p.name for p in path.iterdir()]
    except OSError:
        return False
    for n in names:
        if _temp_of_ours(n):
            continue
        if n not in _STATE_CHILDREN:
            return False
    if (path / "config.json").exists() and not _pulsar_config(path / "config.json"):
        return False
    for name in ("keys.json", "settings.json"):
        if (path / name).exists() and _json_dict(path / name) is None:
            return False
    return True


def marker_kind(path: Path) -> str:
    """"created", "adopted", "legacy" (a marker without provenance) or "" (no marker).

    The line rule install.sh (marker_trusted) and install.ps1 share: lines end at "\n" only, one trailing
    "\r" is dropped (CRLF), and a line after the first that starts with the word "created" or "adopted"
    (case-sensitive, followed by a space or nothing) is provenance. A lone "\r", "\f", "\v" or U+2028 does
    not end a line, and the bytes are read as UTF-8 without BOM detection (a UTF-16 marker is not trusted).
    """
    try:
        # read_bytes: no newline translation. errors="replace": bytes that are not UTF-8 are judged, never a crash
        text = (path / MARKER).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.split("\n")[1:]:
        if line.endswith("\r"):
            line = line[:-1]
        word = line.split(" ", 1)[0]
        if word in ("created", "adopted"):
            return word
    return "legacy"


def _write_marker(path: Path, kind: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        _write_atomic(path / MARKER, MARKER_TEXT + "%s %s\n" % (kind, stamp))
    except OSError:
        pass


def _make_private(path: Path, folder: bool) -> None:
    """Drop the group and other bits of a folder (to 0700) or a file (to 0600) in the Pulsar home.

    Never follows a symlink, never touches what someone else owns, and leaves anything of another type
    alone (like launch.tighten_home). Not on Windows, where modes do not apply.
    """
    if os.name == "nt":
        return
    try:
        st = os.lstat(str(path))
    except OSError:
        return
    right_type = stat.S_ISDIR(st.st_mode) if folder else stat.S_ISREG(st.st_mode)
    if not right_type or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077 == 0:
        return
    try:
        os.chmod(str(path), stat.S_IMODE(st.st_mode) & 0o700)
    except OSError:
        pass


def unsafe_home(path: Path) -> str:
    """Why `path` can never be a Pulsar home (a relative path, a filesystem root, your home directory), or ""."""
    if not path.is_absolute():
        return "is a relative path"           # it would name a different folder in every project
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        return "cannot be resolved"
    if real == Path(real.anchor) or real.parent == real:
        return "is a filesystem root"
    try:
        if real == Path.home().resolve():
            return "is your home directory"
    except (OSError, RuntimeError, KeyError):
        pass
    return ""


def ensure_home() -> Path:
    """Create (or check) the Pulsar home and its fixed sub-folders. Raises HomeError for a foreign folder."""
    h = home()
    why = unsafe_home(h)
    if why:
        raise HomeError("PULSAR_HOME=%s %s; point it at a folder of its own (default ~/.pulsarcode)" % (h, why))
    kind = ""
    try:
        h.mkdir(parents=True)
        kind = "created"
    except FileExistsError:
        if not h.is_dir():
            raise HomeError("PULSAR_HOME=%s is not a folder" % h)
        found = marker_kind(h)
        evidence = home_evidence(h)
        if found in ("created", "adopted"):
            kind = ""                               # already ours
        elif _only_pulsar_state(h):             # empty, or state Pulsar wrote before the marker existed
            # Only an empty folder becomes "created" (uninstall may then remove it whole). Names alone prove
            # nothing: settings.json, logs/ and cache/ are just as likely another tool's, so state from an
            # older build must carry Pulsar's own evidence (a Pulsar config, the v1 key, the profile).
            names = _entries(h)
            if names == []:
                kind = "created"
            elif names and evidence:
                kind = "adopted"
            else:
                raise HomeError("PULSAR_HOME=%s already holds files Pulsar Code cannot tell are its own (%s). "
                                "Nothing was written. Point PULSAR_HOME at a new or empty folder (default "
                                "~/.pulsarcode)." % (h, ", ".join((names or ["unreadable"])[:5])))
        elif evidence and not in_the_way(h) and (found != "legacy" or evidence == "app" or not foreign_entries(h)):
            # an older Pulsar Code home (v1/v2, the installer's app). A marker without provenance (written
            # by a development build into any folder) proves nothing, so then the folder must hold only
            # Pulsar's files, or the engine itself. Never when its app, venv or bin is someone else's:
            # the installers replace those folders whole and uninstall removes them.
            kind = "adopted"
        else:
            shown = ", ".join(_not_ours(h)[:5])
            raise HomeError("PULSAR_HOME=%s already holds files Pulsar Code did not create (%s). Nothing was "
                            "written. Point PULSAR_HOME at a new or empty folder (default ~/.pulsarcode)."
                            % (h, shown or "unknown files"))
    _check_private_home(h)
    for sub in _HOME_DIRS:
        (h / sub).mkdir(mode=0o700, exist_ok=True)
    if kind:
        _write_marker(h, kind)
    _keep_private(h)
    return h


def _check_private_home(h: Path) -> None:
    """chmod 0700, and refuse a home that is not private (as OpenSSH refuses such a key folder).

    Whoever can write into the home can replace config.json and send your stored keys to their own
    server, so a home another user owns, or that stays writable by others after the chmod (a filesystem
    that ignores modes), raises HomeError before anything is written. POSIX only.
    """
    if os.name == "nt":
        try:
            os.chmod(h, 0o700)
        except OSError:
            pass
        return
    try:
        st = os.stat(str(h))
    except OSError as exc:
        raise HomeError("PULSAR_HOME=%s cannot be checked (%s)" % (h, exc.strerror or exc))
    if st.st_uid != os.getuid():
        raise HomeError("PULSAR_HOME=%s belongs to another user (uid %d, you are uid %d). Nothing was written. "
                        "Point PULSAR_HOME at a folder of your own (default ~/.pulsarcode)."
                        % (h, st.st_uid, os.getuid()))
    try:
        os.chmod(h, 0o700)
        st = os.stat(str(h))
    except OSError:
        pass
    if st.st_mode & 0o022:
        raise HomeError("PULSAR_HOME=%s stays writable by other users (mode %s) and could not be made private. "
                        "Nothing was written. Point PULSAR_HOME at a folder on a filesystem that keeps modes "
                        "(default ~/.pulsarcode)." % (h, oct(st.st_mode & 0o777)[2:]))


def _not_ours(h: Path) -> List[str]:
    """What makes `h` foreign, for the HomeError message: folders in the way, unknown entries, then JSON files
    that carry one of Pulsar's names but are not Pulsar's (a project's own config.json)."""
    shown = in_the_way(h) + foreign_entries(h)
    if (h / "config.json").exists() and not _pulsar_config(h / "config.json"):
        shown.append("config.json")
    shown += [n for n in ("keys.json", "settings.json") if (h / n).exists() and _json_dict(h / n) is None]
    return shown


def _closed_folder(path: Path, follow: bool) -> bool:
    """A real folder you own that nobody else can write to (so no entry in it can be swapped under us)."""
    try:
        st = os.stat(str(path)) if follow else os.lstat(str(path))
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & 0o022 == 0


def _keep_private(h: Path) -> None:
    """Private even before any launch: `key`, `status` or `model` alone must not leave a 0755 cache/ or
    claude_config/ (a home made by mkdir -p, an installer or an older build) or a 0644 marker behind, nor a
    0644 catalog cache or session file inside those folders (a home loosened with chmod -R go+rX)."""
    for sub in _PRIVATE_DIRS:
        _make_private(h / sub, folder=True)
    for name in _PRIVATE_FILES:
        _make_private(h / name, folder=False)
    if os.name == "nt" or not _closed_folder(h, follow=True):
        return
    for sub, depth in _PRIVATE_CONTENT_DIRS:
        _keep_entries_private(h / sub, depth)


def _keep_entries_private(folder: Path, depth: int) -> None:
    """0600 / 0700 for what sits directly in `folder`, and `depth` folder levels further down. Only inside a real
    folder you own that nobody else can write to: a symlink or a folder someone else owns is left alone."""
    if not _closed_folder(folder, follow=False):
        return
    try:
        names = os.listdir(str(folder))
    except OSError:
        return
    for name in names:
        # _make_private lstat()s each entry: a regular file becomes 0600, a folder 0700, a symlink is skipped
        _make_private(folder / name, folder=False)
        _make_private(folder / name, folder=True)
        if depth > 0:
            _keep_entries_private(folder / name, depth - 1)


def _private_home_folder(h: Path) -> None:
    """chmod 0700 the home folder itself on a read-only command, the way ensure_home's os.chmod does: through
    a symlinked PULSAR_HOME (~/.pulsarcode -> /data/pulsarcode) too, but only a folder you own. load() calls it
    only for a home that is not your home directory or a filesystem root (unsafe_home)."""
    if os.name == "nt":
        return
    try:
        real = os.path.realpath(str(h))
        st = os.stat(real)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077 == 0:
        return
    try:
        os.chmod(real, stat.S_IMODE(st.st_mode) & 0o700)
    except OSError:
        pass


def _inside_home(path: Path) -> bool:
    try:
        h = os.path.abspath(str(home()))
        return os.path.commonpath([os.path.abspath(str(path)), h]) == h
    except ValueError:                      # another drive on Windows
        return False


def write_private(path: Path, text: str) -> None:
    """Atomic write with mode 0600 from the first byte.

    Anything inside the Pulsar home goes through ensure_home() first, so no write (a catalog cache,
    settings.json, the isolated Claude profile) can land in a folder PULSAR_HOME points at by mistake.
    """
    if _inside_home(path):
        ensure_home()
    _write_atomic(path, text)


def _write_atomic(path: Path, text: str) -> None:
    """write_private without the ensure_home() check (ensure_home itself writes the marker with it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):      # RecursionError: JSON nested thousands of levels deep
        return None


@dataclass
class Profile:
    id: str
    provider: str
    model: str = ""
    base_url: str = ""          # custom endpoints only
    native: bool = False        # custom endpoints only
    context: int = 0            # known context window of `model`
    needs_key: bool = True      # custom endpoints only: False for a server that takes no key

    def resolve(self) -> P.Provider:
        if self.provider == "custom":
            return P.custom_provider(self.base_url, native=self.native, needs_key=self.needs_key)
        prov = P.get(self.provider)
        if prov is None:
            raise KeyError("unknown provider " + self.provider)
        if self.base_url and self.base_url != prov.base_url:
            from dataclasses import replace
            return replace(prov, base_url=self.base_url)
        return prov

    @property
    def label(self) -> str:
        if self.provider == "custom":
            host = re.sub(r"^https?://", "", self.base_url).split("/")[0].rsplit("@", 1)[-1]
            return P.clean_text(host, 80) or "custom"
        prov = P.get(self.provider)
        return prov.name if prov else P.clean_text(self.provider, 80)


@dataclass
class State:
    active: str = ""
    profiles: Dict[str, Profile] = field(default_factory=dict)
    recent: List[Dict[str, str]] = field(default_factory=list)
    launches: int = 0
    prefs: Dict[str, Any] = field(default_factory=dict)
    # A profile this process runs in place of `active` without making it the saved default (a configured
    # local endpoint that answered at launch). Never written to config.json.
    override: str = ""

    # ---- profiles -------------------------------------------------------
    def current(self) -> Optional[Profile]:
        return self.profiles.get(self.override or self.active)

    def upsert(self, profile: Profile, activate: bool = True) -> Profile:
        old = self.profiles.get(profile.id)
        if old is not None and not profile.model:
            profile.model = old.model
            profile.context = old.context
        self.profiles[profile.id] = profile
        if activate:
            self.active = profile.id
            self.override = ""
        return profile

    def set_model(self, profile_id: str, model: str, context: int = 0) -> None:
        prof = self.profiles[profile_id]
        prof.model = model
        prof.context = context
        if profile_id != self.override:           # a model for the override keeps the saved default
            self.active = profile_id
            self.override = ""
        self.push_recent(profile_id, model)

    def push_recent(self, profile_id: str, model: str) -> None:
        if not model:
            return
        entry = {"profile": profile_id, "model": model}
        self.recent = [entry] + [r for r in self.recent if r != entry]
        self.recent = self.recent[:RECENT_CAP]

    def remove(self, profile_id: str) -> None:
        self.profiles.pop(profile_id, None)
        self.recent = [r for r in self.recent if r.get("profile") != profile_id]
        if self.override == profile_id:
            self.override = ""
        if self.active == profile_id:
            self.active = next(iter(self.profiles), "")

    # ---- io ---------------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        return {
            "version": 2,
            "active": self.active,
            "profiles": {k: vars(v) for k, v in self.profiles.items()},
            "recent": self.recent,
            "launches": self.launches,
            "prefs": self.prefs,
        }


def clean_base_url(url: object) -> str:
    """A stored base URL as `pulsarcode key` saves it: without user:password (or a bare token@), a ?query or a
    #fragment. Pulsar never saves those (they carry credentials, and keys travel in a header), so a hand-edited
    config.json cannot put one into a request, a notice or a log line. Not a string: "". A URL that cannot be
    parsed is kept as it is: Core refuses it before sending anything."""
    if not isinstance(url, str):
        return ""
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if "@" not in split.netloc and not split.query and not split.fragment and not url.endswith(("?", "#")):
        return url
    return urllib.parse.urlunsplit(split._replace(netloc=split.netloc.rsplit("@", 1)[-1], query="", fragment=""))
_FIELD_TYPES = {"str": str, "bool": bool, "int": int}


def _profile_from(data: Dict[str, Any]) -> Optional[Profile]:
    """A Profile from its config.json entry. A field of the wrong type (a hand edit, a file Pulsar did not
    write) keeps its default; an entry without a usable id or provider is None."""
    kw: Dict[str, Any] = {}
    for name, f in Profile.__dataclass_fields__.items():
        if name not in data:
            continue
        want = _FIELD_TYPES.get(f.type if isinstance(f.type, str) else getattr(f.type, "__name__", ""))
        value = data[name]
        if want is not None and (not isinstance(value, want) or (want is int and isinstance(value, bool))):
            continue
        kw[name] = value
    try:
        return Profile(**kw)
    except TypeError:
        return None


def config_path() -> Path:
    return home() / "config.json"


def keys_path() -> Path:
    return home() / "keys.json"


def load() -> State:
    h = home()
    if marker_kind(h) in ("created", "adopted") and not unsafe_home(h):
        # read-only commands (status, model, models) never write, so they never reach ensure_home: keep a
        # home Pulsar already owns private here too. A folder it has not claimed is never touched, and
        # neither is one ensure_home refuses outright (your home directory, a filesystem root).
        _private_home_folder(h)
        _keep_private(h)
    raw = _read_json(config_path())
    state = State()
    if isinstance(raw, dict):
        # every field is checked for its shape: a config.json Pulsar did not write (PULSAR_HOME pointed at a
        # project by mistake) or one edited by hand is read as far as it makes sense, never a traceback
        state.active = str(raw.get("active") or "")
        profiles = raw.get("profiles")
        for pid, data in (profiles if isinstance(profiles, dict) else {}).items():
            prof = _profile_from(data) if isinstance(data, dict) else None
            if prof is None:
                continue
            if prof.model and not P.valid_model_id(prof.model):
                prof.model, prof.context = "", 0   # never hand a control sequence to Claude Code's env
            prof.base_url = clean_base_url(prof.base_url)
            state.profiles[pid] = prof
        recent = raw.get("recent")
        state.recent = [r for r in (recent if isinstance(recent, list) else [])
                        if isinstance(r, dict) and P.valid_model_id(r.get("model"))]
        try:
            state.launches = int(raw.get("launches") or 0)
        except (TypeError, ValueError, OverflowError):     # a list, "many", Infinity
            state.launches = 0
        prefs = raw.get("prefs")
        state.prefs = dict(prefs) if isinstance(prefs, dict) else {}
    else:
        migrate_v1(state)
    if state.active not in state.profiles:
        state.active = next(iter(state.profiles), "")
    return state


def save(state: State) -> None:
    ensure_home()
    write_private(config_path(), json.dumps(state.to_json(), indent=2) + "\n")


# ---- keys -------------------------------------------------------------------

def load_keys() -> Dict[str, str]:
    raw = _read_json(keys_path())
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def get_key(profile_id: str) -> str:
    return load_keys().get(profile_id, "")


def set_key(profile_id: str, key: str) -> None:
    keys = load_keys()
    if key:
        keys[profile_id] = key
    else:
        keys.pop(profile_id, None)
    ensure_home()
    write_private(keys_path(), json.dumps(keys, indent=2) + "\n")


# ---- v1 migration -----------------------------------------------------------

V1_KEY_FILES = ("nim.key", "nim.key.migrated")


def _say(line: str) -> None:
    """One line for the person running Pulsar (stderr: stdout may be JSON or a protocol)."""
    try:
        sys.stderr.write(P.clean_text(line, 400) + "\n")
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass


def nim_base_override(environ: Optional[Dict[str, str]] = None) -> str:
    """NVIDIA_NIM_API_BASE as a base URL for the nvidia profile, or "".

    v1 read it to send NVIDIA NIM traffic to an enterprise NIM or a self-hosted OpenAI-compatible server.
    "" when unset, not an http(s) URL, or NVIDIA's own endpoint; credentials, a ?query and a #fragment
    are never kept.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("NVIDIA_NIM_API_BASE") or "").strip()
    if not raw or P.clean_text(raw, 2048) != raw or re.search(r"\s", raw):
        return ""
    try:
        split = urllib.parse.urlsplit(raw)
        host = split.hostname
    except ValueError:
        return ""
    if split.scheme.lower() not in ("http", "https") or not host:
        return ""
    netloc = split.netloc.rsplit("@", 1)[-1]
    base = urllib.parse.urlunsplit((split.scheme.lower(), netloc, split.path, "", "")).rstrip("/")
    default = P.BY_ID["nvidia"].base_url.rstrip("/")
    return "" if base.lower() == default.lower() else base


def _retire_v1_key(h: Path) -> bool:
    """nim.key becomes nim.key.migrated (0600): keys.json holds the key now, and the old file must not be
    imported again when config.json is lost (after a logout it would bring a forgotten key back)."""
    src, dst = h / "nim.key", h / "nim.key.migrated"
    try:
        os.replace(str(src), str(dst))
    except OSError:
        return False
    try:
        os.chmod(str(dst), 0o600)
    except OSError:
        pass
    return True


def forget_v1_key() -> List[str]:
    """Remove the v1 key files (`pulsarcode logout nvidia`). Returns the names removed."""
    gone = []
    for name in V1_KEY_FILES:
        try:
            (home() / name).unlink()
        except OSError:
            continue
        gone.append(name)
    return gone


def _legacy_alias_map() -> Dict[str, str]:
    """v1 aliases (nim-...) back to upstream NIM ids."""
    from pulsar.catalog import NIM_SEED
    out = {"nim-kimi": "moonshotai/kimi-k2.6"}
    for upstream in NIM_SEED:
        provider, model = upstream.split("/", 1)
        compact = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
        alias = "nim-" + compact if compact.startswith("kimi-k2") else \
            "nim-" + provider.replace(".", "-") + "-" + compact
        out.setdefault(alias, upstream)
    return out


def migrate_v1(state: State) -> bool:
    h = home()
    key_file = h / "nim.key"
    if not key_file.exists():
        return False
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not key:
        return False
    aliases = _legacy_alias_map()
    model = "moonshotai/kimi-k2.6"
    try:
        alias = (h / "active_model").read_text(encoding="utf-8").strip()
        model = aliases.get(alias, model)
    except OSError:
        pass
    base = nim_base_override()
    state.upsert(Profile(id="nvidia", provider="nvidia", model=model, base_url=base))
    try:
        history = (h / "model_history").read_text(encoding="utf-8").split()
    except OSError:
        history = []
    for alias in reversed(history):
        if alias in aliases:
            state.push_recent("nvidia", aliases[alias])
    state.push_recent("nvidia", model)
    if "nvidia" not in load_keys():
        set_key("nvidia", key)
    save(state)
    retired = _retire_v1_key(h)
    _say("pulsarcode: moved your v1 NVIDIA NIM setup into Pulsar Code 3 (model %s%s)%s." % (
        model, "; NVIDIA_NIM_API_BASE %s is its base URL" % base if base else "",
        "; the v1 key file is kept as nim.key.migrated" if retired else ""))
    return True


def now() -> float:
    return time.time()
