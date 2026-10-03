# art-engine

A image/video toolkit for social content production: AI generation, cutouts, layered compositing, color grading, text overlays, ffmpeg assembly — plus a drag-and-drop **Artboard** for arranging layers by hand. Ships as a **Muse skill** (`SKILL.md`) so any Muse agent can load and operate it, with plain scripts underneath for humans.

Built for a real Instagram content pipeline (Oct 2026). Not a demo.

## Two ways to use it

**If you're a human:** run the scripts directly. Everything is parameter-driven:

```bash
bin/ae.py <op> --help
```

**If you're a Muse agent:** load `SKILL.md`. It's the complete interface — workflow, tooling, presets, operating rules, and the Artboard protocol. Point it at a project directory and go.

## Requirements

- `python3` (+ `PIL`/`pillow`: `pip install pillow`)
- `ffmpeg`
- API keys for AI image/video generation go in **environment variables, never in files**

## Quickstart

```bash
git clone https://github.com/nlhogsten/art-engine.git
cd art-engine
pip install pillow

# cut out a subject (chroma/luma/shape masks)
bin/ae.py cutout --help

# composite layers from a JSON spec
bin/ae.py composite --help

# grade with a preset, add text, format for social
bin/ae.py grade --preset vandal-raw --input in.jpg --out out.jpg
bin/ae.py text --help
bin/ae.py format --format story --input out.jpg --out story.jpg

# still -> slow zoom/pan clip, join clips, overlay graphics, attach audio
bin/ae.py kenburns --help
bin/ae.py concat --help
bin/ae.py overlay --help
bin/ae.py muxaudio --help
```

## Ops

| op | what it does |
|---|---|
| `cutout` | chroma-key, luma-key, or shape masks (rounded-rect, ellipse) with feather → PNG with alpha |
| `composite` | JSON layer spec: bg color/image, layers with anchor/pos, scale, rotate, opacity, blend, feather, drop shadow |
| `grade` | saturation, contrast, brightness, warmth, vignette, grain, fade, duotone, sharpness |
| `text` | overlay with font/size/anchor/color/stroke/shadow, word wrap |
| `format` | crop/resize to story (1080x1920), feed (1080x1350), square (1080x1080) |
| `kenburns` | still image → slow zoom/pan mp4 |
| `concat` | join clips to common 1080x1920/30fps |
| `overlay` | PNG graphic over a video time range, with fades |
| `muxaudio` | attach an audio track to a video |
| `artboard` | render a board state (see below) to full-res, WYSIWYG |

Every op is stateless; the caller owns versioning.

## Presets (`presets.yaml`)

Grade presets learned from a real Instagram aesthetic — raw flash-lit phone photos, heavy saturation, candid found-weirdness:

- **vandal-raw** — the default look (saturation 1.35, contrast 1.12, warmth, vignette, grain)
- **clean-pop** — brighter, commercial-friendly
- **faded-film** — washed, nostalgic
- **noir** — black and white, heavy contrast
- **duotone-teal** — teal duotone treatment

Plus social formats and three text styles (`meme`, `minimal`, `sticker`). Start from a preset, deviate per taste, and add new ones when a look recurs — that's how the engine learns *your* taste. Forks should tune these to their own eye.

## The Artboard (direct manipulation)

Sometimes prompting is the wrong interface. The Artboard is a drag-and-drop HTML canvas for arranging layers by hand:

```bash
# build a board from a folder of images — one command, no template edits
bin/build_artboard.py --assets ./assets --project ./my-project [--layout pile|grid] [--seed seed.json]
```

Open `my-project/artboard.html`: drag layers, switch formats (story/portrait/square/landscape/pin/thumb), adjust scale/rotate/opacity, reorder, undo/redo. Layer positions are fractional, so one arrangement reflows across formats. Then render exactly what you see:

```bash
bin/ae.py artboard --state state.json --format story --out final.png
```

## The versioned loop

The intended workflow, whether driven by a human or an agent:

1. Brief (subject, format, vibe) → 2. build → 3. preview → 4. feedback → new version.
2. Keep every version (`v1.png`, `v2.png`, …) plus a `manifest.json` recording exact params per version, so any version is reproducible. Never overwrite.

## Origin

Built by [Nate Hogsten](https://github.com/nlhogsten) with his Muse agent, October 2026 — originally to produce artwork for his own Instagram, then generalized. This is the first repo in a small ecosystem of open-source, Muse-usable systems: tools designed so other people's agents can pick them up and run them, not just read about them.

## License

MIT — see [LICENSE](LICENSE).
