# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""pulsarcode command line."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pulsar import REPO, ROOT, TAGLINE, __version__
from pulsar import catalog as C
from pulsar import config as K
from pulsar import fx
from pulsar import launch as L
from pulsar import local as LOC
from pulsar import providers as P
from pulsar import screens as S
from pulsar.fx import ASH, BAD, BEAM, DUST, ICE, OK, WARN

SUBCOMMANDS = {
    "key", "login", "connect", "api", "/api", "--api", "pick", "select", "model", "models", "sonar", "status",
    "doctor", "providers", "use", "logout", "update", "upgrade", "uninstall", "demo", "version",
    "--version", "-V", "help", "--help", "-h", "stop", "core", "mcp", "switch", "relay", "top", "stage", "pilot", "pilot-overlay",
    "statusline", "notify",
}

# Subcommand words of v1 and of earlier builds. They are refused with a pointer to what replaced them, so
# an old habit never starts Claude Code with the word as its first prompt, on whichever provider is active.
_PICK_HINT = "choose the model with pulsarcode pick, or pulsarcode model <id>"
RETIRED = {
    "local": "connect your server once with pulsarcode key http://127.0.0.1:<port>/v1, or set PULSARCODE_LOCAL_ENDPOINT",
    "nim": "pulsarcode use nvidia (pulsarcode key connects NVIDIA NIM)",
    "nvidia": "pulsarcode use nvidia (pulsarcode key connects NVIDIA NIM)",
    "kimi": _PICK_HINT,
    "gemma": _PICK_HINT,
    "qwen": _PICK_HINT,
    "sonar-pick": "pulsarcode pick",
}

HELP = """\
{mark} PULSAR CODE  v{ver}   {tag}

  Run the official Claude Code CLI on any model you hold a key for.

  pulsarcode                 boot, connect if needed, launch Claude Code through Pulsar Core
  pulsarcode [claude args]   anything else is passed to Claude Code
                             (e.g. pulsarcode -p "explain this repo", --continue)

 CONNECT
  pulsarcode key             connect a provider: paste any API key, URL or `ollama` (masked prompt)
  pulsarcode key --stdin     read the key from stdin:  printf %s "$KEY" | pulsarcode key --stdin
  pulsarcode key --from-env VAR   use the key held in $VAR;  pulsarcode key https://host/v1  then paste
  pulsarcode providers       the providers you can connect, and the ones you have connected
  pulsarcode use <provider>  switch the active provider profile
  pulsarcode logout [prov]   forget a stored key

 MODELS
  pulsarcode pick            choose a model across every connected provider
  pulsarcode switch <model>  hot-swap a running session (this one, the only one, or --all / --session <id>)
  pulsarcode model [id]      show or set the default model
  pulsarcode models [query]  list models (--json, --refresh, --top N)
  pulsarcode relay [auto|off|p:model ...]   failover chain when a provider is down or rate-limited

 SEE
  pulsarcode top             Observatory: live sessions, tokens/s, latency, relays
  pulsarcode stage           Pulsar Stage viewer for what the model presents (--web for the browser)
  pulsarcode status          health check (alias: doctor)
  pulsarcode demo            replay the boot sequence

 CONTROL
  pulsarcode pilot on|off|status|tour   computer use with the pulsar cursor (off by default)
  pulsarcode stop [--force]  stop Pulsar Core once its sessions end (--force: now)
  pulsarcode update [ref]    install the latest release, or a tag (v3.0.0) or `main` you name
                             (an older release only with --allow-downgrade)
  pulsarcode uninstall       remove Pulsar Code (keeps Claude Code)

 INSIDE A SESSION
  /pulsar:status  /pulsar:switch <model>  /pulsar:stage [topic]  /pulsar:council <question>

  During boot press M for the model picker, K to change key.
  Env: PULSAR_FX=off|lite, PULSAR_HAPTICS=bell|off, PULSAR_NOTIFY=off|bell|os|loud, NO_COLOR=1,
       PULSAR_LOG=1, PULSAR_CLAUDE_CONFIG=shared, PULSAR_RELAY=off, PULSAR_PLUGIN=off,
       PULSAR_ASCII=1, PULSAR_KEEP_ENV_KEYS=1 (let Claude Code see provider keys from your shell),
       PULSAR_ALLOW_HTTP=1 (allow sending a key over plain http to a non-local host),
       PULSARCODE_LOCAL_ENDPOINT=<url> (your own model server, used while it answers; 0 turns it off).

  Keys live in ~/.pulsarcode/keys.json (0600) and only ever go to the
  provider they belong to; never type a key as a command argument.
  Inside a Claude Code session, key, logout, update, uninstall, pilot on, switching other
  sessions and nested launches with permission flags refuse to run: do those in your own
  terminal. A key passed as a prompt is refused. No telemetry. AGPL-3.0. https://github.com/{repo}
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ui() -> S.UI:
    return S.UI()


def _err(ui: S.UI, msg: str) -> None:
    ui.out.write(ui.ink.paint("pulsarcode: ", BAD, True) + msg + "\n")


# ---------------------------------------------------------------------------
# terminal output that never crashes and never lets a provider rewrite your terminal
# ---------------------------------------------------------------------------

_BRAND_GLYPHS = "\u25c9\u2713\u2717\u25cf\u25cb\u2192\u00b7\u2022\u2501"


def _can_encode(stream, text: str) -> bool:
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def harden_stdio() -> None:
    """A cp1252 / ASCII stdout (Windows pipes, legacy locales) prints '?' instead of crashing."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or _can_encode(stream, _BRAND_GLYPHS):
            continue
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def unicode_ok(stream=None) -> bool:
    """Whether plain CLI output may use Unicode: the same decision the TUIs make (term.detect_unicode:
    PULSAR_ASCII, TERM=linux and other ASCII consoles, LANG=C), then whether the stream can encode it."""
    from pulsar.term import detect_unicode
    s = stream if stream is not None else sys.stdout
    enc = getattr(s, "encoding", None) or ""
    if not detect_unicode(os.environ, enc):
        return False
    return _can_encode(s, _BRAND_GLYPHS)


def glyph(uni: str, plain: str, stream=None) -> str:
    """`uni` when the stream can show it, else the ASCII fallback."""
    from pulsar.term import detect_unicode
    s = stream if stream is not None else sys.stdout
    if not detect_unicode(os.environ, getattr(s, "encoding", None) or ""):
        return plain                         # PULSAR_ASCII=1, TERM=linux, LANG=C (Python's own coercion aside)
    return uni if _can_encode(s, uni) else plain


_PLAIN_FOLD = {"\u2022": "*", "\u2026": "...", "\u00b7": "-", "\u2192": "->", "\u2713": "+", "\u2717": "x",
               "\u25cf": "*", "\u25cb": "o", "\u25c9": "@", "\u2014": "-", "\u2013": "-"}


def _plain(text: str, stream=None) -> str:
    """`text` as is when Unicode is fine, else with Pulsar's own symbols (a masked key's dots, ellipses)
    spelled in ASCII. Anything else non-ASCII becomes '?'."""
    if all(ord(c) < 128 for c in text) or unicode_ok(stream):
        return text
    return "".join(_PLAIN_FOLD.get(c, c if ord(c) < 128 else "?") for c in text)


def _ok_mark(ui: S.UI) -> str:
    return ui.ink.paint(("\u2713 " if ui.u else "+ "), OK, True)


def _arrow(stream=None) -> str:
    return glyph(" \u2192 ", " -> ", stream)


def safe(text: object, limit: int = 200) -> str:
    """Strip control characters from anything that came from a provider, a file or a directory name."""
    return P.clean_text(text, limit)


# ---------------------------------------------------------------------------
# commands a model must never run on your behalf
# ---------------------------------------------------------------------------

SESSION_VARS = ("PULSAR_SESSION", "CLAUDECODE", "PULSAR_SESSION_TOKEN")
HUMAN_PHRASE = "i am human"


def claude_session_var(environ=None) -> str:
    """The variable proving we run inside a Claude Code session (its Bash tool, a hook), or ""."""
    env = os.environ if environ is None else environ
    for var in SESSION_VARS:
        if env.get(var):
            return var
    return ""


