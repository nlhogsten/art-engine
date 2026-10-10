# Composition Spec — Phase 1 + Phase 3

The project file **is** the timeline. A composition is a JSON document
describing layers; `bin/vcompose.py` compiles it to pixels in a single
ffmpeg filter graph. Nothing flattens until final render: text stays text,
vectors stay vectors, every number stays editable.

This is the data model the future video board (Phase 2) will read and
write. The board never touches ffmpeg; it edits this JSON.

Phase 3 adds: **keyframes** (any animatable param as data over time),
**audio** (layers, mix, ducking), and a deeper **effect stack**.

## Top level

```json
{
  "width": 540, "height": 960, "fps": 30, "duration": 7.0,
  "bg": "#000000",
  "layers": [ ... ]
}
```

- `width`, `height` — canvas size in px.
- `fps` — timeline frame rate. All layer content is normalized to it.
- `duration` — total seconds. The render is exactly this long (verified
  with ffprobe after every render; a mismatch is a compiler bug).
- `bg` — canvas color (`#rrggbb`), default `#000000`.
- `layers` — bottom-to-top. Layer 0 is the back; the last layer is the
  front. **Annotation layers must be last** (the compiler warns otherwise).

## Layer

```json
{
  "id": "scroll",
  "type": "video",
  "src": "claude-dead-end.mp4",
  "in": 0.0, "out": 7.0,
  "cuts": [
    {"src_in": 16, "src_out": 20.5, "speed": 2.0},
    {"src_in": 23, "src_out": 25.5, "speed": 2.0},
    {"src_in": 29, "src_out": 32,   "speed": 2.0}
  ],
  "holds": [
    {"at_src": 28.5, "dur": 2.0, "at_cut": 1}
  ],
  "fit": "cover",
  "transform": {"x": 0, "y": 0, "scale": 1.0, "rot": 0, "opacity": 1.0},
  "effects": [
    {"type": "glitch", "shift": 34, "noise": 40, "sat": 0.35,
     "hits": [[1.2, 1.5], [3.0, 3.4], [5.8, 6.2]]}
  ],
  "annotate": false
}
```

- `id` — unique name. Used in errors and by the board.
- `type` — `video` | `image` | `text`.
  - `video`: `src` is a video file.
  - `image`: `src` is a still; held for `out - in` seconds.
  - `text`: no `src`; renders `text` with `font_size`, `color`,
    `font` (fontconfig name or path). Centered on a transparent
    full-canvas plate; move it with `transform.x/y`.
- `src` — path, relative to the composition file's directory (absolute
  also accepted).
- `in` / `out` — placement on the timeline, in seconds. The layer is
  visible only inside `[in, out]`.
- `cuts[]` — **sub-clip control**. Ordered list of source ranges that
  make up the layer's timeline, back to back. Each cut:
  - `src_in`, `src_out` — seconds in the *source* file.
  - `speed` — playback speed. `1.0` = normal. `2.0` uses the verified
    frame-decimated 2× path. Other values use a generic resampler
    (less battle-tested — verify output duration).
  - Output duration of a cut = `(src_out - src_in) / speed`.
  - Empty `cuts` = whole source at speed 1.
- `holds[]` — freeze frames. Each hold:
  - `at_src` — source timestamp of the frame to freeze.
  - `dur` — how long to hold it, in timeline seconds.
  - `at_cut` — index into `cuts[]` *after which* the hold plays.
    Default: after the last cut (hold at the end).
  - The compiler extracts the frame in a pre-pass (no input seeking —
    see gotchas) and feeds it as a looped still.
  - This is the "pause on the clothing frame" move, as data.
- `fit` — how the source maps into the layer frame (`cover` | `contain`
  | `fill`), default `cover`. The layer frame defaults to the full
  canvas; `frame: {"w":..,"h":..}` overrides it.
- `transform` — `x`, `y` (top-left px on canvas), `scale` (multiplier),
  `rot` (degrees), `opacity` (0–1). Applied after effects. This is the
  "nudge the logo 4px" surface — one number, no re-render negotiation.
- `effects[]` — ordered stack, applied to the layer's fitted frame
  *before* transform. Each effect is `{"type": ..., params...}`.
  Effects never leak onto other layers.
- `annotate` — `true` marks an annotation layer (logo bug, label, hand
  mark). Annotation layers are composited **clean, on top**, and content
  effects can never touch them. They must be the last layers in the
  array; the compiler warns if they aren't.
