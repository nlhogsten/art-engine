# Video Dictionary

Shared directing language for the art engine's video work. When Nate says
"push in, then cutout isolation," this is the contract for what gets built.

Two tiers:

- **Core grammar** — the moves we actually use. Every entry has a visual
  reference (`refs/`) and a recipe (ffmpeg) so anyone can rebuild it from
  their own media.
- **Standard terms** — the wider editing vocabulary. Definitions only, so we
  can borrow precisely when we need to.

Note: `refs/` clips are rendered from the director's own media and stay
local. The public repo carries this text plus the recipes.

---

## Core grammar

### 1. Push-in / pull-back

- **What:** A slow zoom into (or out of) a still image.
- **Says:** "Look closer." / "Step back, see the context."
- **Use when:** Opening on evidence, moving from a scene to a detail, or
  releasing tension after a close read.
- **Recipe:** `zoompan=z='1+0.5*on/90':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=90:s=540x960:fps=30`
  (reverse the `z` expression for pull-back)
- **Ref:** `refs/push-in.mp4`

### 2. Cutout isolation

- **Aka:** isolation fade.
- **What:** Everything in the frame dissolves away except the subject, which
  remains as a clean extracted cutout — as if it were traced and lifted in
  Photoshop while the world falls out from under it.
- **Says:** "This is the thing. Everything else was context."
- **Use when:** A single object is the payoff of a search or a story beat.
- **Recipe:** AI subject cutout (`rembg`/u2net, crop to subject, feather
  alpha) → hold cutout on black → dip-to-black in from the previous scene →
  fade the cutout up → subtle breathing zoom (`z='1+0.06*on/N'`).
- **Ref:** `refs/cutout-isolation.mp4`

### 3. Treatment dissolve

- **What:** The photograph cross-dissolves into a graphic treatment of
  itself — outline/edge-detect, duotone, high-contrast.
- **Says:** "We're not looking *through* this image anymore. We're examining
  it as evidence."
- **Use when:** Shifting from story to analysis, or freezing a detail for
  inspection.
- **Recipe:** `edgedetect=low=0.1:high=0.4` (outline) or `curves`/`colorbalance`
  (duotone), then `xfade=transition=fade:duration=1` into the treated version.
- **Ref:** `refs/treatment-dissolve.mp4`

### 4. Scroll-at-speed

- **What:** A screen recording (chat, feed, search) played fast — the scroll
  itself becomes the shot.
- **Says:** "Labor happened. Time passed. I did the work so you don't have
  to watch all of it."
- **Use when:** Dead ends, research montages, anything repetitive that proves
  effort.
- **Recipe:** crop-window animation over a tall capture —
  `crop=W:H:x=0:y='(ih-H)*n/N'` — or `setpts=0.25*PTS` on a real recording.
- **Ref:** `refs/scroll-at-speed.mp4`

### 5. Hard-cut card

- **What:** A few words on black (or paper). No fade in, no fade out — it
  slams in and slams out.
- **Says:** The punchline. Silence + text hits harder than any effect.
- **Use when:** The line the whole beat was building toward. One sentence max.
- **Recipe:** `drawtext=...:enable='between(t,1,2)'` on black. Hard cuts only —
  never fade a card.
- **Ref:** `refs/hard-cut-card.mp4`

### 6. Annotation

- **What:** A hand-drawn mark — circle, arrow, underline — drawn onto the
  evidence in the zine voice (hot pink, wobbly, double-tracked).
- **Says:** "Notice this." In *his* hand, not a UI highlight.
- **Use when:** Pointing at the detail that matters inside a busy frame.
- **Recipe:** `bin/hand_draw.py` — `hand_circle()`, `hand_arrow()`,
  `hand_underline()`. Parametric wobble, two passes, the second never quite
  closes. A perfect ellipse reads as plotted; wobble reads as drawn.
- **Ref:** `refs/annotation.png`

### 7. The hold

- **What:** Deliberate stillness. A static frame, no movement, for 2–4 seconds.
- **Says:** "Sit with this."
- **Use when:** After the cutout isolation, after a card — wherever the
  voiceover needs room to land. Stillness is a move, not the absence of one.
- **Recipe:** loop a single frame, no zoompan. Resist the urge to add motion.
- **Ref:** `refs/the-hold.mp4`

---

## Standard terms

- **Match cut** — cut between two shots matched on shape, motion, or idea
  (the shirt graphic → the lei pattern). The thinking person's transition.
- **Jump cut** — cutting forward within the same shot. Says urgency or
  "I'm skipping the boring part."
- **Smash cut** — abrupt cut, usually to something loud or contrasting after
  quiet. The shock move.
- **J-cut** — the next shot's audio starts before the picture cuts. Pulls the
  viewer forward.
- **L-cut** — the current shot's audio lingers over the next picture. Lets a
  line land while the eye moves on.
- **Cross dissolve** — one shot melts into the next. Says time passing or a
  soft connection. Overuse reads as slideshow.
- **Dip to black** — fade out to black, beat, fade up on the next shot. A
  full stop between ideas; the backbone of the cutout-isolation transition.
- **Fade in / fade out** — from/to black (or white) at the head or tail.
- **Freeze frame** — motion stops dead on a single frame. "Remember this
  face/moment."
- **Speed ramp** — speed changes *within* a shot (slow → fast). Energy shift
  without cutting.
- **Ken Burns** — slow pan/zoom over a still, named for the documentary
  style. Differs from an intentional push-in only in intent: Ken Burns is
  wallpaper motion; a push-in is a directed look.
- **Lower third** — text/graphic in the lower third of frame (name, handle,
  date). Keep it minimal or skip it.
- **B-roll** — illustrative footage laid over the main audio. Our screen
  recordings and stills *are* the b-roll.
- **Cutaway** — a brief shot away from the main subject, then back. A
  reaction, a detail, the phone screen.
- **Montage** — a sequence compressing time or effort (the dead-end scrolls,
  sped up, back to back).
- **Whip pan** — an extremely fast pan, usually as a transition. Energy and
  disorientation; use sparingly.

---

## Directing convention

Name the move, name the subject, name the duration:

> "push into the shirt post, 3 seconds — treatment dissolve to outline, hold
> 2 — hard-cut card: 'she didn't know.'"

The builder executes; the director never touches ffmpeg.