def _stdio_is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def human_gate(action: str) -> Optional[int]:
    """None when `action` may proceed; otherwise the exit code after refusing.

    Inside a Claude Code session the model's Bash tool (and any allow rule such as
    `Bash(pulsarcode:*)`) could run security-sensitive commands. They only proceed when a
    human types the confirmation phrase on a real terminal; a model's Bash tool has no TTY.
    A command with a Claude Code process among its ancestors (launch.claude_ancestor) is refused
    outright, phrase or not: a shell there can unset the session variables and fake a terminal.
    Both are defence in depth; the boundary is Claude Code's own permission prompt before it runs
    a command.
    """
    var = claude_session_var()
    tty = _stdio_is_tty()
    if (tty or not var) and L.claude_ancestor():
        print("pulsarcode: refusing to %s: this command was started by Claude Code, where the model's Bash tool "
              "could run it. Run it yourself in your own terminal, outside Claude Code." % action, file=sys.stderr)
        return 3
    if not var:
        return None
    msg = ("refusing to %s from inside a Claude Code session (%s is set): the model's Bash tool could run "
           "this. Run it yourself in your own terminal, outside Claude Code." % (action, var))
    if tty:
        sys.stderr.write("pulsarcode: you are inside a Claude Code session (%s is set).\n"
                         "  To %s anyway, type: I am human\n" % (var, action))
        try:
            answer = input("  > ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower().rstrip(".!") == HUMAN_PHRASE:
            return None
    print("pulsarcode: " + msg, file=sys.stderr)
    return 3


def key_for(state: K.State, profile_id: str) -> str:
    key = K.get_key(profile_id)
    if key:
        return key
    prov = P.get(state.profiles[profile_id].provider) if profile_id in state.profiles else P.get(profile_id)
    if prov is not None:
        for var in prov.env:
            if os.environ.get(var):
                return os.environ[var].strip()
    return ""


def adopt_env(state: K.State, announce: bool = False) -> bool:
    """First run with no profile but a key in the environment: adopt it silently.

    An NVIDIA key keeps what v1 made of NVIDIA_NIM_API_BASE (an enterprise NIM or a self-hosted server):
    that base URL goes into the profile, and `announce` (the commands that save the profile) says so in
    one line.
    """
    if state.profiles:
        return False
    found = P.env_keys(dict(os.environ))
    if not found:
        return False
    prov = found[0][0]
    base = K.nim_base_override() if prov.id == "nvidia" else ""
    state.upsert(K.Profile(id=prov.id, provider=prov.id, base_url=base))
    if base and announce:
        print("pulsarcode: NVIDIA NIM goes to %s, from NVIDIA_NIM_API_BASE (saved in the nvidia profile)"
              % safe(base, 200), file=sys.stderr)
    return True


def _strip_userinfo(url: str) -> str:
    """Never persist user:password, a token@ or a ?query/#fragment from a pasted URL (config.json, labels, logs)."""
    out = re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/?#@]*@", r"\1", url or "")
    return re.split(r"[?#]", out, maxsplit=1)[0]


def _keep_base_url(prov: P.Provider, base_url: str) -> bool:
    """Whether a detected base URL must be persisted in the profile.

    Custom and local endpoints always; providers that need a URL (Azure, Foundry), cloud routes (the
    Vertex project/region and the Bedrock region are encoded in it) and any registry provider whose
    detected base differs from its default (a region, a gateway).
    """
    if not base_url:
        return False
    if prov.id in ("custom", "ollama", "lmstudio") or prov.needs_url or prov.route == "cloud":
        return True
    default = P.BY_ID.get(prov.id)
    return default is None or base_url.rstrip("/") != (default.base_url or "").rstrip("/")


def placeholder_problem(prov: P.Provider) -> str:
    """Why `prov` must not be contacted: a provider that needs your own URL still points at the registry
    placeholder (https://YOUR-RESOURCE...). "" when it is fine. One test for every caller:
    catalog.placeholder_problem (also used by the catalog fetch and the MCP switch)."""
    return C.placeholder_problem(prov)


def save_link(state: K.State, link: S.Link) -> K.Profile:
    prov = link.provider
    base_url = _strip_userinfo(link.base_url)
    prof = K.Profile(id=link.profile_id, provider=prov.id if prov.id != "custom" else "custom",
                     base_url=base_url if _keep_base_url(prov, base_url) else "",
                     native=prov.route == "native" and prov.id == "custom",
                     needs_key=prov.needs_key)      # a server that takes no key stays keyless on every launch
    state.upsert(prof)
    if link.key or not prov.needs_key:
        K.set_key(link.profile_id, link.key)
    K.save(state)
    return prof


def local_endpoint_profile(state: K.State) -> Optional[K.Profile]:
    """The custom profile for the endpoint PULSARCODE_LOCAL_ENDPOINT names (saved like `pulsarcode key <URL>`
    with no key would save it), or None when unset or turned off. No network: the URL is only parsed.

    An OpenAI-compatible base URL, or an Anthropic Messages one when it ends in /anthropic. A key stored
    for the same profile with `pulsarcode key <URL>` is kept and used.
    """
    raw = LOC.configured_endpoint()
    if not raw:
        return None
    base = LOC.endpoint_base(raw)
    if not base:
        print("pulsarcode: PULSARCODE_LOCAL_ENDPOINT is not an http(s) URL (for example http://127.0.0.1:8000/v1); "
              "ignored.", file=sys.stderr)
        return None
    native = base.endswith("/anthropic")
    pid = S.profile_id_for(P.custom_provider(base, native=native, needs_key=False), base)
    prof = state.profiles.get(pid)
    if prof is None or (prof.provider, prof.base_url, prof.native) != ("custom", base, native):
        prof = state.upsert(K.Profile(id=pid, provider="custom", base_url=base, native=native,
                                      needs_key=prof.needs_key if prof is not None else False), activate=False)
        K.save(state)
    return prof


def prefer_local_endpoint(state: K.State) -> None:
    """Run this launch on the configured local endpoint while it answers, without making it the saved
    default (State.override): when it stops answering, launches go back to your provider by themselves.
    PULSARCODE_PREFER_LOCAL=0 keeps the saved provider; with nothing configured nothing is asked."""
    prof = local_endpoint_profile(state)
    if prof is None or not LOC.prefer_local():
        return
    if LOC.endpoint_answers(prof.base_url):
        state.override = prof.id
        return
    cur = state.current()
    instead = "; this launch uses %s instead" % cur.label if cur is not None and cur.id != prof.id else ""
    print("pulsarcode: %s (PULSARCODE_LOCAL_ENDPOINT) does not answer%s." % (safe(prof.base_url, 200), instead),
          file=sys.stderr)


def offer_to_forget_v1_key(state: K.State) -> None:
    """Once, on a terminal: remove the v1 NVIDIA key file the v1 import kept (nim.key.migrated) now that
    keys.json holds the NVIDIA key? Enter means yes. Either answer is remembered (prefs["v1_key_asked"]);
    `pulsarcode logout nvidia` removes the file in any case. Nothing is asked while keys.json has no NVIDIA
    key: the file is then the only copy."""
    if state.prefs.get("v1_key_asked") or not K.get_key("nvidia"):
        return
    left = [name for name in K.V1_KEY_FILES if (K.home() / name).is_file()]
    if not left:
        return
    try:
        answer = input("pulsarcode: keys.json holds your NVIDIA key now. Remove the v1 copy of it in %s (%s)? "
                       "[Y/n] " % (safe(K.home(), 200), ", ".join(left)))
    except EOFError:
        print("")
        return                                     # no answer: asked again at the next launch
    state.prefs["v1_key_asked"] = True
    if answer.strip().lower() in ("", "y", "yes"):
        gone = K.forget_v1_key()
        print("pulsarcode: removed %s" % ", ".join(gone) if gone else "pulsarcode: nothing to remove")
    else:
        print("pulsarcode: kept it; `pulsarcode logout nvidia` removes it with the key")
    K.save(state)


def sources_for(state: K.State) -> List[S.Source]:
    out = []
    for pid, prof in state.profiles.items():
        try:
            prov = prof.resolve()
        except KeyError:
            continue
        out.append(S.Source(pid, prov, prof.label, key_for(state, pid)))
    return out


def profile_problem(state: K.State, profile_id: str) -> str:
    """Why profile `profile_id` cannot be used (placeholder_problem: no resource URL saved), or ""."""
    prof = state.profiles.get(profile_id)
    if prof is None:
        return ""
    try:
        return placeholder_problem(prof.resolve())
    except KeyError:
        return ""


def run_picker(ui: S.UI, state: K.State, preloaded: Optional[dict] = None,
               refused: Optional[List[str]] = None) -> Optional[Tuple[str, str, int]]:
    """Picker loop (with add-provider detour). Returns (profile, model, ctx) or None.

    refused, when given, receives the reason when the choice was refused (a profile that cannot be used)."""
    while True:
        srcs = sources_for(state)
        if preloaded:
            for s in srcs:
                if s.profile_id in preloaded:
                    s.result = preloaded[s.profile_id]
        prof = state.current()
        current = (prof.id, prof.model) if prof and prof.model else None
        recent = [(r["profile"], r["model"]) for r in state.recent if r.get("profile") in state.profiles]
        choice = S.picker(ui, srcs, current, recent, prof.id if prof else state.active)
        if choice == "uplink":
            link = S.uplink(ui)
            if link is not None:
                save_link(state, link)
                if link.models:
                    preloaded = dict(preloaded or {})
                    preloaded[link.profile_id] = C.CatalogResult(link.models, "live")
            continue
        if choice is None:
            return None
        if not P.valid_model_id(choice.model):
            # Defense in depth: the pickers only offer valid ids. config.load would drop this one silently
            # and the next launch would use another model.
            _err(ui, "%r is not a model id (%s). Nothing was changed." % (safe(choice.model, 60), P.MODEL_ID_RULE))
            return None
        why = profile_problem(state, choice.profile_id)
        if why:
            # Defense in depth too: the picker offers nothing on such a profile. Never promise a launch that
            # would only be refused.
            _err(ui, why)
            if refused is not None:
                refused.append(why)
            return None
        ctx = choice.context or C.guess_context(choice.model)
        state.set_model(choice.profile_id, choice.model, ctx)
        K.save(state)
        return choice.profile_id, choice.model, ctx


def ensure_model(state: K.State, prof: K.Profile, catalog: Optional[C.CatalogResult]) -> None:
    if prof.model:
        return
    prov = prof.resolve()
    models = catalog.models if catalog else []
    prof.model = C.recommended(models, prov) if models or prov.fallback_model else ""
    if prof.model:
        m = next((x for x in models if x.id == prof.model), None)
        prof.context = (m.context if m else 0) or C.guess_context(prof.model)
        state.push_recent(prof.id, prof.model)
    K.save(state)


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

def is_print_mode(args: List[str]) -> bool:
    return any(a in ("-p", "--print") or a.startswith("--output-format") for a in args)


def picker_options(state: K.State, catalogs: dict) -> List[dict]:
    """Rows for Claude Code's /model picker: current, recents, then each provider's picks."""
    rows: List[dict] = []

    def models_for(pid: str) -> List[C.Model]:
        res = catalogs.get(pid)
        return list(res.models) if res else (C.read_cache(pid, ttl=10 * 365 * 86400) or [])

    def add(pid: str, model: str) -> None:
        prof = state.profiles.get(pid)
        if prof is None or not model:
            return
        m = next((x for x in models_for(pid) if x.id == model), None)
        rows.append({"model": model, "provider": prof.label, "context": (m.context if m else 0) or prof.context,
                     "tags": list(m.tags) if m else []})

    cur = state.current()
    if cur is not None:
        add(cur.id, cur.model)
    for r in state.recent:
        add(r.get("profile", ""), r.get("model", ""))
    for pid, prof in state.profiles.items():
        try:
            prov = prof.resolve()
        except KeyError:
            continue
        for m in C.picks(models_for(pid), prov, limit=4):
            add(pid, m.id)
    return rows


# Claude Code flags a nested session may carry without a human confirming it: output shape, limits and
# the model. Everything else (--dangerously-skip-permissions, --permission-mode, --allowedTools,
# --settings, --mcp-config, --plugin-dir, --add-dir, --continue, ...) could widen what the nested
# session may do, so inside a Claude Code session it needs `I am human` typed on a real terminal.
NESTED_SAFE_FLAGS = frozenset((
    "-p", "--print", "--output-format", "--input-format", "--verbose", "--max-turns", "--model",
    "--fallback-model", "--disallowedTools", "--disallowed-tools", "--include-partial-messages",
    "--no-session-persistence", "--append-system-prompt", "--system-prompt", "-h", "--help", "-v", "--version",
))


def nested_risky_flags(args: List[str]) -> List[str]:
    """Flags in `args` (names only, each once) that are not in NESTED_SAFE_FLAGS."""
    out: List[str] = []
    for a in args:
        if len(a) < 2 or not a.startswith("-"):
            continue
        name = a.split("=", 1)[0]
        if name not in NESTED_SAFE_FLAGS and name not in out:
            out.append(name)
    return out


def launch_secret(args: List[str]) -> Optional[P.Detection]:
    """The detection of a key found among Claude Code arguments (they become the model's prompt), else None.

    A whole argument that is a key (`pulsarcode sk-or-v1-...`, `pulsarcode -- key gsk_...`, VAR=key) or a
    credential Pulsar rejects, and any long word with a vendor prefix inside a longer prompt.
    """
    for a in args:
        value = a.split("=", 1)[1] if a.startswith("--") and "=" in a else a
        if not value or value.startswith("-"):
            continue
        det = P.detect(value)
        if det.kind == "rejected" and len(value) >= 16:
            return det
        if det.kind == "key" and (P.looks_like_key(det.key, min_len=20) or det.note.startswith("from ")):
            return det
        for word in value.split():
            word = word.strip("\"'`.,;:()[]{}<>")
            if len(word) >= 24 and P.looks_like_key(word):
                return P.detect(word)
    return None


def cmd_launch(args: List[str], force: str = "") -> int:
    ui = _ui()
    leaked = launch_secret(args)
    if leaked is not None:
        owner = " (%s)" % leaked.best.name if leaked.best is not None and leaked.kind == "key" else ""
        _err(ui, "refusing to start: an argument looks like an API key%s. Claude Code would send it to the model "
                 "as your prompt and keep it in the transcript. Nothing was sent.\n" % owner
             + _argv_secret_help("", leaked))
        return 2
    risky = nested_risky_flags(args)
    if risky:
        # Inside a session, or in a process Claude Code started (human_gate tells): never without a person.
        gate = human_gate("start a nested Claude Code session with %s" % " ".join(risky[:4]))
        if gate is not None:
            return gate
    K.ensure_home()        # a PULSAR_HOME that is someone else's folder is refused before anything is written
    state = K.load()
    headless = is_print_mode(args) or not ui.caps.tty
    first_run = not state.profiles
    if adopt_env(state, announce=True):
        K.save(state)
    prefer_local_endpoint(state)
    if not headless and _stdio_is_tty() and not claude_session_var():
        offer_to_forget_v1_key(state)          # once, before the boot screen takes the terminal

    claude = {"bin": L.find_claude(), "ver": ""}
    catalogs: dict = {}
    core_box: dict = {}
    profile_box: dict = {}

    def t_claude() -> Tuple[str, str]:
        if not claude["bin"]:
            return "fail", "not installed"
        claude["ver"] = L.claude_version(claude["bin"])
        return "ok", claude["ver"] or "ready"

    def t_profile() -> Tuple[str, str]:
        d = L.claude_dir()
        L.seed_claude_config(d)
        profile_box["kept"] = L.install_commands(d)          # said after the boot screen (legacy_commands_note)
        from pulsar import plugin
        plugin.build_plugin()
        return "ok", ("isolated" if isolated_profile(d) else "shared ~/.claude") + " · plugin ready"

    def t_uplink() -> Tuple[str, str]:
        prof = state.current()
        if prof is None:
            return "warn", "no provider yet"
        key = key_for(state, prof.id)
        prov = prof.resolve()
        if prov.needs_key and not key:
            return "warn", prof.label + " · no key"
        extra = len(L.relay_plan(state, prof, lambda pid: key_for(state, pid)))     # the relays it will have
        return "ok", prof.label + ("  " + P.mask(key) if key else "") + ("  +%d relay" % extra if extra else "")

    def t_catalog() -> Tuple[str, str]:
        prof = state.current()
        if prof is None:
            return "warn", "waiting for uplink"
        prov = prof.resolve()
        if placeholder_problem(prov):
            # Azure/Foundry without a resource URL: the key must not go to the placeholder host, not even
            # for a model list (the launch refuses it below and says "Nothing was sent").
            return "warn", "skipped · no resource URL"
        res = C.load_catalog(prov, prof.id, key_for(state, prof.id), timeout=6)
        catalogs[prof.id] = res
        if res.error and res.source == "seed":
            return "warn", "offline · " + res.error[:40]
        return "ok", "%d models · %s" % (len(res.models), res.source)

    def usable() -> bool:
        prof = state.current()
        if prof is None:
            return False
        try:
            prov = prof.resolve()
        except KeyError:
            return False
        return not (prov.needs_key and not key_for(state, prof.id)) and not placeholder_problem(prov)

    def t_core() -> Tuple[str, str]:
        if not usable():
            # Nothing to route yet: igniting Core now would only leave a daemon behind.
            return "warn", "waits for a provider"
        from pulsar.core import spawn
        warm = spawn.attach()
        info = warm or spawn.ensure_core()
        core_box["info"] = info
        return "ok", ":%d · %s · token-gated" % (info.port, "warm" if warm else "ignited")

    tasks = [S.Task("Claude Code", t_claude), S.Task("Plugin + profile", t_profile),
             S.Task("Uplink", t_uplink), S.Task("Model catalog", t_catalog), S.Task("Pulsar Core", t_core)]

    if headless:
        for t in tasks:
            t.start()
        for t in tasks:
            if t.thread:
                t.thread.join()
        intent = "go"
    else:
        quick = state.launches > 0 and not first_run
        intent = force or S.boot(ui, tasks, quick=quick)
        if intent == "quit":
            return 130
    legacy_commands_note(state, profile_box.get("kept") or [])

    if not claude["bin"]:
        _claude_missing(ui)
        return 127

    cur = state.current()
    need_link = cur is None or (cur.resolve().needs_key and not key_for(state, cur.id))
    if need_link or intent == "key":
        if headless:
            _err(ui, "no provider connected. Run `pulsarcode key` in a terminal, or export a key "
                     "(NVIDIA_NIM_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, ...).")
            return 2
        link = S.uplink(ui, reason="" if need_link else "Paste a new key to switch or rotate.")
        if link is None:
            if need_link:
                return 1
        else:
            save_link(state, link)
            if link.models:
                catalogs[link.profile_id] = C.CatalogResult(link.models, "live")
            intent = "pick"
    prof = state.current()
    assert prof is not None

    if not headless and (intent == "pick" or first_run or not prof.model):
        picked = run_picker(ui, state, catalogs)
        prof = state.current()
        assert prof is not None
        if picked is None and not prof.model:
            ensure_model(state, prof, catalogs.get(prof.id))
    else:
        ensure_model(state, prof, catalogs.get(prof.id))

    prov = prof.resolve()
    key = key_for(state, prof.id)
    cloud = prov.route == "cloud"
    direct = cloud or (os.environ.get("PULSAR_DIRECT") == "1" and prov.route == "native")
    options = P.cloud_options(prov, prof.base_url) if cloud else {}
    if cloud:
        missing = P.cloud_missing(prov, options)
        if "project" in missing:
            _err(ui, "%s needs a Google Cloud project: run `pulsarcode key \"vertex <project-id> [region]\"`." % prov.name)
            return 2
    why = placeholder_problem(prov) or ("" if cloud else L.transport_problem(prov, key))
    if why:
        _err(ui, why)
        return 2
    core = sess = None
    token = ""
    route = "cloud" if cloud else "direct" if direct else "core"
    try:
        core, sess, token = L.start_session(state, prof, lambda pid: key_for(state, pid),
                                            label=safe(os.path.basename(os.getcwd()), 60), route=route)
    except RuntimeError as exc:
        if not direct:
            _err(ui, str(exc))
            return 1
        core = sess = None          # direct and cloud routes run without the side channel
    if direct and prov.id == "anthropic":
        L.seed_claude_config(L.claude_dir(), approve_key=key)
    elif route == "core":
        L.acknowledge_gateway_notice(L.claude_dir())     # elsewhere it may concern your own bill: left to you

    state.launches += 1
    K.save(state)
    try:
        env = L.build_env(prov, key, prof.model, prof.context, core, sess, token, options=options, route=route,
                          secrets=[key_for(state, pid) for pid in state.profiles if pid != prof.id])
    except ValueError as exc:
        _err(ui, str(exc))
        L.release_session(sess)
        return 2
    if is_print_mode(args) and not (os.environ.get("PULSAR_STAGE_AUTO") or "").strip():
        env["PULSAR_STAGE_AUTO"] = "0"      # a scripted -p run never opens a Stage viewer by itself
    if L.pilot_ignored(launched=env):
        _err(ui, "PULSAR_PILOT=1 was ignored: it only counts when you start pulsarcode yourself on a terminal, "
                 "outside Claude Code (the model's Bash tool could have set it). "
                 "Turn Pilot on with `pulsarcode pilot on` in your own terminal.")
    from pulsar import plugin
    model_label = prof.model or "Claude default"
    cfg = L.claude_dir()
    extra = plugin.claude_args(model_label, picker_options(state, catalogs),
                               anthropic=prov.id == "anthropic" or cloud,
                               claude_config=cfg, isolated=isolated_profile(cfg),
                               claude_version=claude["ver"], session_id=sess.sid if sess is not None else "")
    argv = extra + args
    try:
        L.claude_argv(claude["bin"], argv)       # Windows: an npm claude.cmd must not see cmd metacharacters
    except ValueError as exc:
        _err(ui, str(exc))
        L.release_session(sess)
        return 2
    # Decide the pins (and any refusal) before WARP announces the session: a refused launch must not look like
    # a started one. exec_claude applies this decision (no second check, no warning printed twice).
    try:
        pins = L.check_pins(argv, env)
    except L.LaunchRefused as exc:
        _err(ui, str(exc))
        L.release_session(sess)
        return 2
    except Exception as exc:                     # never start a Pulsar session unpinned
        _err(ui, "could not pin this session's settings (%s). Claude Code was not started." % type(exc).__name__)
        L.release_session(sess)
        return 1
    if not headless:
        ctx = S._fmt_ctx(prof.context)
        relay = [t for t in (sess.plan[1:] if sess and not direct else [])]
        arrow = _arrow(ui.out)
        if direct:
            where = env.get("AWS_REGION") or env.get("CLOUD_ML_REGION") or ""
            route_line = "direct to %s%s%s" % (prov.name if cloud else (prov.native_base() or "api.anthropic.com"),
                                               "  ·  " + where if where else "",
                                               "  ·  Core side channel %s" % core.url if core else "")
        else:
            assert core is not None and sess is not None
            route_line = "Pulsar Core %s  ·  session %s  ·  relay %s" % (core.url, sess.sid,
                                                                       arrow.join(safe(t.model) for t in relay) or "off")
        lines = [("%s  ·  %s%s" % (prov.name if prov.id != "custom" else prof.label, safe(model_label),
                                   "  ·  " + ctx + " ctx" if ctx else ""), ICE),
                 (route_line, DUST),
                 ("/pulsar:status  ·  /pulsar:stage  ·  pulsarcode top  ·  pulsarcode switch <model>", DUST)]
        S.warp(ui, lines)
    return L.exec_claude(claude["bin"], argv, env, pins=pins)


def legacy_commands_note(state: K.State, kept: List[str]) -> None:
    """One line naming the command files an earlier Pulsar Code wrote into your own Claude profile (shared mode)
    that launch.install_commands left in place because you changed them. Said once for each set of files."""
    names = sorted(kept)
    told = state.prefs.get("kept_commands")
    if names == (sorted(told) if isinstance(told, list) else []):
        return
    if names:
        print("pulsarcode: kept your changed copies of earlier Pulsar Code commands in %s: %s (Pulsar Code no "
              "longer uses them)" % (safe(L.claude_dir() / "commands", 300), ", ".join(safe(n, 40) for n in names)),
              file=sys.stderr)
        state.prefs["kept_commands"] = names
    else:
        state.prefs.pop("kept_commands", None)
    K.save(state)


def isolated_profile(directory: Path) -> bool:
    """Claude Code runs on Pulsar's own profile rather than yours: told by the mode (the folder
    launch.claude_dir() chose), never by a name in the path (PULSAR_HOME=/opt/pc is isolated too)."""
    return directory == K.home() / "claude_config"


def _claude_missing(ui: S.UI) -> None:
    windows_claude = L.wsl_windows_claude()
    if windows_claude:
        _err(ui, "Claude Code for Linux is not installed in this WSL distribution.")
        ui.say("  Found the Windows one at %s, but Pulsar does not use it: Windows programs started from WSL"
               % safe(windows_claude, 120))
        ui.say("  do not receive the session's environment (Pulsar Core's URL and token). Install the Linux build"
               " inside WSL:")
        ui.say("    curl -fsSL https://claude.ai/install.sh | bash", BEAM)
        ui.say("  (with npm, use a Linux Node.js: `which npm` must not point into /mnt/c)", DUST)
        return
    _err(ui, "Claude Code is not installed.")
    ui.say("  Install it with the official installer, then run pulsarcode again:")
    if os.name == "nt":
        ui.say("    irm https://claude.ai/install.ps1 | iex", BEAM)
    else:
        ui.say("    curl -fsSL https://claude.ai/install.sh | bash", BEAM)
    ui.say("  or, with Node.js:  npm install -g @anthropic-ai/claude-code", DUST)


# ---------------------------------------------------------------------------
# other commands
# ---------------------------------------------------------------------------

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
# Any userinfo in a URL, `user:password@` or a bare `token@`: both carry a credential.
_USERINFO = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#@\s]*@")
_URL = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://")


def _url_carries_secret(word: str) -> bool:
    """A URL with userinfo (user:pw@ or token@), a ?query or a #fragment: any of them can hold a key."""
    return bool(_URL.match(word)) and (bool(_USERINFO.match(word)) or "?" in word or "#" in word)


def _secret_in_argv(raw: str, det: P.Detection) -> bool:
    """True when the command line itself carries a credential (visible in ps and shell history)."""
    return (bool(det.key) or det.kind == "rejected"
            or any(_url_carries_secret(w) for w in raw.split()))


def _argv_secret_help(raw: str, det: P.Detection) -> str:
    url = raw.split()[0] if det.kind in ("url", "local") and raw.split() else ""
    hint_url = (url and _URL.match(url) and _strip_userinfo(url).rstrip("/")) or "https://host/v1"
    lines = ["never put a key on the command line: other users can read it in the process list and it "
             "stays in your shell history. Rotate it if this machine is shared. Instead:",
             "    pulsarcode key                              (masked prompt)",
             "    printf %s \"$KEY\" | pulsarcode key --stdin",
             "    pulsarcode key --from-env OPENROUTER_API_KEY",
             "    pulsarcode key " + hint_url + "         (then paste the key)"]
    word = raw.strip()
    if _ENV_NAME.fullmatch(word) and P.provider_for_env_var(word) is not None:
        lines.insert(1, "    did you mean:  pulsarcode key --from-env " + word)
    return "\n".join(lines)


def _looks_like_secret(value: str) -> bool:
    """A vendor key shape, a credential Pulsar rejects, or a long random-looking token (never a VAR name)."""
    v = (value or "").strip()
    if P.looks_like_key(v):
        return True
    return len(v) >= 20 and not v.isupper() and re.search(r"[0-9]", v) is not None


def _read_stdin_secret() -> str:
    """One key (or `URL KEY`, `VAR=KEY`) from stdin, or "".

    Raises ValueError for anything that could hold more than one secret (a piped .env file, several
    words): joined together they would be saved as "the key" and sent in an Authorization header.
    The message never repeats what was read.
    """
    try:
        data = sys.stdin.read(65536)
    except (OSError, ValueError, KeyboardInterrupt):
        return ""
    lines = [line.strip() for line in (data or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) > 1:
        raise ValueError("stdin held %d lines; pipe exactly one key on one line (for a .env file: "
                         "pulsarcode key --from-env VAR). Nothing was saved or sent." % len(lines))
    words = lines[0].split()
    first = words[0].lower()
    if (len(words) == 1 or (len(words) == 2 and (_URL.match(words[0]) or first in ("export", "set", "bearer")))
            or (first in ("vertex", "bedrock") and len(words) <= 3)):
        return " ".join(words)
    raise ValueError("stdin held several words; pipe exactly one key (or `URL KEY`). Nothing was saved or sent.")


def _choose_provider(ui: S.UI, det: P.Detection, provider_id: str, can_ask: bool) -> Tuple[Optional[P.Provider], str]:
    """(provider, error). Never probes vendors to find out: the operator decides."""
    options = det.candidates or P.keyed_providers()
    if provider_id:
        for p in options + [x for x in P.PROVIDERS if x.needs_key and not x.needs_url]:
            if p.id == provider_id:
                return p, ""
        named = P.BY_ID.get(provider_id)
        if named is not None and named.needs_url:
            # Azure OpenAI, Azure AI Foundry: the key alone says nothing about where your resource lives.
            return None, ("%s needs your resource URL with the key: printf %%s \"$KEY\" | pulsarcode key %s  "
                          "(or run `pulsarcode key %s` and paste the key at the prompt). Nothing was sent."
                          % (named.name, C.url_example(named), C.url_example(named)))
        lead = ("%s is not connected with a key alone (see `pulsarcode providers`)" % named.name if named is not None
                else "unknown provider %r" % safe(provider_id, 40))
        return None, "%s. %s: %s" % (lead, "This key shape matches" if det.candidates else "Known",
                                     ", ".join(p.id for p in options))
    if det.certain and det.best is not None:
        return det.best, ""
    if not can_ask:
        return None, ("could not tell which provider this key belongs to (%s). Add --provider <id>, one of: %s"
                      % (det.note or "ambiguous shape", ", ".join(p.id for p in options)))
    for i, p in enumerate(options, 1):
        ui.say("  %2d. %s" % (i, p.name))
    try:
        n = int(input("provider number > ").strip())
        if n < 1:
            raise IndexError
        return options[n - 1], ""
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        return None, "no provider chosen"


def cmd_key(args: List[str], auto_stdin: bool = True) -> int:
    """Connect a provider. The key comes from a masked prompt, stdin or an environment variable; never argv."""
    ui = _ui()
    gate = human_gate("connect or change a provider key")
    if gate is not None:
        return gate
    state = K.load()
    use_stdin, env_var, provider_id, words = False, "", "", []
    it = iter(args)
    for a in it:
        if a == "--stdin":
            use_stdin = True
        elif a == "--from-env" or a.startswith("--from-env="):
            env_var = a.split("=", 1)[1] if "=" in a else next(it, "")
            if not env_var:
                _err(ui, "--from-env needs a variable name, e.g. --from-env OPENROUTER_API_KEY")
                return 2
        elif a == "--provider" or a.startswith("--provider="):
            provider_id = a.split("=", 1)[1] if "=" in a else next(it, "")
        else:
            words.append(a)
    for value, what in ((env_var, "variable name"), (provider_id, "provider id")):
        if value and (P.looks_like_key(value) or (not _ENV_NAME.fullmatch(value) and _looks_like_secret(value))):
            _err(ui, "that looks like a key, not a %s.\n" % what + _argv_secret_help("", P.Detection("key")))
            return 2
    if provider_id and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", provider_id):
        _err(ui, "--provider takes a provider id such as openrouter or groq (see `pulsarcode providers`)")
        return 2
    raw = " ".join(words).strip()
    det = P.detect(raw) if raw else P.Detection("empty")
    if raw and _secret_in_argv(raw, det):
        _err(ui, _argv_secret_help(raw, det))
        return 2
    if raw and det.kind not in ("url", "local", "cloud"):
        _err(ui, det.note or "could not read %r; run `pulsarcode key` and paste it at the prompt." % safe(raw, 40))
        return 2

    stdin_tty = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False
    is_url = bool(raw) and det.kind in ("url", "local") and re.match(r"(?i)^https?://", raw) is not None
    secret = ""
    if env_var:
        if not _ENV_NAME.fullmatch(env_var):
            # Never echo it: whatever was typed here may be a key.
            _err(ui, "--from-env takes an environment variable name (letters, digits and _), "
                     "e.g. --from-env OPENROUTER_API_KEY")
            return 2
        secret = os.environ.get(env_var, "").strip()
        if not secret:
            # A random-looking "name" may be a key typed in the wrong place: never echo that one.
            _err(ui, "%s is empty or not set" % ("that variable" if _looks_like_secret(env_var) else "$" + env_var))
            return 2
        if re.search(r"\s", secret):
            _err(ui, "$%s holds several words or lines; it must hold exactly one key. Nothing was saved or sent."
                 % env_var)
            return 2
    elif use_stdin or (auto_stdin and not stdin_tty and (not raw or is_url)):
        try:
            secret = _read_stdin_secret()
        except ValueError as exc:
            _err(ui, str(exc))
            return 2
        stdin_tty = False                    # consumed: nothing left to ask with
        if not secret and not (is_url and raw):
            _err(ui, "no key received on stdin; pipe one key (printf %s \"$KEY\" | pulsarcode key --stdin) "
                     "or run `pulsarcode key` in a terminal. Nothing was changed.")
            return 2
    elif is_url and stdin_tty:
        import getpass
        host = safe(P._host_of(det.base_url or raw.split()[0]), 80)
        try:
            secret = getpass.getpass("key for %s (Enter if it needs none) > " % host).strip()
        except (EOFError, KeyboardInterrupt):
            return 1

    if not raw and not secret:
        if use_stdin or env_var:
            _err(ui, "no key received")
            return 2
        return _key_interactive(ui, state)

    if secret:
        if raw and not is_url:
            _err(ui, "pass either %r or a key, not both" % safe(raw, 40))
            return 2
        if env_var and not raw and P.provider_for_env_var(env_var) is not None:
            text = env_var + "=" + secret            # the variable name tells which provider it is
        else:
            text = (raw.split()[0] + " " + secret) if raw else secret
        det = P.detect(text)
    if det.kind == "rejected":
        _err(ui, det.note)
        return 2
    if det.kind in ("empty", "unknown") and not det.candidates:
        _err(ui, det.note or "nothing usable received")
        return 2
    if det.warning:
        ui.say(ui.ink.paint("! ", WARN, True) + det.warning)
    prov, problem = _choose_provider(ui, det, provider_id, can_ask=stdin_tty)
    if prov is None:
        _err(ui, problem)
        return 2
    link = S.Link(prov, det.key, S.profile_id_for(prov, det.base_url), base_url=det.base_url)
    refusal = placeholder_problem(link.provider) or (L.transport_problem(prov, det.key) if prov.route != "cloud" else "")
    if refusal:
        _err(ui, refusal + " Nothing was sent or saved.")
        return 2
    ok, detail, status = S.probe(link)
    detail = safe(detail, 300)
    if not ok and status in (401, 403):
        _err(ui, "%s rejected the key (%d)" % (prov.name, status))
        return 1
    if not ok and 300 <= status < 400:
        _err(ui, "%s: %s. Not saved; check the URL (https, exact API base)." % (prov.name, detail))
        return 1
    save_link(state, link)
    ui.say(_ok_mark(ui) + S._fold("%s connected  ·  %s" % (prov.name, detail if ok else "unverified: " + detail), ui.u))
    return 0


def _key_interactive(ui: S.UI, state: K.State) -> int:
    link = S.uplink(ui)
    if link is None:
        return 1
    save_link(state, link)
    picked = run_picker(ui, state, {link.profile_id: C.CatalogResult(link.models, "live")} if link.models else None)
    prof = state.current()
    if prof and not prof.model:
        ensure_model(state, prof, None)
    prof = state.current()
    ui.say(_ok_mark(ui) + S._fold("%s  ·  %s  ready. Run %s" % (
        prof.label, safe(prof.model), ui.ink.paint("pulsarcode", BEAM, True)), ui.u) if prof else "saved")
    return 0 if picked or prof else 1


def cmd_pick(args: List[str]) -> int:
    ui = _ui()
    state = K.load()
    adopt_env(state, announce=True)
    local_endpoint_profile(state)
    if not state.profiles:
        return cmd_key([], auto_stdin=False)
    refused: List[str] = []
    res = run_picker(ui, state, refused=refused)
    if refused:
        return 2
    if res is None:
        prof = state.current()
        ui.say("Kept " + (safe(prof.model) if prof and prof.model else "current model") + ".", DUST)
        return 1
    ui.say(_ok_mark(ui) + "next launch uses %s on %s" % (safe(res[1]), state.profiles[res[0]].label))
    return 0


def cmd_model(args: List[str]) -> int:
    ui = _ui()
    state = K.load()
    prof = state.current()
    if not args:
        if prof is None:
            ui.say("No provider connected. Run `pulsarcode key`.")
            return 1
        print(safe(prof.model) or "(Claude Code default)")
        return 0
    target = " ".join(args).strip()
    pid = state.active
    if ":" in target and target.split(":", 1)[0] in state.profiles:
        pid, target = target.split(":", 1)
    if not P.valid_model_id(target):
        print("pulsarcode: %r is not a model id (%s)" % (safe(target, 60), P.MODEL_ID_RULE), file=sys.stderr)
        return 2
    else:
        for other_id in state.profiles:
            res = C.read_cache(other_id, ttl=10 * 365 * 86400) or []
            if any(m.id == target for m in res) and other_id != pid and not any(
                    m.id == target for m in (C.read_cache(pid, ttl=10 * 365 * 86400) or [])):
                pid = other_id
                break
    if pid not in state.profiles:
        _err(ui, "no provider connected. Run `pulsarcode key`.")
        return 1
    known = C.read_cache(pid, ttl=10 * 365 * 86400) or []
    match = next((m for m in known if m.id == target), None)
    if known and match is None:
        sugg = C.search(known, target, {})[:3]
        if sugg and sugg[0].id.lower() == target.lower():
            match = sugg[0]
            target = match.id
        elif sugg:
            ui.say(ui.ink.paint("! ", WARN, True) + "%s is not in the %s catalog. Closest: %s. Saving anyway." % (
                target, state.profiles[pid].label, ", ".join(safe(m.id) for m in sugg)))
    state.set_model(pid, target, (match.context if match else 0) or C.guess_context(target))
    K.save(state)
    print("pulsarcode: default model is now %s on %s" % (target, state.profiles[pid].label))
    return 0


def cmd_models(args: List[str]) -> int:
    state = K.load()
    adopt_env(state)
    local_endpoint_profile(state)
    as_json = "--json" in args
    refresh = "--refresh" in args
    top = 0
    if "--top" in args:
        try:
            top = int(args[args.index("--top") + 1])
        except (IndexError, ValueError):
            top = 25
    query = " ".join(a for i, a in enumerate(args) if not a.startswith("--") and not (
        i > 0 and args[i - 1] == "--top"))
    allm: List[C.Model] = []
    errors = []
    for pid, prof in state.profiles.items():
        res = C.load_catalog(prof.resolve(), pid, key_for(state, pid), refresh=refresh)
        allm.extend(res.models)
        if res.error:
            # An error that already names the provider ("Azure OpenAI has no resource URL ...") is not prefixed.
            errors.append(res.error if res.error.startswith(prof.label) else "%s: %s" % (prof.label, res.error))
    allm = [m for m in allm if P.valid_model_id(m.id)]   # defense in depth: parse_models already drops them
    labels = {pid: p.label for pid, p in state.profiles.items()}
    if query:
        allm = C.search(allm, query, labels)
    else:
        provs = {pid: p.resolve() for pid, p in state.profiles.items()}
        recent = [(r["profile"], r["model"]) for r in state.recent]
        allm = [m for _, items in C.group(allm, provs, recent) for m in items]
    if top:
        allm = allm[:top]
    if as_json:
        print(json.dumps([{"id": m.id, "provider": m.profile, "context": m.context, "tags": list(m.tags)} for m in allm],
                         indent=2))
        return 0
    ui = _ui()
    cur = state.current()
    dot = glyph("\u25cf", "*")
    ell = glyph("\u2026", "~")
    lw = S._label_width([safe(labels.get(m.profile, m.profile), 80) for m in allm] or ["provider"])
    try:
        cols = int(os.environ.get("COLUMNS") or 0) or shutil.get_terminal_size((120, 24)).columns
    except (ValueError, OSError):
        cols = 120
    id_w = max(24, min(80, max([len(m.id) for m in allm] or [24]), cols - 2 - lw - 1 - 6 - 2 - 12))

    def clip(text: str, n: int) -> str:
        return text if len(text) <= n else text[: max(0, n - 1)] + ell

    for m in allm:
        current = bool(cur and (m.profile, m.id) == (cur.id, cur.model))
        ctx = S._fmt_ctx(m.context)
        line = "%s %-*s %-*s %6s  %s" % (dot if current else " ", id_w, clip(safe(m.id), id_w), lw,
                                         S.clip_label(safe(labels.get(m.profile, m.profile), 80), lw, ell != "~"),
                                         ctx, _plain(safe(" ".join(m.tags), 80)))
        print(ui.ink.paint(line.rstrip(), OK) if current else line.rstrip())
    for e in errors:
        print(ui.ink.paint("! " + safe(e, 300), WARN), file=sys.stderr)
    if not state.profiles:
        print("No provider connected. Run `pulsarcode key`.", file=sys.stderr)
    return 0


def cmd_status(args: List[str]) -> int:
    ui = _ui()
    state = K.load()
    brief = "--brief" in args
    ok_g, bad_g, arrow = glyph("\u2713", "+"), glyph("\u2717", "x"), _arrow()
    ok = lambda s: ui.ink.paint(ok_g, OK, True) + " " + s  # noqa: E731
    mark_neutral = lambda s: ui.ink.paint(glyph("\u25cb", "o"), DUST, True) + " " + s  # noqa: E731
    warn = lambda s: ui.ink.paint("!", WARN, True) + " " + s  # noqa: E731
    bad = lambda s: ui.ink.paint(bad_g, BAD, True) + " " + s  # noqa: E731
    out = []
    out.append(ui.ink.gradient(glyph(ui.mark(), "@") + " PULSAR CODE", bold=True) + ui.ink.paint("  v%s" % __version__, DUST))
    prof = state.current()
    if prof:
        prov = prof.resolve()
        key = key_for(state, prof.id)
        out.append((warn if placeholder_problem(prov) else ok)("provider   %s  (%s route)" % (prof.label, prov.route)))
        out.append(ok("model      %s" % (safe(prof.model) or "Claude Code default")) if prof.model else warn("model      not chosen"))
        if prov.needs_key:
            out.append(ok("key        " + _plain(P.mask(key))) if key else bad("key        missing -> pulsarcode key"))
    else:
        out.append(bad("provider   none -> pulsarcode key"))
    others = [p.label for pid, p in state.profiles.items()
              if pid != state.active and not profile_problem(state, pid)]   # those get their own "!" line below
    if others:
        out.append(ok("also       " + ", ".join(others)))
    # A profile that needs your own resource URL (Azure, Foundry) but has none saved (older saves) is never
    # contacted: launches, relays and switches refuse it. Say so here, with the way out.
    for pid, p in state.profiles.items():
        try:
            pprov = p.resolve()
        except KeyError:
            continue
        if placeholder_problem(pprov):
            out.append(warn("%-10s no resource URL: reconnect with  pulsarcode key %s  (then paste the key)"
                            % (safe(pid, 40), C.url_example(pprov))))
    if not brief:
        cb = L.find_claude()
        out.append(ok("claude     %s  %s" % (L.claude_version(cb), cb)) if cb else bad("claude     not installed"))
        cfg = L.claude_dir()
        out.append(ok("profile    " + str(cfg)) if cfg.is_dir() else
                   mark_neutral("profile    %s  (created on first launch)" % cfg))
        from pulsar.core import spawn, state as CST
        core = spawn.attach()
        out.append(ok("core       %s  pid %d" % (core.url, core.pid)) if core else ok("core       idle (ignites on launch)"))
        sessions = CST.live_sessions(reap=False)
        for sess in sessions[:6]:
            out.append(ok("session    %s  %s  %s" % (safe(sess.sid, 40), safe(sess.label or "", 20),
                                                     arrow.join(safe(t.model) for t in sess.plan))))
        if prof and prof.resolve().catalog != "none" and not placeholder_problem(prof.resolve()):
            res = C.load_catalog(prof.resolve(), prof.id, key_for(state, prof.id), refresh="--refresh" in args)
            line = "catalog    %d models (%s)" % (len(res.models), res.source)
            out.append(warn(line + "  " + safe(res.error, 300)) if res.error else ok(line))
        home = K.home()
        out.append(home_line(home, ok, warn, mark_neutral))
    print("\n".join(("  " if i else "") + line for i, line in enumerate(out)))
    return 0


def home_line(home: Path, ok, warn, neutral) -> str:
    """The status line for the Pulsar home: a check only for an existing private (0700) folder."""
    if not home.is_dir():
        return neutral("home       %s  (created on first connect)" % home)
    if os.name != "nt":
        try:
            mode = home.stat().st_mode & 0o777
        except OSError:
            mode = -1
        if mode != 0o700:
            return warn("home       %s  (mode %s, expected 0700)" % (home, oct(mode)[2:] if mode >= 0 else "?"))
    return ok("home       " + str(home))


def cmd_providers(args: List[str]) -> int:
    """The advertised providers (a filled dot when connected), then any connected profile they leave out.

    Unadvertised profiles (screens.UNADVERTISED) are never offered here, but one the user already
    connected keeps working and is shown, labelled as connected, so `pulsarcode use` stays discoverable.
    """
    ui = _ui()
    state = K.load()
    on_g, off_g = glyph("\u25cf", "*"), glyph("\u25cb", "o")
    # A profile that cannot be used (no resource URL saved) is marked "!" with the way out, not as connected.
    broken = {pid: S.blocked_hint(state.profiles[pid].resolve()) for pid in state.profiles
              if profile_problem(state, pid)}
    connected = {prof.provider for pid, prof in state.profiles.items() if pid not in broken}
    broken_by_provider = {state.profiles[pid].provider: why for pid, why in broken.items()}

    listed = S.advertised_providers()
    name_w = max([len(p.name) for p in listed] + [15])

    def row(p: P.Provider, on: bool, note: str, bad: bool = False) -> None:
        mark = (ui.ink.paint("!", WARN, True) if bad else
                ui.ink.paint(on_g if on else off_g, fx.hex_rgb(p.color) if ui.ink.depth else None, True))
        shape = ", ".join(p.prefixes) or ("no key" if not p.needs_key else "by shape / choose")
        print((" %s %-*s %-8s %-26s %s" % (mark, name_w, p.name, p.route, shape, note)).rstrip())

    for p in listed:
        on = p.id in connected or (p.id in state.profiles and p.id not in broken)
        bad = not on and (p.id in broken_by_provider or p.id in broken)
        note = (broken_by_provider.get(p.id) or broken.get(p.id) or "") if bad else ""
        row(p, on, ui.ink.paint(note, WARN) if bad else ui.ink.paint(p.free or "", OK), bad)
    shown = {p.id for p in listed}
    extra = [(pid, prof) for pid, prof in state.profiles.items() if prof.provider not in shown]
    if extra:
        print(ui.ink.paint("   connected (not in the list above):", DUST))
        for pid, prof in extra:
            prov = P.get(prof.provider)
            label = prof.label + ("" if prof.provider == "custom" or pid == prof.provider else "  [%s]" % safe(pid, 40))
            if pid in broken:
                mark, note = ui.ink.paint("!", WARN, True), ui.ink.paint(broken[pid], WARN)
            else:
                mark = ui.ink.paint(on_g, fx.hex_rgb(prov.color) if prov and ui.ink.depth else None, True)
                note = ui.ink.paint("connected; pulsarcode use %s" % safe(pid, 40), DUST)
            route = prov.route if prov else ("native" if prof.native else "adapter")
            print(" %s %-*s %-8s %s" % (mark, name_w, safe(label, 40), route, note))
    print(ui.ink.paint("   + any OpenAI- or Anthropic-compatible endpoint:  pulsarcode key https://host/v1"
                       "  (then paste the key at the masked prompt)", DUST))
    print(ui.ink.paint("   scripts:  printf %s \"$KEY\" | pulsarcode key --stdin   or   pulsarcode key --from-env VAR",
                       DUST))
    return 0


def cmd_use(args: List[str]) -> int:
    state = K.load()
    if not args:
        for pid, p in state.profiles.items():
            print(("* " if pid == state.active else "  ") + pid + "  " + p.label + ("  " + p.model if p.model else ""))
        return 0
    target = args[0]
    match = [pid for pid in state.profiles if pid == target or state.profiles[pid].label.lower().startswith(target.lower())]
    if not match:
        print("pulsarcode: no connected provider named %s. Connected: %s" % (target, ", ".join(state.profiles) or "none"),
              file=sys.stderr)
        return 1
    why = profile_problem(state, match[0])
    if why:
        # It would only be refused at the next launch: say so now and keep the working profile active.
        print("pulsarcode: " + why, file=sys.stderr)
        return 2
    state.active = match[0]
    K.save(state)
    print("pulsarcode: active provider is now %s" % state.profiles[match[0]].label)
    return 0


def cmd_logout(args: List[str]) -> int:
    gate = human_gate("forget a stored key")
    if gate is not None:
        return gate
    state = K.load()
    target = args[0] if args else state.active
    if target not in state.profiles:
        gone = K.forget_v1_key() if target == "nvidia" else []    # left behind by an earlier logout
        if gone:
            print("pulsarcode: forgot the v1 NVIDIA NIM key file (%s)" % ", ".join(gone))
            return 0
        print("pulsarcode: nothing to forget for %s" % (target or "(none)"), file=sys.stderr)
        return 1
    K.set_key(target, "")
    state.remove(target)
    K.save(state)
    # v1 kept the NVIDIA key in nim.key (nim.key.migrated after the import): forgetting it means those too.
    gone = K.forget_v1_key() if target == "nvidia" else []
    print("pulsarcode: forgot %s%s" % (target, " and the v1 key file (%s)" % ", ".join(gone) if gone else ""))
    return 0


REF_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
INSTALLER_MAX_BYTES = 1_000_000


def valid_ref(ref: str) -> bool:
    """A git tag, branch or commit Pulsar may install from. Never anything a shell or URL could reinterpret."""
    return (REF_RX.fullmatch(ref or "") is not None and ".." not in ref and "//" not in ref
            and not ref.endswith(("/", ".", ".lock")))


def _https_get(url: str, limit: int = INSTALLER_MAX_BYTES, timeout: float = 30.0) -> bytes:
    """GET over https without following redirects (the catalog opener). Raises OSError/ValueError."""
    if not url.startswith("https://"):
        raise ValueError("refusing a non-https URL")
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "pulsarcode/" + __version__})
    try:
        with C.OPENER.open(req, timeout=timeout) as resp:
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise OSError("%s answered HTTP %d" % (url, exc.code))
    if len(data) > limit:
        raise ValueError("download larger than expected")
    return data