- `intentional` — `true` marks a deliberate beat and exempts the layer's
  `[in, out]` range from `vcompose lint` frame rules. The linter cannot
  infer intent: a blank frame you meant (the "nothing." dead-end card) must
  be declared, or it reads as a bug. Declared `holds[]` are intent made
  data and are always exempt from the freeze rule without this flag.

## Effects registry

### glitch — RGB-split glitch with color control
```json
{"type": "glitch", "shift": 34, "noise": 40, "sat": 0.35,
 "hue": 0, "swap": null, "tint": null, "tint_op": 0.25,
 "hits": [[1.2, 1.5], [3.0, 3.4]]}
```
- `shift` — channel split, px.
- `noise` — grain amount (`noise=alls=`).
- `sat` — saturation multiplier (`1.0` = unchanged, `0.35` = drained).
- `hue` — hue rotation, degrees. The color voice of the glitch.
- `swap` — channel permutation: `null` | `"bgr"` | `"brg"` | `"gbr"`.
- `tint` / `tint_op` — hex color overlay + opacity.
- `hits` — `[[t0,t1],...]` ranges (timeline seconds) where the glitch
  is visible. Outside hits the layer plays clean. Empty/missing =
  glitch the whole layer.

### grade — color preset
```json
{"type": "grade", "preset": "vandal-raw"}
```
Presets mirror `presets.yaml`: `vandal-raw`, `clean-pop`,
`faded-film`, `noir`. Implemented as `eq` chains; extend by adding to
`GRADES` in `vcompose.py`.

### blur
```json
{"type": "blur", "sigma": 8, "hits": [[0, 1]]}
```
`hits` optional, same semantics as glitch.

New effects: add a builder function in `vcompose.py` + document here.
Keep every effect a pure function of (stream, params, W, H, fps).

**Hit-range wrapper (applies to ALL effects).** When an effect carries
`hits`, the compiler splits the *pre-effect* stream: one branch stays
clean, the other runs the effect, and the two are overlaid with
`enable='between(t,a,b)+...'`. Outside hits the layer plays the clean
pre-effect stream — the effect is never visible outside its ranges.
(Oct 2026 fix: the wrapper used to split the *effected* stream, leaving
grade/blur/etc. visible at all times. Glitch was unaffected — it split
internally.)

### dissolve — treatment-dissolve as a real effect
```json
{"type": "dissolve",
 "from": [{"type": "grade", "preset": "clean-pop"}],
 "to":   [{"type": "glitch", "shift": 30, "noise": 35, "sat": 0.4}],
 "at": 2.0, "dur": 1.0, "transition": "fade"}
```
Cross-dissolves between two effect stacks via `xfade`, centered at `at`
(layer-local seconds), over `dur`. `transition` is any xfade transition
(`fade`, `fadeblack`, `fadewhite`, `wipeleft`, …). This is the
video-dictionary "treatment dissolve" move, as data — no baked
transition, fully re-parameterizable. `at`/`dur` are structural (not
keyframeable). Cannot be combined with keyframed params on the same
layer (raises a clear error); put the dissolve on its own layer.

### rgbshift — surgical channel split
```json
{"type": "rgbshift", "r": [10, 0], "g": [-6, 3], "b": [-10, 0],
 "hits": [[4.0, 5.0]]}
```
Per-channel `[dx, dy]` px offsets — refines glitch's fixed horizontal
split: all three channels, vertical too. Offsets are keyframeable as
`[dx, dy]` pairs (lerped elementwise).

### scanlines — CRT/VHS
```json
{"type": "scanlines", "spacing": 4, "opacity": 0.25,
 "hits": [[5.0, 6.0]]}
```
Horizontal scanlines every `spacing` px at `opacity`. Both keyframeable.

### shake — positional jitter
```json
{"type": "shake", "amp": 10, "hits": [[6.0, 7.0]]}
```
Random per-frame crop jitter, ±`amp` px. Deterministic (seeded RNG —
cache-safe). `amp` keyframeable. Works on video layers; on image layers
the layer should be cover-fitted first (video layers are fitted before
effects; image layers are not — size-mismatched overlay inputs can
freeze the jitter).

## Render pipeline (what the compiler does)

1. Resolve paths, probe video sources (size/fps/duration), hash them.
2. Pre-pass: extract hold frames (full decode, `select=gte(t,at_src)`,
   first frame — never input seeking on phone recordings).
3. Build **one** filter graph:
   - each video source decoded once, explicit `split` for multiple cuts;
   - cuts → trim + speed → concat → fit → effects → transform;
   - layers composited bottom-to-top via `overlay` with
     `enable='between(t,in,out)'`;
   - annotation layers last, composited clean.
