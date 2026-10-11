# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""Minimal, dependency-free MCP (Model Context Protocol) server over stdio.

Claude Code launches it as a plugin server; it inherits the session's
environment (PULSAR_CORE, PULSAR_SESSION, ANTHROPIC_AUTH_TOKEN), so every
call acts as that session against Pulsar Core.

Tools
  stage_present   present a slide deck on Pulsar Stage
  stage_show      show one visual (card, chart, table, diagram, code, metrics, timeline, progress, markdown)
  stage_clear     clear this session's stage
  council         ask other connected models the same question, side by side
  pulsar_status   which model/provider is answering, relay health, token usage
  pulsar_switch   move this session to another connected model (asks the user first)
  pilot_*         computer use (only when enabled: `pulsarcode pilot on`)

stdout carries JSON-RPC only; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from pulsar import __version__
from pulsar.core.client import CoreClient, CoreUnavailable
from pulsar.stage import schema as SCH

SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Pulsar Code tools. Use stage_present to show the user a slide deck (architecture walkthroughs, plans, "
    "summaries, comparisons) and stage_show for a single chart, table, diagram, code panel or progress tracker. "
    "The user sees it animated in a terminal split or browser tab. Use council to get second opinions from the "
    "other models connected to this session. pulsar_status reports the live route."
)

Tool = Dict[str, Any]

STAGE_OUT = {"type": "object", "properties": {
    "id": {"type": "string"}, "kind": {"type": "string"}, "viewers": {"type": "integer"},
    "where": {"type": "string"}, "slides": {"type": "integer"}}, "required": ["id", "kind", "viewers"]}
COUNCIL_OUT = {"type": "object", "properties": {"answers": {"type": "array", "items": {
    "type": "object", "properties": {"model": {"type": "string"}, "provider": {"type": "string"},
                                     "provider_name": {"type": "string"},
                                     "ok": {"type": "boolean"}, "text": {"type": "string"},
                                     "error": {"type": "string"}, "seconds": {"type": "number"}},
    "required": ["model", "ok"]}}}, "required": ["answers"]}
STATUS_OUT = {"type": "object", "properties": {
    "route": {"type": "array", "items": {"type": "object", "properties": {
        "model": {"type": "string"}, "provider": {"type": "string"}, "provider_name": {"type": "string"}}}},
    "requests": {"type": "integer"}, "relays": {"type": "integer"}, "errors": {"type": "integer"},
    "tokens_in": {"type": "integer"}, "tokens_out": {"type": "integer"}}, "required": ["route"]}


def _text(s: str) -> Dict[str, Any]:
    return {"type": "text", "text": s}


def _provider_name(entry: Dict[str, Any], plan: Optional[List[Dict[str, Any]]] = None) -> str:
    """The provider of a plan target or council answer as the status line and WARP name it ("Groq",
    "127.0.0.1:26893"), never the raw id. A council answer carries no base_url: a custom endpoint's host
    comes from the session's plan target with the same provider and model."""
    from pulsar.statusline import provider_label
    t = dict(entry) if isinstance(entry, dict) else {}
    if t.get("provider") == "custom" and not t.get("base_url"):
        same = [p for p in plan or [] if isinstance(p, dict) and p.get("provider") == "custom"]
        hit = next((p for p in same if p.get("model") == t.get("model")), same[0] if len(same) == 1 else None)
        if hit is not None:
            t["base_url"] = hit.get("base_url") or ""
    return provider_label(t)


class ToolError(Exception):
    pass


_ID = re.compile(r"[\x21-\x7e]{1,200}")


def _safe_id(model_id: str) -> str:
    """A model id that is safe to print and to route (the providers' id rule: nothing a shell could reinterpret)."""
    from pulsar import providers as P
    check = getattr(P, "valid_model_id", None)
    ok = check(model_id or "") if check is not None else bool(_ID.fullmatch(model_id or ""))
    return model_id if ok else ""