def latest_release() -> str:
    """The newest published release tag, or "" (no release yet, offline, rate-limited)."""
    try:
        data = json.loads(_https_get("https://api.github.com/repos/%s/releases/latest" % REPO, 200_000, 8.0))
    except (OSError, ValueError):
        return ""
    tag = data.get("tag_name") if isinstance(data, dict) else ""
    return tag if isinstance(tag, str) and valid_ref(tag) else ""


UPDATE_DIR_PREFIX = "pulsarcode-update-"
# Releases before this one have another installer and home layout: run from a v3 home, their installer
# stops halfway and leaves v1 folders behind. `pulsarcode update` never installs them.
UPDATE_FLOOR = (3, 0, 0)
_VERSION = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]?(dev|a|alpha|b|beta|c|rc|pre|preview)[-.]?(\d+)?)?$",
                      re.I)
_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "rc": 3, "pre": 3, "preview": 3}


def version_key(text: str) -> Optional[Tuple[int, int, int, int, int]]:
    """(major, minor, patch, pre-release rank, pre-release number) of a release tag or version, else None.

    A release sorts after its own pre-releases (v3.1.0-rc.1 < v3.1.0). Branches and commits give None.
    """
    m = _VERSION.match((text or "").strip())
    if not m:
        return None
    major, minor, patch, pre, num = m.groups()
    return int(major), int(minor or 0), int(patch or 0), _PRE_RANK[pre.lower()] if pre else 9, int(num or 0)


