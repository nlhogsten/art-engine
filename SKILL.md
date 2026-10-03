---
name: "art-engine"
description: "Nate's art engine: generate and edit images/video, composite layered cutouts onto photos, grade/color, add text, assemble clips, and prep social-ready posts with an iterate-until-right loop. Use when Nate asks to make, edit, or iterate on visual content for Instagram or other social posts."
---

# Art Engine

## Purpose
Build social-media visuals with Nate through fast iteration: AI-generate or pull from his photos, cut out subjects, composite layered graphics, grade color, add text, assemble video, preview in chat, tweak, repeat. Nothing publishes without his explicit approval.

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
All in `bin/ae.py` (stateless; the agent manages versioning):
- `cutout` — chroma-key, luma-key, or shape masks (rounded-rect, ellipse) with feather → PNG with alpha. For hard cutouts, AI-edit the subject onto a solid green background first, then chroma-key.
- `composite` — JSON layer spec: bg color/image, layers with anchor/pos, scale, rotate, opacity, blend, feather, drop shadow.
- `grade` — saturation, contrast, brightness, warmth, vignette, grain, fade, duotone, sharpness. Presets in `presets.yaml`.
- `text` — overlay with font/size/anchor/color/stroke/shadow, word wrap.
- `format` — crop/resize to story (1080x1920), feed (1080x1350), square (1080x1080).
- `kenburns` — still image → slow zoom/pan mp4.
- `concat` — join clips to common 1080x1920/30fps.
- `overlay` — PNG graphic over a video time range, with fades.
- `muxaudio` — attach an audio track to a video (e.g. his song file).

Run `bin/ae.py <op> --help` for flags. Compile-check after edits: `python3 -m py_compile bin/ae.py`.

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
