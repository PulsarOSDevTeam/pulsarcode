# Demo recordings

The terminal replays in the README and on the site are recordings of the real Pulsar Code terminal
UI, not mockups. `record.py` drives a command in a pseudo-terminal, emulates the screen (and its
cursor) with pyte, and `svgterm.py` writes an animated SVG for GitHub READMEs: the recorded frames
toggled with `steps(1,end)`, wrapped in the brand chrome (REPLAY and DEMO chips, a timecode), a
camera track, a cursor placed from pyte's cursor, an Enter thud, captions in a letterbox below the
screen, a spectrum progress hairline and a reduced-motion state (the last frame, camera at 1.0).
`make_all.sh` sets up everything each scene needs, records it and tears it down again.

```sh
tools/demo/make_all.sh                 # every scene: assets/demo/<name>.svg, docs/demo/<name>.json,
                                       # assets/demo/posters/<name>.png (+ docs/demo/posters/ copies)
tools/demo/make_all.sh picker stage    # only these scenes
tools/demo/make_all.sh --out /tmp/try  # somewhere else (useful while iterating)
tools/demo/make_all.sh --raw /tmp/raw  # keep the untrimmed recordings; re-render one without recording:
                                       #   scenes/fixtures/record_scene.py scenes/top.json /tmp/try/top --from-raw /tmp/raw/top.raw.json
tools/demo/make_all.sh --gif           # GIFs for release media, into $PULSAR_LAUNCH_DIR/demo (never the repo)
tools/demo/make_all.sh --keep top      # keep the scratch homes and logs, print their path
tools/demo/frames.py assets/demo/session.svg --at 0,6.2,12 --sheet /tmp/s.png   # exact frames for review
tools/demo/frames.py assets/demo/session.svg --cursor 10                        # cursor vs recording
tools/demo/screens.py                  # assets/screens/{boot,observatory,stage-*}.png from docs/demo/*.json
```

Where things live: SVG replays in `assets/demo/`, frame JSON once in `docs/demo/` (the site player
reads it; schema `cols`, `rows`, `frames[{t, rows}]` plus optional `title`, `alt`, `demo`,
`cursor`, `camera`, `captions`, `enters`, `hold_last`), posters in `assets/demo/posters/` and
`docs/demo/posters/`. GIFs and frame JSON are never written to `assets/demo/`.

Budgets (the build fails when a scene cannot meet them at 8 frames; `record_scene.py` picks the
largest frame count that fits): each SVG at most 650 KB raw and 120 KB gzip (session 600 KB),
16000 elements, `max_frames` at most 60 (48 for stage and top), posters at most 90 KB. After
recording, `make_all.sh` runs `tools/brand/check.py` over `assets/demo/`.