def _update_refusal(ref: str, allow_downgrade: bool, latest: bool) -> str:
    """Why `ref` must not replace this Pulsar Code, or "". Branches and commits are installed as named."""
    target = version_key(ref)
    if target is None:
        return ""
    shown = safe(ref, 70)
    what = "the latest release (%s)" % shown if latest else shown
    if target[:3] < UPDATE_FLOOR:
        return ("%s is from before v3.0.0: its installer cannot upgrade this Pulsar Code. Nothing was installed."
                % what)
    running = version_key(__version__)
    if running is not None and target < running and not allow_downgrade:
        return ("%s is older than this Pulsar Code (%s). Nothing was installed. To install it anyway: "
                "pulsarcode update %s --allow-downgrade" % (what, safe(__version__, 40), shown))
    return ""


def _clean_path(value: str, cwd: str = "") -> str:
    """PATH without empty or relative entries (each one means the current folder) and without the folder
    `pulsarcode update` runs from: the installer looks up bash, curl, tar and python3 on it."""
    try:
        here = os.path.realpath(cwd or os.getcwd())
    except OSError:
        here = ""
    out: List[str] = []
    for entry in (value or "").split(os.pathsep):
        bare = entry.strip().strip('"')
        if not bare or not os.path.isabs(bare):
            continue
        try:
            if here and os.path.realpath(bare) == here:
                continue
        except (OSError, ValueError):
            continue
        out.append(entry)
    if not out and os.name != "nt":
        out = ["/usr/bin", "/bin"]
    return os.pathsep.join(out)