def _resolve(state: Any, spec: str, unchecked: Optional[List[Any]] = None) -> Optional[Any]:
    """'model', 'provider:model' or 'profile:model' -> (profile, model) among connected profiles.

    `unchecked`, when given, receives (profile id, reason) for the catalogs that could not be consulted."""
    from pulsar import catalog as C
    if ":" in spec:
        head, model = spec.split(":", 1)
        for pid, prof in state.profiles.items():
            if head in (pid, prof.provider) or prof.label.lower() == head.lower():
                return prof, model
    from pulsar import config as K
    order = [p for p in [state.active] + [p for p in state.profiles if p != state.active] if p in state.profiles]
    for pid in order:
        if state.profiles[pid].model == spec:
            return state.profiles[pid], spec
    cands = []
    for pid in order:
        try:
            cands.append((pid, state.profiles[pid].resolve(), K.get_key(pid)))
        except KeyError:
            continue
    # Never bind an unknown id to the active profile: name it provider:model instead.
    pid, known = C.locate(spec, cands, unchecked=unchecked)
    if pid:
        return state.profiles[pid], spec
    if not known and state.current() is not None:
        return state.current(), spec       # no catalog exists or can be asked for: nothing to check against
    return None


def _transport_refusal(target: Any, sess: Any) -> str:
    """Why Core would refuse to send this session's requests to `target`, else "": Core's own check
    (engine.transport_refusal: credentials inside the URL, or a key over plain http to a host outside this
    machine and LAN without the launch's opt-in), made here so a switch is refused at once, with that reason."""
    from pulsar import config as K
    from pulsar import providers as P
    from pulsar.core import engine as E
    try:
        prov = E.provider_for(target)
    except KeyError:
        return ""                           # a provider Core does not know: it answers for that one itself
    if prov.route == "cloud":
        return ""                           # Claude Code reaches it by itself; Core sends nothing there
    key = K.get_key(target.profile)
    reg = P.get(target.profile.split("-")[0]) if not key else None
    for var in reg.env if reg is not None else ():    # where Core looks next: the provider's own variable
        key = key or os.environ.get(var, "").strip()
    resolved = E.Resolved(target, prov, key, target.model, prov.native_for(target.model), E.http_opt_in(sess))
    return E.transport_refusal(resolved)


