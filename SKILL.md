---
name: "art-engine"
description: "Nate's art engine: generate and edit images/video, composite layered cutouts onto photos, grade/color, add text, assemble clips, and prep social-ready posts with an iterate-until-right loop. Use when Nate asks to make, edit, or iterate on visual content for Instagram or other social posts."
---

# Art Engine

## Purpose
Build social-media visuals with Nate through fast iteration: AI-generate or pull from his photos, cut out subjects, composite layered graphics, grade color, add text, assemble video, preview in chat, tweak, repeat. Nothing publishes without his explicit approval.

## Build workflow (standing — Oct 2026)
1. **Styleframes before motion.** Every new visual idea renders as stills first (`vcompose styleframe`, or ffmpeg frame extraction from a draft). Nate approves the *look* before anyone builds motion. Never show a finished video as the first look at a new visual.
2. **Asset gate.** Every cutout is verified on a checkerboard (`vcompose check-assets`) before entering a composition. White-box backgrounds never reach a render.
3. **Motion second.** Only after stills are approved. Keyframes interpolate per-frame (the 0.5s slicing that caused visible stepping was fixed Oct 2026).
4. **Ambiguities declared, never guessed.** The brief template (`BRIEF-TEMPLATE.md`) has an Ambiguities section that must be empty or explicitly user-confirmed before any render. Resolving ambiguity by guessing is a bug.

## Workflow
1. **Brief.** Get (or confirm): subject, format (story/feed/square), vibe or preset, any of his photos/videos to incorporate, text to include. Keep it light — one or two questions max, then start.
2. **Assets.** Sources, in order of preference:
   - His media: `/opt/hatch/bin/media-library` for uploaded photos; device gallery via `device` commands for phone photos/videos. Copy working files into the project dir first.
   - AI generation: `media.generate_image` / `media.generate_video` with `output_dir` set to the project's `assets/`. Chain edits with `resume_from_snapshot_id`.
   - Stock/derived: build shapes, gradients, textures with `bin/ae.py`.
3. **Build.** Compose with `bin/ae.py` (cutout, composite, grade, text, format, video ops). Every op is parameter-driven so tweaks are cheap reruns.
4. **Preview.** Show the result inline in chat every round (`![desc](sandbox://workspace/...)`). Never describe a visual without showing it.
5. **Iterate.** Nate's feedback → adjust params → new version. Keep every version: `~/workspace/art-engine/projects/<slug>/v1.png`, `v2.png`, … plus `manifest.json` recording the exact params per version so any version is reproducible.
6. **Publish (only on approval).** `instagram-cli post-story --draft` first to render the native preview, show it, then publish after he approves. Feed posts via `post-feed`. Follow the instagram skill's approval rules.

## Tooling
All in `bin/` (stateless; the agent manages versioning):
- `ingest.py` — formal video intake. `ingest <video> --out <dir>` probes the
  file and writes: `meta.json` (duration/res/fps), `keyframes/` (scene-change
  frames, timestamps in filenames), `contact.jpg` (1fps contact sheet for
  at-a-glance review), `review.json` (template the agent fills with
  timestamped beats + takeaway). `grab <video> --at <s> --out frame.png`
  pulls an exact frame; `clip <video> --from <s> --to <s> --out clip.mp4`
  cuts a clip. **Process:** every video Nate submits gets ingested, the agent
  reviews the contact sheet and writes the beats, and later frame/clip grabs
  are timestamp lookups — never scrub blind.
- `ae.py` (stateless; the agent manages versioning):
- `cutout` — chroma-key, luma-key, or shape masks (rounded-rect, ellipse) with feather → PNG with alpha. **AI subject cutout**: `~/workspace/.venvs/cutout/bin/python -m rembg` (u2net, local, ~176MB model in `~/.rembg/`) — real photos, no green screen needed; crop the alpha to the garment/subject bbox after. Replaces the old "AI onto green then chroma-key" workaround.
- `composite` — JSON layer spec: bg color/image, layers with anchor/pos, scale, rotate, opacity, blend, feather, drop shadow.
- `grade` — saturation, contrast, brightness, warmth, vignette, grain, fade, duotone, sharpness. Presets in `presets.yaml`.
- `text` — overlay with font/size/anchor/color/stroke/shadow, word wrap.
- `format` — crop/resize to story (1080x1920), feed (1080x1350), square (1080x1080).
- `kenburns` — still image → slow zoom/pan mp4.
- `concat` — join clips to common 1080x1920/30fps.
- `overlay` — PNG graphic over a video time range, with fades.
- `muxaudio` — attach an audio track to a video (e.g. his song file).

Run `bin/ae.py <op> --help` for flags. Compile-check after edits: `python3 -m py_compile bin/ae.py`.

## Video timeline (segment-cached editing)
`bin/vtimeline.py` — timelines are JSON (`project.json`): ordered segments, each rendered through a registered op (`still`, `hold`, `card`, `cutout_isolation`, …). Renders are content-addressed and cached in `~/workspace/art-engine/video-cache/`, so changing one beat re-renders only that beat; assembly concats cached segments with stream copy (no re-encode, no quality loss). `render` builds, `index` shows the timecode map + cache status, `gc` prunes unreferenced segments. Never re-render a whole video for a small change — edit the segment, re-render, reassemble.