Requirements: a Python with Pulsar Code's runtime dependencies from `requirements.txt` (Starlette,
uvicorn, httpx; `PULSAR_PYTHON`, default `.venv/bin/python` or `python3`), pyte, Pillow and
Playwright with a Chromium for the posters (`DEMO_PYTHON`, default `PULSAR_PYTHON`), `openssl`, and
Claude Code (`claude`) on PATH for the session scene. The SVG draws block, braille, dot and
box-drawing characters as geometry and pins text runs to the cell grid, so it does not depend on
the viewer's fonts.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pyte pillow playwright
```

## Scenes

| Scene | Command recorded | What it shows |
|---|---|---|
| `boot` | `pulsarcode demo` | The boot sequence: wordmark decrypt, beams, the SYSTEMS panel |
| `uplink` | `pulsarcode key` | Typing an obviously fake OpenRouter-shaped key: provider detection, handshake, LINK ESTABLISHED with the catalog count |
| `picker` | `pulsarcode pick` | The model picker over seeded catalogs from three providers: search "kimi", move, Enter |
| `session` | `pulsarcode` in a small project | Claude Code launched through Pulsar Core: a question, a Read tool call, a streamed answer, the status line |
| `stage` | `pulsarcode stage --sid ...` | The Stage viewer receiving a scripted five slide sample deck (DEMO) and paging through it; the deck is posted when the viewer shows its idle screen and the replay starts on its first fully decoded title frame (legible at frame 0 and at every loop restart) |
| `top` | `pulsarcode top` | The Observatory over three sessions of synthetic traffic: relays, an open circuit breaker, a Stage event |

Each `scenes/<name>.json` uses `record.py`'s scene format (cols, rows, cmd, env, keys with timings,
`type` for humanised typing, duration, fps, max_frames, title, hold_last). Record them through `make_all.sh`, not with `record.py`
directly: the scenes need the scratch homes, fixtures and variables (`DEMO_PROJECT`, `DEMO_SID`,
`DEMO_STATE`) that `make_all.sh` sets up, and `record.py` passes your whole environment (real `HOME`,
`~/.pulsarcode`, shell credentials) to the recorded command. `scenes/fixtures/record_scene.py`, which
`make_all.sh` uses, adds a few keys on top of the format:

- `sync`: `[[AT, "TEXT"], ...]`; keys timed from AT on are sent relative to the moment TEXT
  really appears on screen.
- `start_at`, `keep_until_last`, `cut_before`, `end_after: ["TEXT", S]`: trim on what is on screen rather than on time.
  Trailing blank frames (a program clearing the screen on exit) are always dropped, so no asset
  ends, loops or shows its reduced-motion still on an empty screen.
- `alt`: the SVG's accessible name (`aria-label` and `<title>`). `title` stays the short window
  caption drawn in the frame.
- `budget`: lower `max_frames` until the SVG fits (`svg_kb`, `gzip_kb`, `elements`).
- `type`: `[[AT, "text\\r"], ...]` typed with a seeded human rhythm (55 ms lognormal base, bigrams,
  pauses after spaces and before punctuation, a 420 ms think before Enter); the expanded keys are
  written back as `typed_keys` so a recording is reviewable.
- `demo`: a DEMO chip in the chrome on every frame (demo upstream, fixtures or synthetic traffic).
- `glow`: geometry glow under bright block glyphs (boot only).
- `camera`: `[[T, ZOOM, COL, ROW, "GLIDE"|"SETTLE"], ...]`, zoom at most 1.8; the build fails if a
  zoom would hide a row with an error. Every zoomed hold is framed so its edges fall on whitespace
  for every frame shown during the hold (the renderer searches the centre within 6 columns and 3
  rows and the zoom down to 92 % of the ask): an edge may cut rules and box lines, never a word,
  and never splits a phrase at a single space. A hold that cannot be framed that way fails the
  build, so a scene asks for a zoom the screen can actually hold.
- `crop`: `{"top": N, "must_contain": "TEXT"}` drops the top N rows of every frame (a framing choice,
  not an edit). The build fails unless the cropped rows hold TEXT in every frame and no error text.
  The session scene crops Claude Code's welcome banner (its mascot and version line), so the replay
  opens on Pulsar's own lines and shows no Anthropic mark. Every recording is also checked for the
  mascot's glyph rows (`svgterm.CLI_MASCOT`): a kept frame that still shows it fails the build, and
  `tests/test_demo_brand.py` scans the shipped JSON, the SVG geometry and the PNGs for it.
- `colour_step`: colour precision of one replay (default 8 per channel). Boot uses 16: its wordmark
  and beams carry one colour per cell, and the coarser step merges neighbours into fewer paths, so
  more frames fit the budget (boot shows 21 frames, about 9 fps, instead of 15).
- `lead`: seconds the replay opens on its last frame (the composed poster) before a 300 ms dip to
  the background and the first frame. The loop is rotated, so it starts and ends on the same
  frame and nobody lands on a half-drawn opening (boot uses it: its first frames are decode noise).
- `captions`: `[[T0, T1, "caption", "must_show", {"poster": true}], ...]`; the build fails unless
  `must_show` is on screen for the whole time the caption shows. Captions describe, never claim.

Times in `camera` and `captions` may be anchors resolved on the recording: `"enter"`,
`"first:TEXT"`, `"last:TEXT"`, `"end"`, optionally `[ANCHOR, OFFSET]`; `COL` may be `"at:TEXT"`.
Anchors resolve on the frames the replay actually keeps (after `max_frames` and the budget), and
the frames an anchor or a caption's `must_show` lands on are always kept, so a caption can never
start on a frame that does not show its text yet.

## Rendering cost

An SVG inside `<img>` is repainted whole whenever anything in it changes, and Chromium re-lays out
every SVG text under a transform whose scale changes. So the renderer keeps the browser idle
between changes: every hold in a keyframe track is `steps(1,end)` (nothing interpolates while
nothing moves), the progress hairline steps only when the screen changes or the timecode ticks,
the cursor blink is written out as steps inside the replay's timeline, frames shown while the
camera holds still sit in groups with a fixed transform, and a frame shown during a camera move
carries that move itself (sampled at 30 Hz), so only the visible frame re-lays out while the
camera glides. Measure with Playwright: rAF rate and main-thread busy time of the SVG in an
`<img>` at 860 px.

## Determinism

A replay is a real-time recording of real programs, so two runs are not byte-identical: spinners,
decode noise and particle fields differ, upstream timings ("Crunched for 4s", "ttft 524ms") vary,
Core's session ids are random, and the synthetic traffic in `top` lands differently. What is pinned
from run to run:

- frame timestamps: each sample is stamped with its slot on the scene's fps grid, not the poll's wall
  clock, so two runs of a scene land on the same timestamps;
- the wall clock: every process of a scene (Claude Code, Core, the viewers, the fixtures) shares one
  clock from `fixtures/pinclock.c` that reads 09:41:00 UTC when the scene starts, so the Stage and
  Observatory headers, the feed and "done 9:41 AM" read the same on every run. `record_scene.py`
  fails a scene whose frames show any other time of day (`clock_problems`);
- Core's address: the scenes run Core on its default port 4747 when it is free (make_all.sh says
  when it is not);
- the scene direction (typed keys are seeded and written back as `typed_keys`; camera and caption
  anchors, and the Stage deck post, resolve on screen events, not on timers), the screens shown and
  their order, the captions and their must-show validation;
- the budgets: frames are fitted into 95 % of the SVG budget, so a rerun never lands on its edge.

A scene that fails is recorded once more before the run gives up on it. Review a re-recording with
`frames.py` contact sheets rather than a byte diff. `--out DIR` renders the posters from the SVGs
it just recorded into DIR and runs `tools/brand/check.py` on DIR too.

A scene whose anchor text never appears fails (exit 1) and writes the last screen to
`<out>.failed.txt` instead of replacing a good asset with a recording of the wrong screen.

## Fixtures

| File | Role |
|---|---|
| `fixtures/upstream.py` | A scripted stand-in for the providers (stdlib only). One loopback port is both an OpenAI-compatible server and the HTTPS proxy the scenes see; it terminates TLS for the provider hosts with a throwaway CA and refuses every other host. `--mode agent` scripts the session turn, `--mode traffic` serves variable latency, rate limits and one unhealthy provider |
| `fixtures/seed.py` | Seeds a scratch `PULSAR_HOME`: profiles with fake keys, catalog caches parsed by Pulsar's own code, an isolated Claude Code profile that already trusts the demo project. `seed.py core` starts that home's Core and registers sessions |
| `fixtures/post_scene.py` | Posts a Stage scene to Core as a session, the way the pulsar MCP server does |
| `fixtures/stage_scenes.json` | The deck and progress scenes |
| `fixtures/traffic.py` | Synthetic Claude Code requests into Core for the Observatory |
| `fixtures/orbit/` | The tiny project the session scene opens |
| `fixtures/pinclock.c` | An `LD_PRELOAD` shim that pins the wall clock of the processes named in `DEMO_PIN_EXE` (Claude Code and Pulsar's Python) to one shared clock reading 09:41:00 UTC at scene start (`DEMO_PIN_ORIGIN`); monotonic clocks untouched |
| `fixtures/pinned_python`, `fixtures/pinned_boot.py` | The `PULSAR_PYTHON` the scenes run on: the launcher's bootstrap with the hardware probe pinned to an illustrative "Demo machine · 8 cores · 16 GB · no GPU", so no asset publishes the recording machine's hardware and the boot asset is the same wherever it is recorded |

## Safety

These guarantees hold when the scenes are recorded through `make_all.sh`, which is the only
supported way to run them.

- Every scene runs under `env -i` with its own scratch `HOME` and `PULSAR_HOME` in a temporary
  folder. Your `~/.claude`, `~/.pulsarcode` and shell credentials are never read; `seed.py`
  refuses to run against the account's real `~/.pulsarcode`.
- The keys are obviously fake (`sk-or-v1-fake-key-not-real`, which the uplink scene types and
  shows masked, and the seeded `gsk_fake_...`, `nvapi-fake-...` profiles). The demo
  upstream accepts only keys containing "fake", so a real key pasted by mistake is rejected and
  never forwarded. Nothing leaves the machine.
- Every process a run starts (upstream, Core, session holders, traffic) is stopped by PID on exit,
  also after a failure or Ctrl-C. Running the script again overwrites the outputs and nothing else.

## Notes for the README

Recordings show Claude Code's own interface (prompt, transcript, tool rows), cropped below its
welcome banner, so no replay shows Anthropic's mascot or a Claude Code version line. Captions must describe Pulsar Code as running the
official Claude Code CLI on any model, without "Claude Code x" lockups or anything that suggests
Anthropic endorsement, and must carry the disclaimer:

> Claude and Claude Code are trademarks of Anthropic PBC. Pulsar Code is independent and not
> affiliated with or endorsed by Anthropic. Anthropic does not support routing Claude Code to
> non-Claude models; a Claude Code update can change behavior.

Everything in the recordings that looks like data is illustrative, not a measurement or a real
catalog:

- Model names and catalog counts come from the demo stub (`fixtures/upstream.py`), not from the
  providers. The stub's OpenRouter catalog has 19 models, so `uplink` ends on "OpenRouter · 19
  models online" while its own detection line says "hundreds of models behind one key", and the
  picker counts the three seeded catalogs together. The counts disagree between scenes by design;
  never quote them as real numbers.
- The boot panel's values ("312 models · demo", ":4747") are canned in `pulsarcode demo`, which
  labels the screen DEMO MODE, and its HARDWARE row is the pinned "Demo machine" (see
  `fixtures/pinned_python`).
- Throughput, latency, relays and breakers in `top` come from synthetic traffic (`fixtures/traffic.py`).

Any caption that mentions these numbers must say they come from a demo stub.

Posters: the README's `<details>` table shows `assets/demo/posters/<name>.png` (the last frame)
linking to the animated SVG; the site player shows `docs/demo/posters/<name>.png` before it loads
the frames.

Alt text: embed each animation with a sentence that says what it shows, not just the command,
for example `<img src="assets/demo/picker.svg" alt="Pulsar Code model picker: searching kimi
across three providers' catalogs, moving the selection and choosing a model">`. Each scene's
`alt` key holds that sentence (it is also the SVG's own accessible name); reuse it.