class Server:
    def __init__(self, client: Optional[CoreClient], out=None) -> None:
        self.client = client
        self.out = out or sys.stdout
        self.lock = threading.Lock()
        self.tools: Dict[str, Tool] = {}
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self.protocol = SUPPORTED[0]
        self.local = threading.local()        # per tools/call: the client's progress token
        self.progress_every = 4.0
        self.viewer_opened = False
        self.closed = False
        self.workers: List[threading.Thread] = []
        self._register_core_tools()
        if os.environ.get("PULSAR_PILOT") == "1":
            self._register_pilot()

    # -- registry ------------------------------------------------------------
    def add(self, name: str, description: str, schema: Dict[str, Any],
            handler: Callable[[Dict[str, Any]], Any], annotations: Optional[Dict[str, Any]] = None,
            output: Optional[Dict[str, Any]] = None) -> None:
        tool: Tool = {"name": name, "description": description, "inputSchema": schema}
        if annotations:
            tool["annotations"] = annotations
        if output:
            tool["outputSchema"] = output
        self.tools[name] = tool
        self.handlers[name] = handler

    def _need_core(self) -> CoreClient:
        if self.client is None:
            raise ToolError("Pulsar Core is not reachable from this session (launch Claude Code with `pulsarcode`).")
        return self.client

    def _register_core_tools(self) -> None:
        self.add("stage_present",
                 "Present a slide deck to the user on Pulsar Stage (animated, in a terminal split or browser). "
                 "Use for explanations that benefit from structure: architecture, plans, reviews, comparisons. "
                 "A new deck replaces what is on stage, so put related topics in one deck, a slide each. "
                 "Each slide: title, optional subtitle, body (markdown-lite with '- ' bullets), optional code "
                 "{lang,text}, stat {value,label}, or embed (a chart/table/diagram/metrics/timeline/progress object with 'kind').",
                 {"type": "object", "properties": {
                     "title": {"type": "string"},
                     "subtitle": {"type": "string"},
                     "slides": {"type": "array", "items": SCH.SLIDE_SCHEMA, "minItems": 1, "maxItems": SCH.MAX_SLIDES}},
                  "required": ["slides"]},
                 self.stage_present, {"title": "Present on Pulsar Stage", "readOnlyHint": False, "openWorldHint": False},
                 STAGE_OUT)
        self.add("stage_show",
                 "Show one visual on Pulsar Stage. kind is one of: card {body,tone}, markdown {body}, code {code:{lang,text}}, "
                 "table {columns,rows}, chart {chart:bar|line|spark, labels, series:[{name,values}], unit}, "
                 "diagram {nodes:[{id,label}], edges:[{from,to,label}], direction:LR|TB}, metrics {metrics:[{label,value,delta,trend}]}, "
                 "timeline {events:[{when,title,detail,status}]}, progress {steps:[{title,status:pending|active|done|failed}]}. "
                 "Every kind needs its content (a progress scene without steps is refused). "
                 "Re-send a progress scene with the same id to update it live.",
                 {"type": "object", "properties": {
                     "kind": {"type": "string", "enum": [k for k in SCH.KINDS if k != "deck"]},
                     "id": {"type": "string", "description": "Reuse to update the same visual in place."},
                     "title": {"type": "string"}},
                  "required": ["kind"], "additionalProperties": True},
                 self.stage_show, {"title": "Show on Pulsar Stage", "readOnlyHint": False, "openWorldHint": False},
                 STAGE_OUT)
        self.add("stage_clear", "Clear this session's Pulsar Stage.", {"type": "object", "properties": {}},
                 self.stage_clear, {"title": "Clear Pulsar Stage", "idempotentHint": True})
        self.add("council",
                 "Ask other models connected to this Pulsar session the same question and get their answers side by "
                 "side (second opinions, cross-checking a plan, comparing approaches). The prompt goes to those "
                 "providers. Optionally name models as 'model-id' or 'profile:model-id'; by default the session's "
                 "other models are asked, or two of the provider's recommended models when only one is connected.",
                 {"type": "object", "properties": {
                     "prompt": {"type": "string"},
                     "models": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                     "system": {"type": "string"},
                     "max_tokens": {"type": "integer", "minimum": 64, "maximum": 16384}},
                  "required": ["prompt"]},
                 # Not read-only: the prompt leaves this machine for other providers and spends their quota.
                 self.council, {"title": "Ask the council", "readOnlyHint": False, "destructiveHint": False,
                                "openWorldHint": True}, COUNCIL_OUT)
        self.add("pulsar_status", "Report which provider and model are answering, relay health and token usage.",
                 {"type": "object", "properties": {}}, self.status, {"title": "Pulsar status", "readOnlyHint": True},
                 STATUS_OUT)
        self.add("pulsar_switch",
                 "Move this session to another model from the user's connected providers; it answers from the next "
                 "message on. Only use it when the user asks to switch. Call it without `model` to list what is "
                 "available. `model` is a model id, or 'provider:model-id' to pick the provider.",
                 {"type": "object", "properties": {"model": {"type": "string", "maxLength": 200}}},
                 self.switch, {"title": "Switch model", "readOnlyHint": False, "destructiveHint": False,
                               "openWorldHint": False})

    def _register_pilot(self) -> None:
        try:
            from pulsar.pilot import tools as pilot_tools
        except Exception as exc:  # pilot is optional; never break the server
            sys.stderr.write("pulsar mcp: pilot unavailable: %s\n" % exc)
            return
        for spec in pilot_tools.specs():
            self.add(spec["name"], spec["description"], spec["inputSchema"], spec["handler"], spec.get("annotations"))

    # -- tool handlers --------------------------------------------------------------
    def _maybe_open_viewer(self, client: CoreClient) -> str:
        """Put the stage on screen at the model's first scene: unless one shows it already (the Claude Code
        Stage pane counts) or PULSAR_STAGE_AUTO=0. What opens, and how (a split, or the browser without
        taking the focus), is pulsar.stage.launcher's choice; with no terminal (a scripted run) it opens
        nothing."""
        if self.viewer_opened or os.environ.get("PULSAR_STAGE_AUTO", "1") == "0":
            return ""
        self.viewer_opened = True
        try:
            info = client.get("/pulsar/stage/viewer", timeout=3)
        except CoreUnavailable:
            return ""
        if int(info.get("viewers") or 0) > 0:
            return "already on screen"
        try:
            from pulsar.stage.launcher import open_viewer
            return open_viewer(info.get("url", ""), info.get("sid", ""))
        except Exception as exc:
            sys.stderr.write("pulsar mcp: viewer launch failed: %s\n" % exc)
            return "open it with `pulsarcode stage` in another terminal"

    def _post_scene(self, scene: Dict[str, Any]) -> str:
        try:
            norm = SCH.normalize(scene)
        except SCH.SceneError as exc:
            raise ToolError("Invalid scene: %s" % exc)
        client = self._need_core()
        try:
            res = client.post("/pulsar/stage", {"scene": norm}, timeout=10)
        except CoreUnavailable as exc:
            raise ToolError("Pulsar Stage is unavailable: %s" % exc)
        where = self._maybe_open_viewer(client)
        what = ("deck '%s' (%d slides, the first on screen)" % (norm.get("title") or "untitled", len(norm["slides"]))
                if norm["kind"] == "deck" else "%s '%s'" % (norm["kind"], norm.get("title") or norm["id"]))
        tail = (" Viewer: " + where + ".") if where else ""
        viewers = int(res.get("viewers") or 0)
        text = "Shown on Pulsar Stage: %s (id %s, %s live viewer%s).%s" % (
            what, norm["id"], viewers, "" if viewers == 1 else "s", tail)
        return {"content": [_text(text)],
                "structured": {"id": norm["id"], "kind": norm["kind"], "viewers": viewers, "where": where,
                               "slides": len(norm.get("slides") or [])}}

    def stage_present(self, args: Dict[str, Any]) -> Any:
        scene = dict(args)
        scene["kind"] = "deck"
        return self._post_scene(scene)

    def stage_show(self, args: Dict[str, Any]) -> Any:
        return self._post_scene(dict(args))

    def stage_clear(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            self._need_core().post("/pulsar/stage", {"op": "clear"}, timeout=5)
        except CoreUnavailable as exc:
            raise ToolError(str(exc))
        return [_text("Pulsar Stage cleared.")]

    def council(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt is required")
        client = self._need_core()
        stop = self._progress_ticker("Asking the council")
        try:
            res = client.post("/pulsar/council", {k: v for k, v in args.items()
                                                   if k in ("prompt", "models", "system", "max_tokens")}, timeout=900)
        except CoreUnavailable as exc:
            raise ToolError("Council unavailable: %s" % exc)
        finally:
            stop.set()
        parts = []
        answers = []
        plan = self._own_plan()
        for a in res.get("answers") or []:
            name = _provider_name(a, plan)
            head = "### %s (%s, %ss)" % (a.get("model"), name, a.get("seconds"))
            parts.append(head + "\n" + (a.get("text") or "") if a.get("ok") else head + "\nERROR: " + str(a.get("error")))
            answers.append({"model": str(a.get("model") or ""), "provider": str(a.get("provider") or ""),
                            "provider_name": name, "ok": bool(a.get("ok")), "text": str(a.get("text") or ""),
                            "error": str(a.get("error") or ""), "seconds": float(a.get("seconds") or 0)})
        if len(answers) == 1 and plan and answers[0]["model"] == str(plan[0].get("model") or ""):
            # One answer, from the model that is already answering: say so, or it gets retold as a verdict.
            parts.insert(0, "Only this session's own model answered, so this is not a second opinion. To ask "
                            "others, name them in `models` or connect another provider (`pulsarcode connect`).")
        return {"content": [_text("\n\n".join(parts) or "No answers.")], "structured": {"answers": answers}}

    def _own_plan(self) -> List[Dict[str, Any]]:
        """This session's plan as dicts (base_url names a custom endpoint's host), [] when unknown."""
        try:
            from pulsar.core import state as ST
            sid = os.environ.get("PULSAR_SESSION", "")
            sess = ST.load_session(sid) if sid else None
            return [vars(t) for t in sess.plan] if sess is not None else []
        except Exception:
            return []

    def _progress_ticker(self, label: str) -> threading.Event:
        """notifications/progress while a long call runs, when the client asked for them."""
        stop = threading.Event()
        token = getattr(self.local, "progress", None)
        if token is None:
            return stop

        def tick() -> None:
            started = time.time()
            n = 0
            while not stop.wait(self.progress_every):
                n += 1
                self.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {
                    "progressToken": token, "progress": n,
                    "message": "%s (%ds)" % (label, int(time.time() - started))}})
        threading.Thread(target=tick, daemon=True).start()
        return stop

    def status(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            st = self._need_core().get("/pulsar/stats", timeout=5)
        except CoreUnavailable as exc:
            raise ToolError(str(exc))
        plan = [t for t in st.get("plan") or [] if isinstance(t, dict)]
        lines = ["Route (first answers, the rest are relay targets):"]
        for i, t in enumerate(plan):
            lines.append("  %d. %s  %s" % (i + 1, t.get("model") or "default", _provider_name(t, plan)))
        agg = st.get("session") or {}
        lines.append("Requests %s · relays %s · errors %s · tokens in %s / out %s" % (
            agg.get("requests", 0), agg.get("relays", 0), agg.get("errors", 0), agg.get("tokens_in", 0),
            agg.get("tokens_out", 0)))
        if st.get("breakers"):
            lines.append("Cooling down: " + ", ".join("%s (%ss)" % (k, v.get("open_for")) for k, v in st["breakers"].items()))
        structured = {"route": [{"model": str(t.get("model") or ""), "provider": str(t.get("provider") or ""),
                                 "provider_name": _provider_name(t, plan)} for t in plan],
                      "requests": int(agg.get("requests") or 0), "relays": int(agg.get("relays") or 0),
                      "errors": int(agg.get("errors") or 0), "tokens_in": int(agg.get("tokens_in") or 0),
                      "tokens_out": int(agg.get("tokens_out") or 0)}
        return {"content": [_text("\n".join(lines))], "structured": structured}

    def switch(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        from pulsar import catalog as C
        from pulsar import config as K
        from pulsar import launch as L
        from pulsar.core import state as ST
        sid = os.environ.get("PULSAR_SESSION", "")
        sess = ST.load_session(sid) if sid else None
        if sess is None:
            raise ToolError("This session was not started with `pulsarcode`, so it cannot switch models.")
        state = K.load()
        want = str(args.get("model") or "").strip()
        if not want:
            lines = ["Connected providers and models (pass one as `model`):"]
            for pid, prof in state.profiles.items():
                ids = [m.id for m in (C.read_cache(pid, ttl=10 * 365 * 86400) or [])][:25]
                lines.append("- %s (%s): %s" % (pid, prof.provider, ", ".join(_safe_id(i) for i in ids if _safe_id(i))
                                                or prof.model or "no cached models"))
            lines.append("Current: " + " -> ".join(t.model for t in sess.plan))
            if sess.side_channel:
                lines.append("Note: " + ST.side_channel_note(sess) + "; use Claude Code's /model instead.")
            return [_text("\n".join(lines))]
        if not _safe_id(want):
            raise ToolError("That is not a valid model id.")
        head, colon, tail = want.partition(":")
        if colon and head and not tail.strip():
            raise ToolError("Name a model after %s: (for example %s:<model-id>), or call pulsar_switch without "
                            "arguments to list models." % (head, head))
        unchecked: List[Any] = []
        found = _resolve(state, want, unchecked)
        if found is None:
            why = C.unlisted_message(want, list(state.profiles), unchecked)
            raise ToolError(why[:1].upper() + why[1:]
                            + " Call pulsar_switch without arguments to list models. Nothing was changed.")
        prof, model = found
        if not _safe_id(model):
            raise ToolError("That is not a valid model id.")
        if sess.side_channel:
            # Claude Code talks to this session's provider itself (cloud or direct route): a new plan would
            # change nothing it sends, so never report a switch that does not happen.
            note = ST.side_channel_note(sess)
            raise ToolError("%s. Use Claude Code's /model, or relaunch with this model: pulsarcode model %s:%s, "
                            "then pulsarcode. Nothing was changed." % (note[:1].upper() + note[1:], prof.id, model))
        try:
            prov = prof.resolve()
        except KeyError:
            raise ToolError("Unknown provider for %s." % prof.id)
        # The same refusals as `pulsarcode switch`: a key never goes to YOUR-RESOURCE..., and a Core
        # session is never moved to a target Core cannot serve (it would refuse every later message).
        why = C.placeholder_problem(prov)
        if not why and prov.route == "cloud" and not sess.side_channel:
            why = ("%s is used by Claude Code itself, not through Pulsar Core, so this session cannot switch to it. "
                   "Relaunch with that profile instead: pulsarcode use %s, then pulsarcode. Nothing was changed."
                   % (prov.name, prof.id))
        if not why:
            from pulsar.core import engine as E
            # The session's own plain-http opt-in (recorded at its launch), not this process's environment.
            opted = dict(os.environ, PULSAR_ALLOW_HTTP="1") if E.http_opt_in(sess) else None
            why = L.transport_problem(prov, K.get_key(prof.id), opted)
        if why:
            raise ToolError(why)
        m = next((x for x in C.read_cache(prof.id, ttl=10 * 365 * 86400) or [] if x.id == model), None)
        target = ST.Target(prof.id, prof.provider, model, prof.base_url, prof.native,
                           (m.context if m else 0) or C.guess_context(model))
        refusal = _transport_refusal(target, sess)
        if refusal:
            raise ToolError("Not switched: %s" % refusal)
        # The relays it had stay only where the relay rules allow them (never behind a local or custom
        # model, never in a session with Pilot armed).
        plan = L.switch_plan(state, sess.plan, target, pilot=bool(sess.flags.get("pilot")))
        if not ST.update_plan(sid, plan[:5]):
            raise ToolError("Could not update this session.")
        return [_text("Switched this session to %s on %s. It answers from the next message on; the relay chain is "
                      "%s." % (model, prof.label, " -> ".join(t.model for t in plan[1:5]) or "empty"))]

    # -- JSON-RPC ----------------------------------------------------------------------
    def send(self, msg: Dict[str, Any]) -> None:
        # ASCII JSON on the raw byte stream: independent of the locale and the Windows console code page.
        line = json.dumps(msg, ensure_ascii=True) + "\n"
        with self.lock:
            if self.closed:
                return
            try:
                buf = getattr(self.out, "buffer", None)
                if buf is not None:
                    buf.write(line.encode("ascii"))
                    buf.flush()
                else:
                    self.out.write(line)
                    self.out.flush()
            except (OSError, ValueError):
                self.closed = True       # Claude Code went away

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method is None:
            return None  # a response to something we never send
        if method == "initialize":
            wanted = str(params.get("protocolVersion") or SUPPORTED[0])
            self.protocol = wanted if wanted in SUPPORTED else SUPPORTED[0]
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": self.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pulsar", "title": "Pulsar Code", "version": __version__},
                "instructions": INSTRUCTIONS}}
        if mid is None:
            return None  # notifications (initialized, cancelled, ...)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            tools = list(self.tools.values())
            if not self._structured():
                tools = [{k: v for k, v in t.items() if k != "outputSchema"} for t in tools]
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
        if method == "tools/call":
            name = str(params.get("name") or "")
            handler = self.handlers.get(name)
            if handler is None:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool " + name}}
            args = params.get("arguments") or {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            self.local.progress = meta.get("progressToken")
            try:
                out = handler(args if isinstance(args, dict) else {})
                if isinstance(out, dict):
                    result: Dict[str, Any] = {"content": out["content"], "isError": False}
                    if out.get("structured") is not None and self._structured() and "outputSchema" in self.tools[name]:
                        result["structuredContent"] = out["structured"]
                    return {"jsonrpc": "2.0", "id": mid, "result": result}
                return {"jsonrpc": "2.0", "id": mid, "result": {"content": out, "isError": False}}
            except ToolError as exc:
                return {"jsonrpc": "2.0", "id": mid, "result": {"content": [_text(str(exc))], "isError": True}}
            except Exception as exc:
                sys.stderr.write(traceback.format_exc())
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [_text("Pulsar tool %s failed: %s" % (name, type(exc).__name__))], "isError": True}}
        if method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}

    def serve(self, inp=None) -> None:
        if inp is None:
            raw = getattr(sys.stdin, "buffer", None)
            inp = (b.decode("utf-8", errors="replace") for b in raw) if raw is not None else sys.stdin
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            batch = msg if isinstance(msg, list) else [msg]
            for m in batch:
                if not isinstance(m, dict):
                    continue
                if m.get("method") == "tools/call":
                    # Long calls (council, pilot) must not block pings or other calls.
                    t = threading.Thread(target=lambda mm=m: self._reply(mm), daemon=True)
                    self.workers = [w for w in self.workers if w.is_alive()] + [t]
                    t.start()
                else:
                    self._reply(m)

    def _structured(self) -> bool:
        """structuredContent / outputSchema exist from protocol 2025-06-18 on."""
        return self.protocol >= "2025-06-18"

    def drain(self, timeout: float = 2.0) -> None:
        """After stdin closes: give in-flight calls a moment, then stop writing for good."""
        end = time.time() + timeout
        for t in list(self.workers):
            t.join(max(0.0, end - time.time()))
        with self.lock:
            self.closed = True

    def _reply(self, m: Dict[str, Any]) -> None:
        resp = self.handle(m)
        if resp is not None:
            self.send(resp)


def main(argv: Optional[List[str]] = None) -> int:
    srv = Server(CoreClient.from_session_env())
    try:
        srv.serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    srv.drain()
    pilot_tools = sys.modules.get("pulsar.pilot.tools")
    if pilot_tools is not None:
        try:
            pilot_tools.shutdown()       # the cursor overlay goes, X11 gets back the keycodes typing borrowed
        except Exception:
            pass
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    # Daemon threads may still hold stdout's lock; a normal interpreter shutdown can then abort
    # (SIGABRT, a crash dialog on macOS). Nothing is left to say, so leave immediately.
    os._exit(0)