def _system_bash(path_value: str) -> str:
    """bash by absolute path: the system's own /bin/bash (or /usr/bin/bash) first, so no bash a project folder on
    PATH holds can stand in for it; else the first one on `path_value` (absolute entries only), for systems
    without one there. The installer needs bash 3.2 or later, which every system bash is."""
    for d in ["/bin", "/usr/bin"] + path_value.split(os.pathsep):
        d = d.strip().strip('"')
        cand = os.path.join(d, "bash")
        if os.path.isabs(d) and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return ""


def _download_installer(ref: str) -> str:
    """install.sh (install.ps1 on Windows) from `ref` itself, as a 0600 file alone in a private 0700 folder.

    Never directly in the shared temp dir: whatever another local user plants next to the script
    (a pulsar/ folder, a pulsarcode file) must not be mistaken for the source to install.
    """
    import tempfile
    name = "install.ps1" if os.name == "nt" else "install.sh"
    data = _https_get("https://raw.githubusercontent.com/%s/%s/%s" % (REPO, ref, name))
    folder = tempfile.mkdtemp(prefix=UPDATE_DIR_PREFIX)
    path = os.path.join(folder, name)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        _drop_installer(path)
        raise
    return path


def _drop_installer(path: str) -> None:
    """Remove a downloaded installer and its private folder (only a folder _download_installer made)."""
    try:
        os.unlink(path)
    except OSError:
        pass
    folder = os.path.dirname(path)
    if os.path.basename(folder).startswith(UPDATE_DIR_PREFIX):
        try:
            os.rmdir(folder)
        except OSError:
            pass