## Video composition (layer-based NLE — Phase 1 + 3)
`bin/vcompose.py` — the composition model: the project file **is** the timeline. Schema in `composition/SPEC.md`. A composition is layers with transforms, sub-clip cuts/holds, and parameterized effect stacks, compiled to pixels in one ffmpeg graph. Nothing flattens until final render.
- **Layers** (bottom-to-top): `video` / `image` / `text`, each with `in`/`out` placement, `transform` {x, y, scale, rot, opacity}, `effects[]`, and `fit` (cover/contain/fill). Nudging a logo 4px = one number.
- **Sub-clip control**: `cuts[]` = ordered source ranges with per-cut `speed`; `holds[]` = freeze a source frame (`at_src`, `dur`, `at_cut`). This is how cookie-banner frames get excised and clothing frames held — as data, not re-renders.
- **Effects** (parameterized stacks): `glitch` {shift, noise, sat, hue, swap, tint, tint_op, hits}, `grade` {preset: vandal-raw/clean-pop/faded-film/noir}, `blur` {sigma, hits}, `dissolve` {from[], to[], at, dur, transition} (treatment-dissolve as a real effect), `rgbshift` {r/g/b as [dx,dy]}, `scanlines` {spacing, opacity}, `shake` {amp, hits}. `hits` = timeline ranges where the effect fires; outside them the layer plays clean (pre-effect stream — Oct 2026 wrapper fix).
- **Keyframes** (Phase 3): any animatable param takes a scalar or `[{t, value, ease}]` (linear/in-out/step). The degradation arc is data now: ramp glitch 0→max, step-snap to clean. Compiled per-frame (Oct 2026 fix — the old ≤0.5s slices visibly stepped motion; see SPEC.md): frame-level sampling, overlay-chain assembly.
- **Audio** (Phase 3): top-level `audio[]` {src, in/out, gain, src_in?, duck_by?} → single AAC track, sidechain ducking (bed dips under VO), muxed in.
- **Annotation rule**: logo bugs/labels are `annotate: true` layers, composited clean on top; content effects can never touch them. Annotation layers must be last.
- **Caching**: content hash of the composition JSON + source file hashes → `video-cache/vc-<key>.mp4`. `render` / `index` commands mirror vtimeline. Every render's duration is ffprobe-verified against the declared `duration`.
- Relation to vtimeline: vtimeline = the beat-level timeline (segments); vcompose = the layer composition *inside* a beat. A vtimeline segment can be a rendered composition.
- Examples: `composition/examples/b4-scroll-c.json` (EP1 B4, 7.000s); `composition/examples/kf-degradation.json` (keyframe arc); `composition/examples/kf-degradation-audio.json` (bed + ducked VO); `composition/examples/fx-showcase.json` (dissolve/rgbshift/scanlines/shake).
- Gotchas live in SPEC.md (no input seeking on phone recordings, verified 2x path, looped-input holds, explicit splits, overlay-enable for hit ranges).

## Style profiles + lint (taste as data)
`style/` — his video conventions formalized as machine-checkable rules, so taste compounds instead of evaporating. `default.yaml` holds universal gates (no-dead-viewport, no-accidental-freeze, loading-must-be-covered); `vandal-raw.yaml` is his profile (inherits default, adds annotation-stays-clean, content-carries-grade, glitch-declares-hits). Every rule carries the incident that created it + a human rationale.
- `vcompose.py lint comp.json [--profile vandal-raw]` — renders, then checks: errors exit 1 (block the render), warnings print and pass. Run before every final render.
- The loop: incident → rule → lint → fix. New "that feels off" → record it in `style/`, encode the check, lint catches it forever.
- `intentional: true` on a layer marks a deliberate beat (the "nothing." card) and exempts its range — the linter can't infer intent. Declared `holds[]` are always exempt from the freeze rule.
- Forking: copy `vandal-raw.yaml` → `<name>.yaml`, keep `inherits: default`, tune. Profiles are taste, not personal data — they ship in the public repo. Details in `style/STYLE-GUIDE.md`.

