# Board TODO — Phase 3 features (vcompose supports, board doesn't expose yet)

Do NOT rebuild the board. This is the surface it needs to add, when Phase 3
gets a UI pass. vcompose.py already compiles all of it; the board just needs
to read/write the JSON.

**Status (Oct 5 2026): ALL SECTIONS IMPLEMENTED + VERIFIED.** Details per
section below. Bridge needed two small changes (keyframe-aware px scaling
in proxy `scale_comp`, audio src absolutization + duration probes) —
documented under "Bridge changes".

## Keyframes — DONE
- **Inspector**: every animatable numeric param (transform x/y/scale/rot/opacity,
  glitch shift/noise/sat/hue/tint_op, blur sigma, rgbshift r/g/b pairs,
  scanlines spacing/opacity, shake amp) has a ◆ diamond button: click to
  convert scalar→keyframes at the playhead (or bake keyframes→scalar at the
  playhead). Keyframed params show editable (t, value, ease) rows with
  per-row ease picker (linear/in-out/step), delete, and "+ key at playhead".
  Scalar inputs disable while keyframed (they show the evaluated value).
- **Timeline**: keyframe diamonds on the selected layer's lane; drag to
  retime (pointer events, touch-safe 13px targets); re-sorts on release.
- **Validation**: categorical params (grade preset, glitch swap/tint,
  dissolve at/dur/transition) show a greyed-out disabled diamond with a
  tooltip — no keyframe UI offered.
- **Preview**: every keyframe edit saves + re-renders the proxy (same
  450ms-debounced path as param changes).
- **Guards**: canvas drag and the nudge pad refuse to run when x/y are
  keyframed (status hint instead) — they can't clobber keyframe lists.
- Verified: kf-degradation.json renders 7.000s; diamonds/drag logic
  exercised via code path review + node --check (no live-browser test).

## Audio — DONE
- **New "Audio" tab + panel**: rows per audio layer — id, src (text),
  in/out, gain slider, src_in, duck_by (dropdown of other audio ids),
  source-duration readout from bridge probes, remove, "+ Add audio layer".
- **Timeline**: audio lanes below the video lanes with in/out bars;
  drag a bar to move the placement (duration preserved).
- **Preview**: proxy now includes the mixed audio track (bridge change:
  audio srcs absolutized in `scale_comp`; verified `video`+`audio`
  streams in the proxy mp4). No canvas representation, as specced.
- Verified: kf-degradation-audio.json through the board's proxy + render
  paths — 7.000s, AAC present, ducking intact.

## New effects (inspector additions) — DONE
- **dissolve**: from[]/to[] stack editors (nested effect fieldsets with
  full param rows, scalar-only, add/remove; dissolve not offered nested),
  at/dur numbers, transition dropdown (16 xfade names). **Warns** with a
  red banner when the layer has any keyframed params (vcompose rejects
  dissolve + keyframes on one layer) — warns in-panel, doesn't fail
  silently.
- **rgbshift**: three [dx,dy] pair rows, each keyframeable as pairs.
- **scanlines**: spacing + opacity sliders, both keyframeable.
- **shake**: amp slider, keyframeable; shows a hint on image layers
  (documented freeze limitation — works on video layers).
- All four added to the "Add effect" dropdown with sane defaults.

## Hits — DONE (generalized)
- Every effect row now gets the hits editor (was glitch/blur-only).
  Dissolve deliberately excluded — at/dur is its timing.

## Untouched / verified
- Board save round-trips keyframe lists, nested dissolve stacks, and
  `audio[]` without stripping (verified via POST/GET API comparison).
- All Phase 2 interactions untouched (drag, nudge, cut/hold editor,
  undo/redo, save-on-release, touch patterns); JS passes node --check.

## Bridge changes (vboard.py)
- `scale_comp`: px-denominated params now scale keyframe-aware
  (`sx_param`: scalars, keyframe lists, and [dx,dy] pairs; min-guards
  preserved for glitch shift / blur sigma / scanlines spacing).
  Covers transform x/y, glitch shift, blur sigma, rgbshift r/g/b,
  shake amp, scanlines spacing.
- `scale_comp`: audio `src`s absolutized (proxy JSON lives in ~/.cache).
- `probes()`: audio sources get `{dur}` entries for the panel readout.
- Proxy docstring notes the mixed-audio track.

## Deferred
- Live iPhone gesture test of the new controls (diamonds, audio bars) —
  same standing gap as the Phase 2 board; interaction code follows the
  proven Artboard pointer-event patterns.
- Nested dissolve stacks are scalar-only in the UI (no keyframe diamonds
  inside from/to) — vcompose-side semantics for keyframes there are
  undefined; matches the "dissolve on its own layer" guidance.