def windows_powershell() -> str:
    """Windows PowerShell by absolute path: a bare `powershell` is also looked up in the current folder."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    if not re.match(r"^[A-Za-z]:[\\/]", root):
        root = "C:\\Windows"
    return root.rstrip("\\/") + "\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"


# ---------------------------------------------------------------------------
# package-managed installs (Homebrew, Scoop, pipx, uv tool, pip)
# ---------------------------------------------------------------------------

_MANAGERS = {
    "homebrew": ("Homebrew", "brew upgrade pulsarcode", "brew uninstall pulsarcode"),
    "brew": ("Homebrew", "brew upgrade pulsarcode", "brew uninstall pulsarcode"),
    "scoop": ("Scoop", "scoop update pulsarcode", "scoop uninstall pulsarcode"),
    "pipx": ("pipx", "pipx upgrade pulsarcode", "pipx uninstall pulsarcode"),
    "uv": ("uv", "uv tool upgrade pulsarcode", "uv tool uninstall pulsarcode"),
}


def _under(path: str, folder: str) -> bool:
    if not folder:
        return False
    try:
        a, b = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(os.path.expanduser(folder)))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def install_channel(environ=None, root: Optional[str] = None) -> Tuple[str, str, str]:
    """(manager, update command, uninstall command) when a package manager owns this install, else ("", "", "").

    PULSAR_MANAGED is set by the Homebrew and Scoop launchers. A `pulsar` package inside site-packages
    was installed by pip, pipx or `uv tool`. Those must be updated by their manager: the installer
    would put a second copy into ~/.pulsarcode and leave the managed one shadowing or shadowed on PATH.
    """
    env = os.environ if environ is None else environ
    root = os.path.realpath(ROOT if root is None else root)
    managed = (env.get("PULSAR_MANAGED") or "").strip().lower()
    if managed in _MANAGERS:
        return _MANAGERS[managed]
    parts = [x.lower() for x in re.split(r"[\\/]+", root) if x]
    if parts and parts[-1] in ("site-packages", "dist-packages"):
        if _under(root, env.get("PIPX_HOME", "")) or "pipx" in parts:
            return _MANAGERS["pipx"]
        if _under(root, env.get("UV_TOOL_DIR", "")) or ("uv" in parts and "tools" in parts):
            return _MANAGERS["uv"]
        py = sys.executable or "python3"
        q = '"%s"' % py if " " in py else py
        return ("pip", "%s -m pip install --upgrade pulsarcode" % q, "%s -m pip uninstall pulsarcode" % q)
    if managed:
        name = safe(managed, 40)
        return (name, "", "")
    return ("", "", "")


def _managed_note(manager: str, command: str, what: str) -> str:
    """`what` is "update" or "remove"."""
    if command:
        return "This Pulsar Code was installed with %s; %s it with:\n    %s" % (manager, what, command)
    return ("This Pulsar Code is managed by %s (PULSAR_MANAGED); %s it with that package manager."
            % (manager, what))


def cmd_update(args: List[str]) -> int:
    """Install a release (default: the latest), a tag or a branch. No shell string is ever built.

    A package-managed install (Homebrew, Scoop, pipx, uv tool, pip) is never reinstalled into
    ~/.pulsarcode: the right command for its manager is printed instead. A release from before v3.0.0
    is never installed, nor one older than this Pulsar Code without --allow-downgrade. An exported
    PULSAR_REF is ignored (a pin left in a dotfile would reinstall the same ref forever): name the ref.
    The installer runs by absolute path, from its own private folder, with a PATH that holds no
    relative entry and not the folder you ran this from.
    """
    manager, upgrade, _ = install_channel()
    if manager:
        print("pulsarcode: " + _managed_note(manager, upgrade, "update"), file=sys.stderr)
        return 1
    gate = human_gate("update Pulsar Code")
    if gate is not None:
        return gate
    allow_downgrade = "--allow-downgrade" in args
    refs = [a for a in args if a != "--allow-downgrade"]
    if len(refs) > 1:
        print("pulsarcode: update takes one tag, branch or commit (plus --allow-downgrade). Nothing was installed.",
              file=sys.stderr)
        return 2
    ref = refs[0].strip() if refs else ""
    if refs and not valid_ref(ref):
        print("pulsarcode: %r is not a valid tag, branch or commit (letters, digits, . _ / - only)" % safe(ref, 70),
              file=sys.stderr)
        return 2
    if not refs and (os.environ.get("PULSAR_REF") or "").strip():
        print("pulsarcode: PULSAR_REF is set in this shell; update ignores it. To install that ref, name it: "
              "pulsarcode update <ref>", file=sys.stderr)
    latest = not ref
    if latest:
        ref = latest_release()
    if not ref:
        # Never fall back to an unreleased branch on our own: offline, rate-limited and "no release
        # yet" all look the same from here, and main is not a verified release.
        print("pulsarcode: could not find the latest release of %s (GitHub unreachable or rate-limited, or no "
              "release published yet). Nothing was installed.\n"
              "  Try again later:          pulsarcode update\n"
              "  Install a specific tag:   pulsarcode update vX.Y.Z   (tags: https://github.com/%s/releases)\n"
              "  Install unreleased main:  pulsarcode update main   (not a release; at your own risk)"
              % (REPO, REPO), file=sys.stderr)
        return 1
    why = _update_refusal(ref, allow_downgrade, latest)
    if why:
        print("pulsarcode: " + why, file=sys.stderr)
        return 1 if latest else 2
    env = dict(os.environ)
    env["PATH"] = _clean_path(env.get("PATH", ""))
    bash = "" if os.name == "nt" else _system_bash(env["PATH"])
    if os.name != "nt" and not bash:
        print("pulsarcode: bash was not found (the installer needs it). Nothing was installed.", file=sys.stderr)
        return 1
    try:
        path = _download_installer(ref)
    except (OSError, ValueError) as exc:
        print("pulsarcode: could not download the installer for %s: %s" % (ref, safe(exc, 200)), file=sys.stderr)
        return 1
    env["PULSAR_REF"] = ref
    env["PULSAR_NO_LAUNCH"] = "1"
    env["PULSAR_NO_LOCAL_SRC"] = "1"         # always the downloaded release, never a folder next to the script
    env.pop("PULSAR_SRC", None)
    if os.name == "nt":
        cmd = [windows_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    else:
        cmd = [bash, path]
    print("pulsarcode: installing %s" % ref)
    try:
        # From the installer's own private folder: nothing in the folder you ran this from can stand in
        # for a tool it calls.
        return subprocess.call(cmd, env=env, cwd=os.path.dirname(path) or None)
    finally:
        _drop_installer(path)


# What Pulsar keeps in its home; uninstall removes these and nothing else.
HOME_CHILDREN = K.HOME_CHILDREN


def home_problem(home: Path) -> str:
    """Why `home` must not be deleted ("" when it is a Pulsar home).

    A marker Pulsar wrote when it created the folder (or found it empty) is enough. A folder adopted
    from an older Pulsar Code must still hold Pulsar's own files. Without provenance (no marker, or a
    marker from a build that wrote it into any folder), the folder must hold nothing but Pulsar's files.
    """
    why = K.unsafe_home(home)
    if why:
        return why
    real = home.resolve()
    if not real.is_dir():
        return "is not a folder"
    kind = K.marker_kind(real)
    if kind == "created":
        return ""
    if not K.home_evidence(real):
        return "has no Pulsar Code marker (%s)" % K.MARKER
    if kind == "adopted":
        return ""
    foreign = K.foreign_entries(real)
    if foreign:
        return ("holds files Pulsar Code did not create (%s) and no marker proving Pulsar created the folder"
                % ", ".join(safe(n, 40) for n in foreign[:5]))
    return ""


def _remove(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# The line install.ps1 writes into each launcher it makes: "# Pulsar Code home: <path>" (PowerShell, Git
# Bash) or rem Pulsar Code home: "<path>" (cmd.exe, with % doubled).
_SHIM_HOME = re.compile(r"^(#|rem) Pulsar Code home: (.+?)\s*$")


def _shim_home(text: str) -> str:
    """The home a launcher names on its own 'Pulsar Code home:' line, or ""."""
    for line in text.splitlines():
        m = _SHIM_HOME.match(line)
        if not m:
            continue
        value = m.group(2)
        if m.group(1) == "rem":
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            value = value.replace("%%", "%")
        return value
    return ""


def _same_folder(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def _our_shim(path: str, home: Path) -> bool:
    """True when `path` is the pulsarcode command an installer wrote for this home (never someone else's file):
    a link into the home, or a launcher whose own 'Pulsar Code home:' line names exactly this home. A
    substring is never enough: with PULSAR_HOME=/p, another install's launcher holds "/p" too."""
    try:
        real_home = str(home.resolve())
        if os.path.islink(path):
            return os.path.realpath(path).startswith(real_home + os.sep)
        with open(path, "rb") as fh:
            head = fh.read(8192).decode("utf-8", "replace")
    except OSError:
        return False
    named = _shim_home(head)
    return bool(named) and _same_folder(named, real_home)


# The PATH block install.sh appends to your shell startup file: printf '\n# Pulsar Code\n%s\n' "$line".
# v1 installers wrote "# pulsarcode" (earlier "# Local Pulsar / pulsarcode") above the same export line, or
# above `set -gx PATH "<dir>" $PATH` in config.fish. In config.fish, `fish_add_path -g <dir>` (global, kept
# in config.fish only) is read as well as `fish_add_path <dir>` (also stored in fish's universal variables).
# On macOS with bash, install.sh writes into the first login file bash reads: ~/.bash_profile, else an existing
# ~/.bash_login, else ~/.profile.
RC_FILES = (".zshrc", ".bashrc", ".bash_profile", ".bash_login", ".profile",
            os.path.join(".config", "fish", "config.fish"))
RC_MARK = "# Pulsar Code"
RC_MARKS = (RC_MARK, "# pulsarcode", "# Local Pulsar / pulsarcode")
_RC_LINE = re.compile(r'^(?:export PATH="(?P<sh>[^"\r\n]+):\$PATH"|fish_add_path (?P<fishg>-g )?(?P<fish>[^\r\n]+?)'
                      r'|set -gx PATH "(?P<v1fish>[^"\r\n]+)" \$PATH)\s*$')
SHIM_NAMES = ("pulsarcode", "pulsarcode.cmd", "pulsarcode.ps1")


def _rc_dir(m: "re.Match") -> str:
    """The bin dir of a matched installer line, unquoted: install.sh writes fish_add_path '<dir>' when
    the folder has a space or a non-ASCII character (its only quoted form)."""
    if m.group("sh"):
        return m.group("sh")
    if m.group("v1fish"):
        return m.group("v1fish")
    d = m.group("fish")
    if len(d) >= 2 and d[0] == d[-1] == "'" and "'" not in d[1:-1]:
        return d[1:-1]
    return d


def _rc_files(base: Path, use_env: bool = True) -> List[Path]:
    """Every startup file install.sh may have written, deduplicated by real path: the ones under the
    home folder plus $ZDOTDIR/.zshrc and $XDG_CONFIG_HOME/fish/config.fish."""
    cands = [base / rel for rel in RC_FILES]
    if use_env:
        zd = os.environ.get("ZDOTDIR") or ""
        xdg = os.environ.get("XDG_CONFIG_HOME") or ""
        if zd and os.path.isabs(zd):
            cands.append(Path(zd) / ".zshrc")
        if xdg and os.path.isabs(xdg):
            cands.append(Path(xdg) / "fish" / "config.fish")
    out: List[Path] = []
    seen = set()
    for rc in cands:
        key = os.path.realpath(str(rc))
        if key not in seen:
            seen.add(key)
            out.append(rc)
    return out


def rc_blocks(home_dir: Optional[str] = None) -> List[Tuple[Path, str]]:
    """(startup file, bin dir) for every exact two-line block the installer wrote."""
    base = Path(home_dir) if home_dir else Path.home()
    out: List[Tuple[Path, str]] = []
    for rc in _rc_files(base):
        try:
            if not rc.is_file():
                continue
            lines = rc.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines[:-1]):
            if line.rstrip("\r") not in RC_MARKS:
                continue
            m = _RC_LINE.match(lines[i + 1].rstrip("\r"))
            if m:
                out.append((rc, _rc_dir(m)))
    return out