## Video board (direct-manipulation editor — Phase 2)
`bin/vboard.py` + `composition/board.html` — the Premiere-like surface. The board never touches ffmpeg; it edits the composition JSON and the bridge renders.
- **Open it**: `python3 bin/vboard.py <comp.json> [--port 8901] [--open]` → open the printed localhost URL (desktop or phone browser on the same network with `--host 0.0.0.0`).
- **Proxy preview**: 270px vcompose render shown on canvas; scrubber/playhead steps the proxy video; play button plays it.
- **Layers panel**: click to select, 👁 show/hide (`_hidden`, excluded from renders), 🔒 lock.
- **Direct manipulation**: drag the selected layer on canvas (Artboard grammar — touch-safe, window-level tracking) → transform x/y updates live, writes back on release. Nudge pad for 2px steps.
- **Inspector**: numeric/sider transform inputs (x, y, scale, rot, opacity), per-effect params (glitch: shift/noise/sat/hue/swap/tint/tint_op/hits; grade preset; blur sigma), add/remove effects, in/out placement.
- **Cuts panel** (video layers): editable cut rows (src_in/src_out/speed), "remove 1s at playhead" (splits the cut under the playhead — the cookie-banner exciser), hold rows + "hold frame at playhead". Content duration auto-normalizes layer out + comp duration.
- **Undo/redo** (60 steps), **save-on-release** (writes the comp JSON), **Render** button (full-res vcompose + download link).
- Board-only metadata (`_hidden`, `_locked`) lives in the JSON; vcompose ignores/filters it.

## Video dictionary
`video-dictionary/DICTIONARY.md` — the shared directing language for video work: 7 core moves (push-in, cutout isolation, treatment dissolve, scroll-at-speed, hard-cut card, annotation, the hold) each with a visual ref and an ffmpeg recipe, plus standard editing terms. Directing convention: name the move, the subject, the duration — e.g. "push into the shirt post, 3 seconds — treatment dissolve to outline, hold 2." The builder executes; the director never touches ffmpeg. `bin/hand_draw.py` draws the wobbly hand annotation marks (circle/arrow/underline).

## Presets
`presets.yaml` holds Nate's style presets (from his IG: raw flash-lit phone photos, heavy saturation, candid found-weirdness) and social formats. Start from a preset, then deviate per feedback. Add new presets when a look recurs — that's how the engine learns his taste.

## Operating Rules
1. Preview every version in chat. No blind tweaks.
2. Version everything; never overwrite. Manifest per version.
3. Prefer his real photos as base layers; AI for elements, textures, backgrounds.
4. Keep text minimal on images — his style lets captions do the talking.
5. Publishing needs explicit approval every time, including drafts-to-live.
6. Honest limits: no music *sticker* on stories via API (link sticker to the song, or mux audio into video files); generated video clips are ~10s each (stitch for longer); AI cutouts vary in quality — keying needs clean backgrounds; never use his likeness in ways he didn't ask for.

## Artboard widget (direct manipulation)
- Template `artboard_template.html` + `bin/build_artboard.py` produce `~/workspace/art-engine/projects/<project>/artboard.html`, shown via `widget.create` (kind `html_file`).
- Widget: drag layers on canvas, format switcher (story 1080x1920, portrait 1080x1350, square 1080x1080, landscape 1920x1080, pin 1000x1500, thumb 1280x720), scale/rotate/opacity sliders, layer reorder/add/delete, bg color.
- Layer positions are fractional (fx/fy/fw of canvas), so one arrangement reflows across formats.
- Render: `ae.py artboard --state <widget-state.json> [--format NAME | --size WxH] --out <file>`. Read state with `widget.state` after the user says "render". Renders full-res WYSIWYG.
- iOS: touchstart/touchmove use non-passive listeners + preventDefault so the chat scroll view doesn't steal drags; pointer events skip touch pointerType to avoid double-handling.
- v4: undo/redo (↶ ↷) covering all mutations via snapshot history (60 deep); stale-echo guard (`_seq` — bridge echoes arriving out of order are ignored instead of re-rendering); save-on-release for sliders/bg (live visual, persist on `change`) to cut echo races; drag uses window-level touchmove/touchend attached at gesture start.
- New boards: `bin/build_artboard.py --assets <img-folder> --project <project-dir> [--layout pile|grid] [--seed seed.json] [--covers a,b,c]` — one command per board, no template edits. Seed JSON pins exact layer arrangement/order (covers board seed: `projects/covers/seed.json`).

## vcompose styleframe + check-assets (added Oct 2026)
Two commands that make the standing workflow's first two steps first-class:

- **`vcompose styleframe comp.json -t 2.5 [-t 5.0 ...] -o frame.png`** — stills before motion. Renders the comp through the normal cached path (no re-render when nothing changed), then extracts each requested frame with output-side ffmpeg seek (frame-accurate). One `-t` writes exactly to `-o`; multiple `-t` write suffixed files in `-t` order (`-o frame.png` + `-t 1 -t 5` → `frame-1.png`, `frame-2.png`). Every new visual is approved as stills before anyone builds motion.
- **`vcompose check-assets comp.json [-o sheet.png]`** — the asset gate. For each image layer: composites over a gray checkerboard (no pure white, so transparency never reads as white), saves per-asset PNGs (`<sheet>-<id>.png`) plus a contact sheet, and prints a per-asset PASS/WARN report. Heuristic: rim pixels (outer 4% border) that are opaque AND near-white (>240 all channels); >20% → WARN = background probably not cut out (the white-box sticker failure). Nuance: sticker-style white *outlines* are intentional and pass (a few px, well under 20%); white *fill* touching edges is the bug. Opaque photos that fill the frame can WARN — that is a prompt for human review, not a verdict. Runs before any composition render.