4. Render to the content-addressed cache; verify duration with ffprobe.

## ffmpeg gotchas (hard-won — do not regress)

- **Never `-ss` before `-i`** on the phone screen recordings. Filters
  appear to run but durations come out wrong. Full-decode, trim
  in-filter.
- 2× speedup: `trim=start=A:end=B,setpts=PTS-STARTPTS,` +
  `select='not(mod(n\,2))',setpts=N/{fps}/TB`. Verified.
- Freeze holds: `-loop 1 -framerate {fps} -t {dur} -i frame.png`.
  (`trim=duration=` on a single-frame image stream yields ~0.03s.)
- One source → multiple trims in one graph: explicit `split`.
- Effect hit-ranges: overlay the effected full-duration stream with
  `enable='between(t,a,b)+...'`, never re-trim in-graph pads.
- Verify **every** output duration with ffprobe. Bisect on mismatch.

## Phase 3 — keyframes, audio, deeper effects

### Keyframes

Any animatable param accepts a scalar **or** a keyframe list:

```json
"effects": [
  {"type": "glitch",
   "shift": [{"t": 0, "value": 0, "ease": "in-out"},
             {"t": 6.5, "value": 34},
             {"t": 6.6, "value": 0, "ease": "step"}],
   "noise": [{"t": 0, "value": 0}, {"t": 6.5, "value": 40}],
   "sat": 0.35}
]
```

- `t` — layer-local seconds (same clock as `hits`).
- `ease` — interpolation *into* this keyframe: `linear` (default),
  `in-out` (smoothstep), `step` (hold previous value, then jump).
- Before the first keyframe the first value holds; after the last, the
  last holds.
- Animatable: transform `x/y/scale/rot/opacity`; glitch
  `shift/noise/sat/hue/tint_op`; blur `sigma`; rgbshift `r/g/b`
  (`[dx,dy]` pairs); scanlines `spacing/opacity`; shake `amp`.
- Categorical params stay scalar: grade `preset`, glitch `swap`/`tint`,
  dissolve `at`/`dur`/`transition`. Keyframing one raises a clear error.
- Compile strategy (per-frame since Oct 2026 — the ≤0.5s slice version
  visibly stepped motion twice per second and was rejected): the layer is
  split into exactly round(dur×fps) frame-level slices at keyframe
  boundaries; every keyframed param is sampled at each slice midpoint and
  baked as a constant; each frame is extracted with select, run through the
  normal constant-param effect/transform chain, stamped at its comp-timeline
  timestamp with setpts, and composited onto the running composite one
  overlay at a time (overlay chain, NOT concat — concat cannot re-timestamp
  single-frame segments: every slice emerges at pts 0). Reuses the existing
  effect/transform builders — keyframes are a sampling layer, not a second
  filter architecture. Param sampling is per-frame; pixel-level holds of
  2–3 frames can still occur from sub-pixel shifts quantizing to the pixel
  grid — that is the physical floor, not stepping.
- The Option B degradation arc is now data: ramp glitch 0→max over the
  scroll, `step`-ease snap to 0 at the Muse find. See
  `composition/examples/kf-degradation.json`.

### Audio

Top-level `audio[]` — mixed to a single AAC track and muxed in:

```json
"audio": [
  {"id": "bed", "src": "lofi.mp3", "in": 0, "out": 7, "gain": 0.25},
  {"id": "vo", "src": "vo.m4a", "in": 2, "out": 5, "gain": 1.4,
   "src_in": 0, "duck_by": "bed"}
]
```

- `src` — audio file; `src_in` — offset into the source (default 0).
- `in`/`out` — placement on the timeline; `gain` — linear multiplier.
- `duck_by` — id of another audio layer to duck under this one
  (sidechain compression: threshold 0.008, ratio 20 — the bed dips ~4dB
  while VO is present, verified by controlled A/B).
- Mix: full-decode + atrim in-filter, stereo/48k, adelay positioning,
  apad to comp duration, `amix normalize=0` + `alimiter`, AAC 160k.
- No `audio` = the old video-only command path, untouched.
- Example: `composition/examples/kf-degradation-audio.json` — lofi bed
  + VO ducked under it (bed -25.8dB mean, VO on top, verified with
  volumedetect).

## Phase 2 (not this file's problem, but the shape it must serve)

The video board reads/writes this JSON: scrubber sets a preview time,
dragging a layer writes `transform.x/y`, sliders write effect params,
the cut list is editable per clip. Proxy preview renders the same
graph at low res; full render reuses the segment cache.