def remove_rc_block(rc: Path, bin_dir: str) -> bool:
    """Remove exactly the installer's block for `bin_dir` (and the blank line it added before it).

    Writes through a symlinked startup file to its target, keeps the file's permissions, and is atomic.
    False when there was nothing to remove or the file cannot be written (read-only dotfiles).
    """
    try:
        real = Path(os.path.realpath(str(rc)))
        if not os.access(str(real), os.W_OK) or not real.stat().st_mode & 0o222:
            return False                           # read-only dotfile (even for root): leave it alone
        with open(str(real), encoding="utf-8", errors="surrogateescape", newline="") as fh:
            text = fh.read()                       # newline="": CRLF files stay CRLF
    except OSError:
        return False
    lines = text.splitlines(True)
    out: List[str] = []
    i, changed = 0, False
    while i < len(lines):
        line = lines[i]
        if line.rstrip("\r\n") in RC_MARKS and i + 1 < len(lines):
            m = _RC_LINE.match(lines[i + 1].rstrip("\r\n"))
            if m and _rc_dir(m) == bin_dir:
                if out and out[-1].strip() == "":
                    out.pop()                                 # the "\n" printf put before the block
                i += 2
                changed = True
                continue
        out.append(line)
        i += 1
    if not changed:
        return False
    import tempfile
    try:
        mode = real.stat().st_mode & 0o7777
        fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix="." + real.name + ".pulsar-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                fh.write("".join(out))
            os.chmod(tmp, mode)
            os.replace(tmp, str(real))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


def _fish_universal(rc: Path, bin_dir: str) -> bool:
    """`rc` holds the installer's `fish_add_path <bin_dir>`: fish also stored the folder in its universal
    fish_user_paths, which no startup file edit removes."""
    try:
        lines = rc.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return False
    for line in lines:
        m = _RC_LINE.match(line.rstrip("\r"))
        if m and m.group("fish") and not m.group("fishg") and _rc_dir(m) == bin_dir:
            return True
    return False


def _only_shims_left(folder: str) -> bool:
    """`folder` holds nothing but pulsarcode launchers (or nothing at all)."""
    try:
        return not [n for n in os.listdir(folder) if n not in SHIM_NAMES]
    except OSError:
        return False


def _on_windows() -> bool:
    return os.name == "nt"


def user_path_without(raw: str, folder: str, expand: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """`raw` (a Windows PATH as stored, ';'-separated) without its entries naming `folder`, compared the way
    Windows does (case-insensitive, %VARIABLES% expanded, a trailing backslash ignored); None when no
    entry names it. Every other entry, empty ones included, stays exactly as written."""
    import ntpath
    expand = expand or ntpath.expandvars

    def norm(p: str) -> str:
        return ntpath.normcase(p.strip().rstrip("\\/"))
    want = norm(folder)
    if not raw or not want:
        return None
    parts = raw.split(";")
    kept = [p for p in parts if not (p.strip() and want in (norm(p), norm(expand(p))))]
    return None if len(kept) == len(parts) else ";".join(kept)


def _broadcast_environment() -> None:
    """Tell Explorer, and so every terminal opened from now on, that the user environment changed."""
    try:
        import ctypes
        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000,  # type: ignore
                                                 ctypes.byref(result))
    except Exception:
        pass


def remove_user_path_entry(folder: str, reg=None, broadcast: bool = True) -> bool:
    """Take `folder` out of the Windows user PATH (HKCU\\Environment), where install.ps1 added it.

    True when an entry was removed. The value keeps its type: an expandable PATH keeps its %VARIABLES%.
    `reg` is the winreg module (a stand-in in tests).
    """
    if reg is None:
        if os.name != "nt":
            return False
        import winreg as reg
    try:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, "Environment", 0, reg.KEY_READ | reg.KEY_WRITE)
    except OSError:
        return False
    try:
        raw, kind = reg.QueryValueEx(key, "Path")
        new = user_path_without(raw, folder) if isinstance(raw, str) else None
        if new is None:
            return False
        reg.SetValueEx(key, "Path", 0, kind if kind in (reg.REG_SZ, reg.REG_EXPAND_SZ) else reg.REG_EXPAND_SZ, new)
    except OSError:
        return False
    finally:
        reg.CloseKey(key)
    if broadcast:
        _broadcast_environment()
    return True


def marker_bin_dirs(home: Optional[Path] = None) -> List[str]:
    """The bin folders install.sh recorded in the home marker ('bin <dir>' lines)."""
    try:
        text = ((home or K.home()) / K.MARKER).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line[4:].rstrip("\r") for line in text.splitlines()[1:]
            if line.startswith("bin ") and os.path.isabs(line[4:].rstrip("\r"))]


def _shim_dirs(blocks: List[Tuple[Path, str]]) -> List[str]:
    dirs: List[str] = []
    found = shutil.which("pulsarcode")
    for d in (os.environ.get("PULSAR_BIN_DIR"), os.environ.get("PULSARCODE_BIN_DIR"),
              str(Path.home() / ".local" / "bin"), *[b for _, b in blocks], *marker_bin_dirs(),
              os.path.dirname(found) if found else ""):
        if d and os.path.isabs(os.path.expanduser(d)) and os.path.expanduser(d) not in dirs:
            dirs.append(os.path.expanduser(d))
    return dirs


def cmd_uninstall(args: List[str]) -> int:
    ui = _ui()
    gate = human_gate("uninstall Pulsar Code")
    if gate is not None:
        return gate
    manager, _, remove_cmd = install_channel()
    home = K.home()
    if manager and not os.path.lexists(str(home)):
        print("pulsarcode: no Pulsar Code data in %s. %s" % (home, _managed_note(manager, remove_cmd, "remove")))
        return 0
    problem = home_problem(home)
    if problem:
        print("pulsarcode: refusing to uninstall: PULSAR_HOME=%s %s. Nothing was deleted." % (home, problem),
              file=sys.stderr)
        return 2
    if "--yes" not in args:
        if not sys.stdin.isatty():
            print("pulsarcode: pass --yes to uninstall non-interactively", file=sys.stderr)
            return 2
        what = ("Remove %s (keys, settings, isolated Claude profile)? The pulsarcode command stays with %s. [y/N] "
                % (home, manager)) if manager else (
            "Remove %s (keys, settings, isolated Claude profile) and the pulsarcode command? [y/N] " % home)
        ans = input(what)
        if ans.strip().lower() not in ("y", "yes"):
            return 1
    L.stop_core()
    blocks = rc_blocks()
    removed_from: List[str] = []           # folders a launcher of ours was taken out of
    for d in _shim_dirs(blocks):
        for name in SHIM_NAMES:
            f = os.path.join(d, name)
            if os.path.lexists(f) and _our_shim(f, home):
                try:
                    os.unlink(f)
                except OSError:
                    continue
                if d not in removed_from:
                    removed_from.append(d)
    real = home.resolve()
    for name in HOME_CHILDREN:
        if name != K.MARKER:
            _remove(real / name)
    leftovers = [p.name for p in real.iterdir() if p.name != K.MARKER] if real.is_dir() else []
    if not leftovers:
        _remove(real / K.MARKER)
        try:
            real.rmdir()
        except OSError:
            pass
    else:
        ui.say("Kept %d file(s) Pulsar Code did not create in %s." % (len(leftovers), real), DUST)
    # The installer's PATH line goes only when its folder holds nothing else: other tools (claude itself
    # lives in ~/.local/bin) may depend on it.
    for rc, d in blocks:
        full = os.path.expanduser(d)
        if not os.path.isabs(full) or not os.path.isdir(full):
            ui.say("Kept the PATH line for %s in %s: that folder could not be checked." % (safe(d, 120), rc), DUST)
            continue
        try:
            others = [n for n in os.listdir(full) if n not in SHIM_NAMES]
        except OSError:
            others = ["?"]
        if others:
            ui.say("Kept the PATH line for %s in %s: that folder holds other programs." % (safe(d, 120), rc), DUST)
            continue
        universal = _fish_universal(rc, d)
        if remove_rc_block(rc, d):
            ui.say("Removed the PATH line for %s from %s." % (safe(d, 120), rc), DUST)
            if universal:
                shown = "'%s'" % d if "'" not in d else d
                ui.say("fish also keeps %s in its universal fish_user_paths; to drop it, run in fish:  "
                       "set -U fish_user_paths (string match -v -- %s $fish_user_paths)"
                       % (safe(d, 4096), safe(shown, 4098)), DUST)        # a cut path would make a wrong command
        else:
            ui.say("Could not edit %s; remove its PATH line for %s yourself." % (rc, safe(d, 4096)), DUST)
    # install.ps1 adds its bin folder to the Windows user PATH; it goes with the last launcher in it.
    if _on_windows():
        for d in removed_from:
            if _only_shims_left(d) and remove_user_path_entry(d):
                ui.say("Removed %s from your user PATH (new terminals)." % safe(d, 120), DUST)
    if manager:
        ui.say("Pulsar Code's data was removed. " + (
            "Remove the pulsarcode command itself with:  %s" % remove_cmd if remove_cmd else
            "Remove the pulsarcode command itself with %s." % manager), ASH)
        return 0
    ui.say("Pulsar Code removed. Claude Code itself was left installed. Thanks for flying.", ASH)
    return 0


def cmd_demo(args: List[str]) -> int:
    import time
    ui = _ui()
    quick = "--quick" in args
    scale = 0.3 if quick else 1.0           # --quick: the canned tasks finish quickly too

    def slow(label: str, d: float, detail: str):
        def fn():
            time.sleep(d * scale)
            return "ok", detail
        return S.Task(label, fn)
    tasks = [slow("Claude Code", 0.4, "ready"), slow("Isolated profile", 0.2, "isolated"),
             slow("Uplink", 0.7, "demo"), slow("Model catalog", 1.1, "312 models · live"),
             slow("Pulsar Core", 1.4, ":4747 · ignited · token-gated")]
    S.boot(ui, tasks, quick=quick, demo=True)   # explicit: never inferred from argv
    S.warp(ui, [("demo  ·  " + TAGLINE, ICE)])
    return 0


def cmd_stop(args: List[str]) -> int:
    """Stop Pulsar Core. While sessions are live it waits for them: new launches start a fresh Core at once,
    and this one exits when the last of them ends, so no running Claude Code is left sending to a free port.
    --force stops it now."""
    from pulsar.core import state as ST
    force = "--force" in args
    live = [] if force else ST.live_sessions(reap=False)
    if not L.stop_core(force=force):
        print("pulsarcode: Pulsar Core was not running")
        return 0
    if live:
        n = len(live)
        print("pulsarcode: Pulsar Core stops when its %d live session%s; new launches start a fresh Core. "
              "To stop it now: pulsarcode stop --force" % (n, " ends" if n == 1 else "s end"))
    else:
        print("pulsarcode: Pulsar Core is shutting down")
    return 0


def _profile_named(state: K.State, head: str) -> Optional[K.Profile]:
    """The connected profile `head` names (profile id, provider id or label), else None."""
    for pid, prof in state.profiles.items():
        if pid == head or prof.provider == head or prof.label.lower() == head.lower():
            return prof
    return None


def _resolve_target(state: K.State, spec: str,
                    unchecked: Optional[List[Tuple[str, str]]] = None) -> Optional[Tuple[K.Profile, str]]:
    """'model', 'provider:model' or 'profile:model' -> (profile, model).

    `unchecked`, when given, receives (profile id, reason) for the catalogs that could not be consulted."""
    spec = spec.strip()
    if not spec:
        return None
    if ":" in spec:
        head, model = spec.split(":", 1)
        named = _profile_named(state, head)
        if named is not None:
            return named, model
    order = [pid for pid in [state.active] + [p for p in state.profiles if p != state.active] if pid in state.profiles]
    for pid in order:
        if state.profiles[pid].model == spec:
            return state.profiles[pid], spec
    cands = []
    for pid in order:
        try:
            cands.append((pid, state.profiles[pid].resolve(), key_for(state, pid)))
        except KeyError:
            continue
    # Cached catalogs first, then the catalogs of profiles that have none yet. An id no connected
    # catalog lists is never bound to the active profile by default: that would send one vendor's
    # model id (and the next launch) to another vendor. Use provider:model for that.
    pid, known = C.locate(spec, cands, unchecked=unchecked)
    if pid:
        return state.profiles[pid], spec
    if not known and state.current() is not None:
        return state.current(), spec       # no catalog exists or can be asked for: nothing to check against
    return None


def cloud_switch_problem(prov: P.Provider, profile_id: str = "") -> str:
    """Why a running Core session cannot move to `prov`, or "". Bedrock, Vertex (and a Claude login token)
    are spoken to by Claude Code itself: Core cannot serve them, so the session would only get refusals."""
    if prov.route != "cloud":
        return ""
    return ("%s is used by Claude Code itself, not through Pulsar Core, so a running session cannot switch to "
            "it. Relaunch with that profile instead: pulsarcode use %s, then pulsarcode. Nothing was changed."
            % (prov.name, safe(profile_id or prov.id, 40)))


def cmd_switch(args: List[str]) -> int:
    """Hot-swap the model of a running session (next message onward) and make it the default.

    Inside a session (PULSAR_SESSION set, e.g. /pulsar:switch) only that session switches; moving any
    other session from inside one needs a human (human_gate). From another terminal it switches the
    only running session, if exactly one runs; --session <sid> picks one, --all every one. The model
    also becomes the default for the next launch.
    """
    from pulsar.core import state as ST
    sid = ""
    rest: List[str] = []
    listing = every = False
    it = iter(args)
    for a in it:
        if a == "--session":
            sid = next(it, "")
        elif a == "--all":
            every = True
        elif a == "--list":
            listing = True
        elif a == "--":
            rest.extend(it)
        else:
            rest.append(a)
    own = os.environ.get("PULSAR_SESSION", "")
    if not sid and not every:
        sid = own
    state = K.load()
    live = ST.live_sessions(reap=False)
    arrow = _arrow()
    only = ""
    if not sid and not every and len(live) == 1 and not claude_session_var():
        only = sid = live[0].sid           # from another terminal with exactly one session running: that one
    if listing or not rest:
        for s in live:
            print("%s  %-28s %s" % (safe(s.sid, 40), safe(s.label or s.cwd)[-28:], arrow.join(safe(t.model) for t in s.plan)))
        if not live:
            print("pulsarcode: no running sessions")
        return 0
    spec = " ".join(rest).strip()
    if not P.valid_model_id(spec):
        print("pulsarcode: %r is not a model id (%s)" % (safe(spec, 60), P.MODEL_ID_RULE), file=sys.stderr)
        return 2
    if every or (sid and sid != own):
        # A model inside one session (an allow rule like Bash(pulsarcode switch:*)) moves only its own session,
        # also when its shell unset PULSAR_SESSION (human_gate sees the Claude Code process above it).
        gate = human_gate("switch other running sessions")
        if gate is not None:
            return gate
    head, colon, tail = spec.partition(":")
    if colon and head and not tail.strip():
        # `pulsarcode switch groq:` would save an empty model (and print an empty id).
        print("pulsarcode: name a model after %s: (for example %s:<model-id>; see `pulsarcode models`). "
              "Nothing was changed." % (safe(head, 40), safe(head, 40)), file=sys.stderr)
        return 2
    unchecked: List[Tuple[str, str]] = []
    found = _resolve_target(state, spec, unchecked)
    if found is None:
        if not state.profiles:
            print("pulsarcode: no provider connected. Run `pulsarcode key`.", file=sys.stderr)
        else:
            print("pulsarcode: %s Nothing was changed." % C.unlisted_message(spec, list(state.profiles), unchecked),
                  file=sys.stderr)
        return 1
    prof, model = found
    try:
        prov = prof.resolve()
    except KeyError:
        print("pulsarcode: unknown provider for %s" % prof.id, file=sys.stderr)
        return 1
    why = placeholder_problem(prov) or L.transport_problem(prov, key_for(state, prof.id))
    if why:
        print("pulsarcode: " + why, file=sys.stderr)
        return 2
    targets = [s for s in live if every or (sid and s.sid == sid)]
    # A cloud or direct session: Claude Code talks to its provider itself, so a new plan changes nothing it
    # sends. Never claim that switch: refuse it (one session), or leave it out and say so (--all).
    blocked = [s for s in targets if s.side_channel]
    relaunch = "pulsarcode model %s:%s, then pulsarcode" % (safe(prof.id, 40), safe(model, 120))
    if blocked and not every:
        print("pulsarcode: %s. Use Claude Code's /model in that session, or relaunch with this model: %s. "
              "Nothing was changed." % (ST.side_channel_note(blocked[0]), relaunch), file=sys.stderr)
        return 2
    targets = [s for s in targets if not s.side_channel]
    if targets and cloud_switch_problem(prov, prof.id):
        # A Core session cannot be served by Bedrock or Vertex: every later message would be refused.
        print("pulsarcode: " + cloud_switch_problem(prov, prof.id), file=sys.stderr)
        return 2
    target = L.target_for(prof, model)
    m = next((x for x in C.read_cache(prof.id, ttl=10 * 365 * 86400) or [] if x.id == model), None)
    target.context = (m.context if m else 0) or C.guess_context(model)
    for s in targets:
        # The relays a session keeps follow relay_plan's rules (the MCP switch uses the same): none behind a
        # local or custom model, and none in a session with Pilot armed.
        ST.update_plan(s.sid, L.switch_plan(state, s.plan, target, pilot=bool(s.flags.get("pilot"))))
    state.set_model(prof.id, model, target.context)
    K.save(state)
    if targets and only:
        where = "the only running session (%s)" % safe(only, 40)
    elif targets:
        where = "%d running session%s" % (len(targets), "" if len(targets) == 1 else "s")
    elif sid:
        where = "the next launch (session %s is not running)" % safe(sid, 40)
    elif every:
        where = "the next launch"
    else:
        where = "the next launch" + (" (add --all to switch the %d running session%s too)" % (
            len(live), "" if len(live) == 1 else "s") if live else "")
    print("pulsarcode: %s on %s for %s%s" % (safe(model, 120), prof.label, where,
                                              " (takes effect on the next message)" if targets else ""))
    for s in blocked:
        print("pulsarcode: left out: %s (use Claude Code's /model there)." % ST.side_channel_note(s))
    return 0


# PULSAR_RELAY values that turn the relay off for one shell, over any saved `pulsarcode relay` choice.
RELAY_ENV_OFF = ("off", "none", "0")
# What launch.relay_plan does, in the words `pulsarcode relay` prints.
RELAY_AUTO_RULE = ("auto relays only from a cloud provider to the other cloud providers you connected: a local "
                   "server, a host on your own network or a custom endpoint never relays automatically.")
RELAY_PILOT_RULE = "A session with Pilot armed never relays."


def _relay_rules(pref: object) -> None:
    """The rules that decide whether a session relays, for the relay choice `pref` (nothing when it is off)."""
    if pref in ("off", "none", "0", False):
        return
    if not isinstance(pref, list):
        print("  " + RELAY_AUTO_RULE)
    print("  " + RELAY_PILOT_RULE)
    if L.pilot_env().get("PULSAR_PILOT") == "1":
        print("  Pilot is on, so a session launched now runs without a relay.")


def cmd_relay(args: List[str]) -> int:
    state = K.load()
    env_off = os.environ.get("PULSAR_RELAY", "").strip().lower() in RELAY_ENV_OFF
    if not args:
        pref = state.prefs.get("relay", "auto")
        prof = state.current()
        chain = L.relay_plan(state, prof, lambda pid: key_for(state, pid)) if prof else []
        shown = pref if isinstance(pref, str) else ", ".join(safe(p) for p in pref)
        if env_off:
            shown = "off (PULSAR_RELAY=off for this shell; saved: %s)" % shown
        print("relay: %s" % shown)
        print("chain: %s" % (_arrow().join(safe(m) for m in [prof.model if prof else "?"] + [t.model for t in chain])))
        if not env_off:
            _relay_rules(pref)
        return 0
    if args[0] in ("auto", "off"):
        state.prefs["relay"] = args[0]
    else:
        state.prefs["relay"] = [a for a in args if a]
    K.save(state)
    print("pulsarcode: relay set to %s" % state.prefs["relay"])
    if env_off:
        print("pulsarcode: PULSAR_RELAY=off is set in this shell, so launches from it still run without a relay")
    else:
        _relay_rules(state.prefs["relay"])
    return 0


def quiet_broken_pipe() -> int:
    """`pulsarcode models | head`: the reader left. Point stdout at devnull so the interpreter's final
    flush does not print a second error, and exit like a shell pipeline would (141)."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass
    return 141


def _optional(module: str, func: str, args: List[str]) -> int:
    import importlib
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        print("pulsarcode: this feature is not available in this build (%s)" % exc, file=sys.stderr)
        return 2
    return int(getattr(mod, func)(args) or 0)       # a closed pipe is handled once, in main()


# v1 names of Core's tuning knobs (same meaning, seconds), read only when the v3 name is not set.
V1_TUNING = (
    ("PULSAR_NIM_STREAM_HEADER_TIMEOUT", "PULSAR_HEADER_TIMEOUT"),
    ("PULSAR_NIM_STREAM_HEADER_TIMEOUT_REASONING", "PULSAR_HEADER_TIMEOUT_REASONING"),
    ("PULSAR_NIM_FIRST_TOKEN_TIMEOUT", "PULSAR_FIRST_TOKEN_TIMEOUT"),
    ("PULSAR_NIM_FIRST_TOKEN_TIMEOUT_REASONING", "PULSAR_FIRST_TOKEN_TIMEOUT_REASONING"),
    ("PULSAR_NIM_STREAM_PING_INTERVAL", "PULSAR_PING_INTERVAL"),
)


def apply_v1_tuning(environ=None) -> None:
    """Copy a v1 PULSAR_NIM_* tuning value to its v3 name when that is unset (Core inherits the result)."""
    env = os.environ if environ is None else environ
    for old, new in V1_TUNING:
        value = (env.get(old) or "").strip()
        if value and not (env.get(new) or "").strip():
            env[new] = value


def main(argv: Optional[List[str]] = None) -> int:
    """Run one pulsarcode command. A reader that leaves a pipe early (`pulsarcode models | head`) ends it
    quietly with 141, like a shell pipeline, whether the pipe breaks mid-command or at the final flush."""
    try:
        code = _dispatch(argv)
        out = sys.stdout
        if out is not None:
            out.flush()                       # surfaces a closed pipe here, not in the interpreter's exit
        return code
    except BrokenPipeError:
        return quiet_broken_pipe()


def _dispatch(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    harden_stdio()
    from pulsar.term import enable_windows_vt
    enable_windows_vt()
    if os.environ.get("PULSAR_NO_ANIM") == "1" or os.environ.get("PULSAR_SKIP_WIZARD") == "1":
        os.environ["PULSAR_FX"] = "off"  # PULSAR_SKIP_WIZARD is the v1 spelling
    apply_v1_tuning()
    launch_only = bool(argv) and argv[0] == "--"
    if launch_only:
        argv = argv[1:]
    elif argv and argv[0] in RETIRED:
        print("pulsarcode: `pulsarcode %s` was a v1 command; now %s. Nothing was sent." % (argv[0], RETIRED[argv[0]]),
              file=sys.stderr)
        return 2
    sub = argv[0] if argv and argv[0] in SUBCOMMANDS and not launch_only else ""
    rest = argv[1:] if sub else argv
    try:
        if sub == "core":
            from pulsar.core import server
            return server.main(rest)
        if sub == "mcp":
            from pulsar.mcp import server as mcp_server
            return mcp_server.main(rest)
        if sub == "statusline":
            from pulsar import statusline
            return statusline.main(rest)
        if sub == "notify":
            from pulsar import notify
            return notify.main(rest)
        if sub == "switch":
            return cmd_switch(rest)
        if sub == "relay":
            return cmd_relay(rest)
        if sub == "top":
            return _optional("pulsar.observatory", "cmd_top", rest)
        if sub == "stage":
            return _optional("pulsar.stage.term", "cmd_stage", rest)
        if sub == "pilot":
            # No "I am human" escape for `pilot on`: arming is never allowed from inside a session, so it is
            # refused at once (before any banner) instead of offering a confirmation that cannot succeed.
            var = claude_session_var()
            if rest[:1] == ["on"] and var:
                print("pulsarcode: refusing to turn on computer use (Pilot) from inside a Claude Code session (%s "
                      "is set): the model's Bash tool could run this. Run `pulsarcode pilot on` yourself in your "
                      "own terminal, outside Claude Code." % var, file=sys.stderr)
                return 3
            return _optional("pulsar.pilot.cli", "cmd_pilot", rest)
        if sub == "pilot-overlay":
            return _optional("pulsar.pilot.overlay", "main", rest)
        if sub in ("help", "--help", "-h"):
            ui = _ui()
            print(_plain(HELP.format(mark=glyph(ui.mark(), "@"), ver=__version__, tag=TAGLINE, repo=REPO)))
            return 0
        if sub in ("version", "--version", "-V"):
            print("pulsarcode " + __version__)
            return 0
        if sub in ("key", "login", "connect", "api", "/api", "--api"):
            return cmd_key(rest)
        if sub in ("pick", "select"):
            return cmd_pick(rest)
        if sub == "model":
            return cmd_model(rest)
        if sub in ("models", "sonar"):
            return cmd_models(rest)
        if sub in ("status", "doctor"):
            return cmd_status(rest)
        if sub == "providers":
            return cmd_providers(rest)
        if sub == "use":
            return cmd_use(rest)
        if sub == "logout":
            return cmd_logout(rest)
        if sub in ("update", "upgrade"):
            return cmd_update(rest)
        if sub == "uninstall":
            return cmd_uninstall(rest)
        if sub == "demo":
            return cmd_demo(rest)
        if sub == "stop":
            return cmd_stop(rest)
        return cmd_launch(rest)
    except K.HomeError as exc:
        print("pulsarcode: %s" % safe(exc, 400), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\x1b[0m\x1b[?25h\n" if sys.stderr.isatty() else "\n")
        return 130
