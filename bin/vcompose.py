#!/usr/bin/env python3
"""vcompose — layer-based video composition compiler (Phase 1).

A composition JSON *is* the timeline (see composition/SPEC.md). This compiles
it to pixels in ONE ffmpeg filter graph. Nothing flattens until final render.

    vcompose.py render comp.json -o out.mp4 [--force] [--keep-tmp] [--graph]
    vcompose.py index comp.json        # layer/timecode map + cache status
    vcompose.py lint comp.json [--profile vandal-raw]
                               # style-profile check (see style/STYLE-GUIDE.md)

Renders are content-addressed (composition JSON + source file hashes) and
cached under ~/workspace/art-engine/video-cache/vc-<key>.mp4, same philosophy
as vtimeline.py: change one number, only the composition re-renders.

Hard rules baked in (learned the hard way):
- never input-seek (-ss before -i) on phone screen recordings; full-decode
  and trim in-filter.
- 2x = trim + setpts=PTS-STARTPTS + select='not(mod(n,2))' + setpts=N/fps/TB.
- holds = pre-extracted frame fed as -loop 1 -framerate fps -t dur input.
- one source feeding N trims in one graph -> explicit split.
- effect hit-ranges = overlay the effected full-duration stream with
  enable='between(...)', never re-trim in-graph pads.
- every output duration verified with ffprobe; mismatch = compiler bug.
"""
import sys, os, json, hashlib, subprocess, shutil, argparse, tempfile, math
import yaml

CACHE = os.path.expanduser("~/workspace/art-engine/video-cache")
os.makedirs(CACHE, exist_ok=True)

SWAP_MAP = {"bgr": "2:1:0", "brg": "2:0:1", "gbr": "1:2:0",
            "grb": "1:0:2", "rbg": "0:2:1"}

GRADES = {
    "vandal-raw": "eq=saturation=1.35:contrast=1.12:brightness=0.02",
    "clean-pop":  "eq=saturation=1.20:contrast=1.08",
    "faded-film": "eq=saturation=0.85:contrast=0.92",
    "noir":       "eq=saturation=0.00:contrast=1.25:brightness=-0.05",
}

DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{r.stderr[-2000:]}")
    return r


def probe(path):
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=width,height,avg_frame_rate,duration",
             "-show_entries", "format=duration", "-of", "json", path])
    d = json.loads(r.stdout)
    st = d["streams"][0]
    num, den = st["avg_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    dur = float(st.get("duration") or d["format"]["duration"])
    return {"w": st["width"], "h": st["height"], "fps": fps, "dur": dur}


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_dur(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path])
    return float(r.stdout.strip())


# ---------------------------------------------------------------- graph
class Graph:
    def __init__(self):
        self.inputs = []
        self.parts = []
        self.n_in = 0
        self.n_lb = 0

    def add_input(self, args):
        idx = self.n_in
        self.inputs.extend(args)
        self.n_in += 1
        return idx

    def lb(self, hint="x"):
        self.n_lb += 1
        return f"{hint}{self.n_lb}"

    def emit(self, filt):
        self.parts.append(filt)

    def build(self):
        return ";".join(self.parts)


def fit_chain(src_w, src_h, fw, fh, fit):
    """Scale/crop chain mapping a src_wh frame into a fw x fh frame."""
    if fit == "fill":
        return f"scale={fw}:{fh},setsar=1"
    if fit == "contain":
        return (f"scale={fw}:{fh}:force_original_aspect_ratio=decrease,"
                f"pad={fw}:{fh}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    # cover (default)
    return (f"scale={fw}:{fh}:force_original_aspect_ratio=increase,"
            f"crop={fw}:{fh},setsar=1")


def speed_chain(speed, fps):
    if speed == 1:
        return ""
    if speed == 2:
        # verified 2x path: decimate frames, retime
        return f",select='not(mod(n\\,2))',setpts=N/{fps}/TB"
    return f",setpts=PTS/{speed}"  # generic (verify output duration)


def hits_expr(hits, t_off=0.0):
    if t_off:
        return "+".join(f"between(t+{t_off:.3f},{a},{b})" for a, b in hits)
    return "+".join(f"between(t,{a},{b})" for a, b in hits)


# ---------------------------------------------------------------- keyframes
# Any animatable param accepts a scalar OR a keyframe list:
#   "shift": [{"t": 0, "value": 0},
#             {"t": 6.5, "value": 34, "ease": "in-out"},
#             {"t": 6.6, "value": 0, "ease": "step"}]
# t is layer-local seconds (0 = layer in-point), the same clock as effect
# hits. ease on a keyframe describes interpolation INTO it from the previous:
#   linear (default), in-out (smoothstep), step (hold previous, jump at t).
# Before the first keyframe the first value holds; after the last, the last.
# Values may be scalars or [x, y] pairs (pair lerp is elementwise).
#
# Compile strategy: split the layer into frame-level slices at keyframe
# boundaries (exactly round(dur*fps) slices), sample every keyframed param
# at each slice midpoint, build the normal constant-param chain per slice,
# and composite the frames onto the running composite one overlay at a time
# (NOT concat: concat cannot re-timestamp single-frame segments — every
# slice emerges at pts 0. See build_keyframed_layer docstring.)
# Frame-level slices mean params update every frame: smooth motion, no
# stepping. (A 0.5s slice version shipped first and visibly lagged — motion
# updated twice per second. Never go back.)
# This reuses the existing effect/transform builders untouched — keyframes
# are a sampling layer, not a second filter architecture.

KF_TRANSFORM = {"x", "y", "scale", "rot", "opacity"}
KF_NUMERIC = {
    "glitch": {"shift", "noise", "sat", "hue", "tint_op"},
    "grade": set(),          # preset is categorical — scalar only
    "blur": {"sigma"},
    "dissolve": set(),       # at/dur/transition are structural — scalar only
    "rgbshift": {"r", "g", "b"},   # [dx, dy] pairs, lerped elementwise
    "scanlines": {"spacing", "opacity"},
    "shake": {"amp"},
}


def is_keyframed(v):
    return isinstance(v, list) and v and isinstance(v[0], dict) \
        and "t" in v[0]


def kf_sorted(kfs):
    ks = sorted(kfs, key=lambda k: k["t"])
    for k in ks:
        if "t" not in k or "value" not in k:
            raise ValueError(f"keyframe needs t + value: {k}")
    return ks


def _lerp(a, b, u):
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return [x + (y - x) * u for x, y in zip(a, b)]
    return a + (b - a) * u


def kf_sample(kfs, t):
    kfs = kf_sorted(kfs)
    if t <= kfs[0]["t"]:
        return kfs[0]["value"]
    for i in range(1, len(kfs)):
        p, c = kfs[i - 1], kfs[i]
        if t <= c["t"]:
            span = c["t"] - p["t"]
            u = 0.0 if span <= 0 else (t - p["t"]) / span
            ease = c.get("ease", "linear")
            if ease == "step":
                return p["value"]
            if ease == "in-out":
                u = u * u * (3 - 2 * u)
            elif ease != "linear":
                raise ValueError(f"unknown ease: {ease}")
            return _lerp(p["value"], c["value"], u)
    return kfs[-1]["value"]


def kf_bounds(kf_lists, dur, fps):
    """Keyframe time boundaries, subdivided to at most one frame per span.

    Frame-level granularity is what makes keyframed motion smooth: each
    slice holds its sampled params for exactly one frame, so params update
    every frame instead of stepping. An 8s/30fps layer = ~240 slices; the
    graph stays tractable (a few thousand filter lines).
    """
    max_slice = 1.0 / fps
    pts = {0.0, float(dur)}
    for kfs in kf_lists:
        for k in kf_sorted(kfs):
            pts.add(min(max(float(k["t"]), 0.0), float(dur)))
    pts = sorted(pts)
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        # Exactly round(span*fps) slices: each slice maps 1:1 to a content
        # frame, so slice i <-> frame i with no drift. (ceil() overshoots
        # by one on float-exact spans like 2.0s@30fps -> 61 slices.)
        n = max(1, int(round((b - a) * fps)))
        for i in range(1, n):
            out.append(a + (b - a) * i / n)
        out.append(b)
    return out


def collect_keyframes(layer):
    """[(scope, eidx, name, kfs)]; scope 't' = transform, 'fx' = effect."""
    found = []
    for name, v in layer.get("transform", {}).items():
        if is_keyframed(v):
            if name not in KF_TRANSFORM:
                raise ValueError(f"transform.{name} is not animatable")
            found.append(("t", None, name, kf_sorted(v)))
    for ei, e in enumerate(layer.get("effects", [])):
        allowed = KF_NUMERIC.get(e["type"], set())
        for name, v in e.items():
            if name in ("type", "hits"):
                continue
            if is_keyframed(v):
                if name not in allowed:
                    raise ValueError(
                        f"effect {e['type']}.{name} is not animatable "
                        f"(categorical params stay scalar)")
                found.append(("fx", ei, name, kf_sorted(v)))
    return found


# ---------------------------------------------------------------- effects
def fx_glitch(g, cur, e, W, H, fps, cdur, t_off=0.0):
    """RGB-split glitch. Hit ranges are handled by the apply_effect wrapper
    (clean = pre-effect stream); this just builds the effected stream."""
    sh = e.get("shift", 34)
    nz = e.get("noise", 40)
    sat = e.get("sat", 1.0)
    hue = e.get("hue", 0)
    swap = e.get("swap")
    tint = e.get("tint")
    tint_op = e.get("tint_op", 0.25)
    start = cur
    ga, gb, gc = g.lb("ga"), g.lb("gb"), g.lb("gc")
    ga2, gc2 = g.lb("ga2"), g.lb("gc2")
    gm = g.lb("gm")
    g.emit(f"[{start}]format=gbrp,split=3[{ga}][{gb}][{gc}]")
    g.emit(f"[{ga}]crop={W-sh}:{H}:{sh}:0,"
           f"pad={W}:{H}:0:0:color=black[{ga2}]")
    g.emit(f"[{gc}]crop={W-sh}:{H}:0:0,"
           f"pad={W}:{H}:{sh}:0:color=black[{gc2}]")
    g.emit(f"[{ga2}][{gb}][{gc2}]mergeplanes=0x001020:gbrp[{gm}]")
    cur = gm
    cur2 = g.lb("gn")
    g.emit(f"[{cur}]noise=alls={nz}:allf=t,hue=s={sat}[{cur2}]")
    cur = cur2
    if hue:
        h2 = g.lb("gh")
        g.emit(f"[{cur}]hue=h={hue}[{h2}]")
        cur = h2
    if swap:
        s2 = g.lb("gs")
        g.emit(f"[{cur}]shuffleplanes={SWAP_MAP[swap]}[{s2}]")
        cur = s2
    if tint:
        tc = g.lb("tc")
        t2 = g.lb("gt")
        hexcol = tint.lstrip("#")
        g.emit(f"color=c=0x{hexcol}:s={W}x{H}:d={cdur}:r={fps},"
               f"format=rgba[{tc}]")
        g.emit(f"[{cur}][{tc}]blend=all_mode=overlay:"
               f"all_opacity={tint_op}[{t2}]")
        cur = t2
    f2 = g.lb("gf")
    g.emit(f"[{cur}]format=yuv420p[{f2}]")
    return None, f2


def fx_grade(g, cur, e, W, H, fps, cdur, t_off=0.0):
    preset = e.get("preset", "vandal-raw")
    if preset not in GRADES:
        raise ValueError(f"unknown grade preset: {preset}")
    out = g.lb("gr")
    g.emit(f"[{cur}]{GRADES[preset]},format=yuv420p[{out}]")
    return None, out  # no clean branch needed; hits handled by wrapper


def fx_blur(g, cur, e, W, H, fps, cdur, t_off=0.0):
    out = g.lb("bl")
    g.emit(f"[{cur}]boxblur={e.get('sigma', 8)}:1,format=yuv420p[{out}]")
    return None, out


def fx_dissolve(g, cur, e, W, H, fps, cdur, t_off=0.0):
    """Treatment-dissolve as a real effect: cross-dissolve between two
    effect stacks, centered at `at`, over `dur`, via xfade.

    {"type":"dissolve","from":[{...effects...}],"to":[{...effects...}],
     "at":3.0,"dur":1.0,"transition":"fade"}
    transition = any xfade transition (fade, fadeblack, fadewhite, ...).
    at/dur are layer-local seconds. Not slice-compatible: combining with
    keyframed params on the same layer raises a clear error.
    """
    if t_off != 0.0:
        raise ValueError("dissolve cannot be combined with keyframed params "
                         "on the same layer")
    at = e.get("at", cdur / 2.0)
    dur = e.get("dur", 1.0)
    tr = e.get("transition", "fade")
    a0, a1 = at - dur / 2.0, at + dur / 2.0
    if not (0 <= a0 < a1 <= cdur + 1e-6):
        raise ValueError(f"dissolve window [{a0:.2f},{a1:.2f}] outside "
                         f"layer duration {cdur:.2f}")
    s1, s2 = g.lb("da"), g.lb("db")
    g.emit(f"[{cur}]split=2[{s1}][{s2}]")
    a, b = s1, s2
    for fe in e.get("from", []):
        a = apply_effect(g, a, fe, W, H, fps, cdur)
    for fe in e.get("to", []):
        b = apply_effect(g, b, fe, W, H, fps, cdur)
    ta, tb = g.lb("dta"), g.lb("dtb")
    g.emit(f"[{a}]trim=start=0:end={a1:.3f},setpts=PTS-STARTPTS,"
           f"format=yuv420p[{ta}]")
    g.emit(f"[{b}]trim=start={a0:.3f}:end={cdur:.3f},setpts=PTS-STARTPTS,"
           f"format=yuv420p[{tb}]")
    out = g.lb("dx")
    g.emit(f"[{ta}][{tb}]xfade=transition={tr}:duration={dur:.3f}:"
           f"offset={a0:.3f},format=yuv420p[{out}]")
    return None, out


def fx_rgbshift(g, cur, e, W, H, fps, cdur, t_off=0.0):
    """Surgical channel split: per-channel [dx, dy] px offsets.
    {"type":"rgbshift","r":[6,0],"g":[-4,2],"b":[-6,0]}
    Refines glitch's fixed horizontal R/B split: all three channels,
    vertical too. Offsets are keyframeable as [dx, dy] pairs.
    """
    chans = {}
    for name in ("r", "g", "b"):
        v = list(e.get(name, [0, 0]))
        dx, dy = int(round(v[0])), int(round(v[1]))
        if abs(dx) >= W or abs(dy) >= H:
            raise ValueError(f"rgbshift {name} offset ({dx},{dy}) too large")
        chans[name] = (dx, dy)
    # format=gbrp split order is G, B, R — track planes explicitly.
    pg, pb, pr = g.lb("rsg"), g.lb("rsb"), g.lb("rsr")
    g.emit(f"[{cur}]format=gbrp,split=3[{pg}][{pb}][{pr}]")
    shifted = []
    for lbl, name in ((pg, "g"), (pb, "b"), (pr, "r")):
        dx, dy = chans[name]
        ax, ay = abs(dx), abs(dy)
        o = g.lb("rso")
        g.emit(f"[{lbl}]crop={W - ax}:{H - ay}:{max(-dx, 0)}:{max(-dy, 0)},"
               f"pad={W}:{H}:{max(dx, 0)}:{max(dy, 0)}:color=black[{o}]")
        shifted.append(o)
    out = g.lb("rsm")
    g.emit(f"[{shifted[0]}][{shifted[1]}][{shifted[2]}]"
           f"mergeplanes=0x001020:gbrp,format=yuv420p[{out}]")
    return None, out


def fx_scanlines(g, cur, e, W, H, fps, cdur, t_off=0.0):
    """CRT/VHS scanlines. {"type":"scanlines","spacing":3,"opacity":0.3}"""
    out = g.lb("sl")
    g.emit(f"[{cur}]drawgrid=w=iw:h={e.get('spacing', 3)}:t=1:"
           f"c=black@{e.get('opacity', 0.3)},format=yuv420p[{out}]")
    return None, out


def fx_shake(g, cur, e, W, H, fps, cdur, t_off=0.0):
    """Positional jitter. {"type":"shake","amp":8,"hits":[[2,4]]}
    amp = max px displacement; deterministic per-frame random (cache-safe)."""
    amp = float(e.get("amp", 8))
    a = max(1, int(math.ceil(amp)))
    out = g.lb("sk")
    g.emit(f"[{cur}]scale={W + 2 * a}:{H + 2 * a},"
           f"crop={W}:{H}:x='random(0)*{2 * a}':y='random(1)*{2 * a}',"
           f"setsar=1,format=yuv420p[{out}]")
    return None, out


FX = {"glitch": fx_glitch, "grade": fx_grade, "blur": fx_blur,
      "dissolve": fx_dissolve, "rgbshift": fx_rgbshift,
      "scanlines": fx_scanlines, "shake": fx_shake}


def apply_effect(g, cur, e, W, H, fps, cdur, t_off=0.0):
    """Build effect; honor optional hits via overlay-enable (never re-trim).

    t_off shifts the hit clock: keyframed layers are sliced with timestamps
    reset to 0, so hits (layer-local seconds) need the slice's start offset.
    The clean branch is ALWAYS the pre-effect stream: splitting the effected
    output here would leave the effect visible outside its hits.
    """
    etype = e["type"]
    if etype not in FX:
        raise ValueError(f"unknown effect type: {etype}")
    hits = e.get("hits")
    if hits:
        pre1, pre2 = g.lb("ec"), g.lb("ee")
        g.emit(f"[{cur}]split=2[{pre1}][{pre2}]")
        fx_in, clean = pre2, pre1
    else:
        fx_in, clean = cur, None
    _, fx = FX[etype](g, fx_in, e, W, H, fps, cdur, t_off)
    if not hits:
        return fx
    out = g.lb("hx")
    g.emit(f"[{clean}][{fx}]overlay=0:0:"
           f"enable='{hits_expr(hits, t_off)}':eof_action=pass:"
           f"format=yuv420[{out}]")
    return out


# ---------------------------------------------------------------- audio
# Top-level "audio": [{id, src, in, out, gain, src_in?, duck_by?}].
# in/out = placement on the comp timeline (seconds); gain = linear
# multiplier; src_in = offset into the source file. duck_by names another
# audio layer whose presence sidechain-ducks this one (music under VO).
# Mixed to one track and muxed as AAC. No audio layers = no audio output
# (byte-identical video path to Phase 2).

def build_audio(g, comp, compdir, duration):
    layers = comp.get("audio", [])
    if not layers:
        return None
    ids = [a["id"] for a in layers]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate audio layer id")
    positioned = {}
    for a in layers:
        src = a["src"] if os.path.isabs(a["src"]) \
            else os.path.join(compdir, a["src"])
        if not os.path.exists(src):
            raise ValueError(f"audio src not found: {src}")
        lin, lout = a["in"], a["out"]
        if not (0 <= lin < lout <= duration + 1e-6):
            raise ValueError(f"audio '{a['id']}': in/out {lin}/{lout} "
                             f"outside duration {duration}")
        seg = lout - lin
        src_in = a.get("src_in", 0)
        ai = g.add_input(["-i", src])
        al = g.lb("au")
        g.emit(f"[{ai}:a]atrim=start={src_in}:end={src_in + seg},"
               f"asetpts=PTS-STARTPTS,volume={a.get('gain', 1.0)},"
               f"aformat=sample_fmts=fltp:sample_rates=48000:"
               f"channel_layouts=stereo,"
               f"adelay={int(lin * 1000)}:all=1,"
               f"apad=whole_dur={duration}[{al}]")
        positioned[a["id"]] = al
    side = dict(positioned)  # sidechain taps the unducked streams
    # A stream tapped as a sidechain ALSO feeds the mix — a filter pad
    # can't be consumed twice, so split sidechain targets.
    for tid in {a["duck_by"] for a in layers if a.get("duck_by")}:
        s1, s2 = g.lb("as1"), g.lb("as2")
        g.emit(f"[{positioned[tid]}]asplit=2[{s1}][{s2}]")
        side[tid], positioned[tid] = s1, s2
    for a in layers:
        if a.get("duck_by"):
            other = a["duck_by"]
            if other not in side:
                raise ValueError(f"audio '{a['id']}' duck_by unknown "
                                 f"layer '{other}'")
            dl = g.lb("ad")
            g.emit(f"[{positioned[a['id']]}][{side[other]}]"
                   f"sidechaincompress=threshold=0.008:ratio=20:"
                   f"attack=10:release=300[{dl}]")
            positioned[a["id"]] = dl
    stems = [positioned[a["id"]] for a in layers]
    mx = g.lb("amx")
    g.emit("".join(f"[{s}]" for s in stems) +
           f"amix=inputs={len(stems)}:normalize=0,"
           f"alimiter=limit=0.95[{mx}]")
    return mx


# ---------------------------------------------------------------- layers
def content_duration(layer, src_dur=None):
    if layer["type"] in ("image", "text"):
        return layer["out"] - layer["in"]
    cuts = layer.get("cuts") or (
        [{"src_in": 0, "src_out": src_dur, "speed": 1}] if src_dur else [])
    d = sum((c["src_out"] - c["src_in"]) / c.get("speed", 1) for c in cuts)
    d += sum(h["dur"] for h in layer.get("holds", []))
    return d


def build_video_layer(g, layer, comp, compdir, workdir, probes):
    W, H, fps = comp["width"], comp["height"], comp["fps"]
    fw, fh = layer.get("frame", {"w": W, "h": H}).values()
    src = layer["src"] if os.path.isabs(layer["src"]) \
        else os.path.join(compdir, layer["src"])
    vi = g.add_input(["-i", src])
    pr = probes[src]
    cuts = layer.get("cuts") or [{"src_in": 0, "src_out": pr["dur"],
                                  "speed": 1}]
    for c in cuts:
        if c["src_out"] > pr["dur"] + 0.05 or c["src_in"] < 0:
            raise ValueError(f"cut {c} outside source duration {pr['dur']:.2f} "
                             f"for layer {layer['id']}")

    # holds: pre-extracted frames (full decode, never input seeking)
    hold_inputs = []
    for i, h in enumerate(layer.get("holds", [])):
        png = os.path.join(workdir, f"hold_{layer['id']}_{i}.png")
        run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf",
             f"select='gte(t\\,{h['at_src']})',"
             f"{fit_chain(pr['w'], pr['h'], fw, fh, layer.get('fit', 'cover'))},"
             f"format=yuv420p",
             "-frames:v", "1", png])
        hi = g.add_input(["-loop", "1", "-framerate", str(fps),
                          "-t", str(h["dur"]), "-i", png])
        hl = g.lb("hold")
        g.emit(f"[{hi}:v]fps={fps},setsar=1,format=yuv420p[{hl}]")
        hold_inputs.append(hl)

    # cuts -> parts
    pads = []
    if len(cuts) > 1:
        sp = [g.lb("cs") for _ in cuts]
        g.emit(f"[{vi}:v]split={len(cuts)}" +
               "".join(f"[{s}]" for s in sp))
    else:
        sp = [f"{vi}:v"]
    for j, c in enumerate(cuts):
        pl = g.lb("p")
        chain = (f"[{sp[j] if len(cuts) > 1 else sp[0]}]"
                 f"trim=start={c['src_in']}:end={c['src_out']},"
                 f"setpts=PTS-STARTPTS{speed_chain(c.get('speed', 1), fps)},"
                 f"fps={fps},"
                 f"{fit_chain(pr['w'], pr['h'], fw, fh, layer.get('fit', 'cover'))},"
                 f"format=yuv420p[{pl}]")
        g.emit(chain)
        pads.append(pl)

    # interleave holds at their at_cut positions
    n_cuts = len(cuts)
    seq = []
    for i, pl in enumerate(pads):
        seq.append(pl)
        for hl, h in zip(hold_inputs, layer.get("holds", [])):
            at = h.get("at_cut", n_cuts - 1)
            if at == i:
                seq.append(hl)
    if len(seq) > 1:
        cc = g.lb("cc")
        g.emit("".join(f"[{s}]" for s in seq) +
               f"concat=n={len(seq)}:v=1:a=0[{cc}]")
        cur = cc
    else:
        cur = seq[0]
    return cur


def build_image_layer(g, layer, comp, compdir):
    fps = comp["fps"]
    src = layer["src"] if os.path.isabs(layer["src"]) \
        else os.path.join(compdir, layer["src"])
    dur = layer["out"] - layer["in"]
    ii = g.add_input(["-loop", "1", "-framerate", str(fps),
                      "-t", str(dur), "-i", src])
    il = g.lb("img")
    g.emit(f"[{ii}:v]fps={fps},setsar=1,format=rgba[{il}]")
    return il


def build_text_layer(g, layer, comp):
    W, H, fps = comp["width"], comp["height"], comp["fps"]
    dur = layer["out"] - layer["in"]
    safe = layer["text"].replace("\\", "\\\\").replace("'", r"'\''") \
        .replace(":", r"\:")
    tl = g.lb("txt")
    g.emit(f"color=0x00000000:{W}x{H}:d={dur}:r={fps},"
           f"drawtext=fontfile={layer.get('font', DEJAVU_BOLD)}:"
           f"text='{safe}':fontsize={layer.get('font_size', 44)}:"
           f"fontcolor={layer.get('color', '#ffffff')}:"
           f"x=(w-text_w)/2:y=(h-text_h)/2,"
           f"format=rgba,fps={fps}[{tl}]")
    return tl


def apply_transform(g, cur, t, W, H):
    scale, rot, op = t.get("scale", 1.0), t.get("rot", 0), t.get("opacity", 1.0)
    if scale != 1.0:
        s2 = g.lb("ts")
        g.emit(f"[{cur}]scale=iw*{scale}:ih*{scale}[{s2}]")
        cur = s2
    if rot:
        r2 = g.lb("tr")
        g.emit(f"[{cur}]rotate={rot}*PI/180:fillcolor=none[{r2}]")
        cur = r2
    if op < 1.0:
        o2 = g.lb("to")
        g.emit(f"[{cur}]format=rgba,colorchannelmixer=aa={op}[{o2}]")
        cur = o2
    return cur


def build_keyframed_layer(g, layer, comp, compdir, workdir, probes, kfs,
                          prev):
    """Keyframe path: per-frame param sampling, assembled via overlay chain.

    Each frame's params are sampled at its midpoint and baked as constants,
    so motion updates every frame (no 0.5s stepping). Frames are extracted
    by time window with select, processed individually, keep their natural
    timestamps, are shifted onto the comp timeline with setpts, and
    composited onto the running composite one overlay at a time
    (eof_action=pass).

    Why an overlay chain instead of slice -> trim -> concat: the concat
    filter does not re-timestamp single-frame segments. Every slice leaves
    trim+setpts at pts=0 and concat emits them all at pts=0 (verified: 3
    one-frame slices -> 3 frames all stamped pts 0 -> 0.067s output);
    keeping original timestamps does not help either (concat mis-offsets
    them). The overlay chain stamps each frame at its exact timestamp, so
    assembly is sample-accurate by construction. Cost is O(n) overlays for
    an n-frame layer; keyframed layers are the exception, not the rule.

    Keyframe t and effect hits share the layer-local clock (0 = layer
    in-point); frames are selected by time window and keep their natural
    timestamps (no zeroing, no frame-index rounding), then shifted onto the
    comp timeline with setpts=PTS+lin/TB before compositing.
    """
    W, H, fps = comp["width"], comp["height"], comp["fps"]
    lin, lout = layer["in"], layer["out"]
    dur = lout - lin
    if layer["type"] == "video":
        content = build_video_layer(g, layer, comp, compdir, workdir, probes)
    elif layer["type"] == "image":
        content = build_image_layer(g, layer, comp, compdir)
    elif layer["type"] == "text":
        content = build_text_layer(g, layer, comp)
    else:
        raise ValueError(f"unknown layer type: {layer['type']}")

    bounds = kf_bounds([k[3] for k in kfs], dur, fps)
    n = len(bounds) - 1
    cs = [g.lb("ks") for _ in range(n)]
    g.emit(f"[{content}]split={n}" + "".join(f"[{s}]" for s in cs))

    fx_list = layer.get("effects", [])
    cur = prev
    for i in range(n):
        s0, s1 = bounds[i], bounds[i + 1]
        mid = (s0 + s1) / 2.0
        vals = {(scope, eidx, name): kf_sample(kfl, mid)
                for scope, eidx, name, kfl in kfs}
        # Select by time window (upper bound exclusive via lt): the frame
        # keeps its natural layer-local timestamp, so no frame-index math
        # and no rounding hazards. Each slice holds exactly one content
        # frame; 0 or 2 degrades gracefully (pass-through / both placed).
        tm = g.lb("kt")
        g.emit(f"[{cs[i]}]select='gte(t\\,{s0:.6f})*lt(t\\,{s1:.6f})'[{tm}]")
        fc = tm
        for ei, e in enumerate(fx_list):
            e2 = dict(e)
            for (scope, eidx, name), v in vals.items():
                if scope == "fx" and eidx == ei:
                    e2[name] = v
            # t_off=0: the frame's clock is already layer-local (no
            # timestamp reset), so hits evaluate directly.
            fc = apply_effect(g, fc, e2, W, H, fps, 1.0 / fps)
        t2 = {}
        for name, v in layer.get("transform", {}).items():
            t2[name] = vals.get(("t", None, name), v)
        fc = apply_transform(g, fc, t2, W, H)
        x, y = t2.get("x", 0), t2.get("y", 0)
        tp = g.lb("ktp")
        g.emit(f"[{fc}]setpts=PTS+{lin}/TB[{tp}]")
        m = g.lb("km")
        g.emit(f"[{cur}][{tp}]overlay={x:.2f}:{y:.2f}:"
               f"eof_action=pass[{m}]")
        cur = m
    kf = g.lb("kcf")
    g.emit(f"[{cur}]format=yuv420p[{kf}]")
    return kf


# ---------------------------------------------------------------- compile
def canonical(comp, compdir, probes):
    layers = []
    for L in comp["layers"]:
        e = dict(L)
        if L["type"] in ("video", "image"):
            src = L["src"] if os.path.isabs(L["src"]) \
                else os.path.join(compdir, L["src"])
            e["src_hash"] = sha_file(src)
            e.pop("src", None)
        layers.append(e)
    audio = []
    for A in comp.get("audio", []):
        e = dict(A)
        src = A["src"] if os.path.isabs(A["src"]) \
            else os.path.join(compdir, A["src"])
        e["src_hash"] = sha_file(src)
        e.pop("src", None)
        audio.append(e)
    return {"canvas": [comp["width"], comp["height"], comp["fps"],
                       comp.get("duration"), comp.get("bg", "#000000")],
            "layers": layers, "audio": audio}


def compile_comp(comp, compdir, workdir, probes):
    W, H, fps = comp["width"], comp["height"], comp["fps"]
    duration = comp["duration"]
    bg = comp.get("bg", "#000000").lstrip("#")
    g = Graph()

    # validate annotation ordering
    seen_annot = False
    for L in comp["layers"]:
        if L.get("annotate"):
            seen_annot = True
        elif seen_annot:
            print(f"WARNING: content layer '{L['id']}' after annotation "
                  f"layer — annotation must be last", file=sys.stderr)

    bi = g.add_input(["-f", "lavfi", "-i",
                      f"color=c=0x{bg}:s={W}x{H}:d={duration}:r={fps}"])
    g.emit(f"[{bi}:v]format=yuv420p[base]")
    prev = "base"

    for L in comp["layers"]:
        lin, lout = L["in"], L["out"]
        if not (0 <= lin < lout <= duration + 1e-6):
            raise ValueError(f"layer {L['id']}: in/out {lin}/{lout} outside "
                             f"duration {duration}")
        kfs = collect_keyframes(L)
        if kfs:
            # Keyframed path composites slice-by-slice onto prev itself
            # and returns the new prev (already on the comp timeline).
            prev = build_keyframed_layer(g, L, comp, compdir, workdir,
                                         probes, kfs, prev)
            continue
        else:
            if L["type"] == "video":
                cur = build_video_layer(g, L, comp, compdir, workdir, probes)
            elif L["type"] == "image":
                cur = build_image_layer(g, L, comp, compdir)
            elif L["type"] == "text":
                cur = build_text_layer(g, L, comp)
            else:
                raise ValueError(f"unknown layer type: {L['type']}")

        cdur = content_duration(
            L, probes[os.path.join(compdir, L["src"])
                      if not os.path.isabs(L["src"]) else L["src"]]["dur"]
            if L["type"] == "video" else None)
        if abs(cdur - (lout - lin)) > 0.06:
            print(f"WARNING: layer '{L['id']}' content {cdur:.2f}s != "
                  f"placement {lout - lin:.2f}s", file=sys.stderr)

        if not kfs:
            for e in L.get("effects", []):
                cur = apply_effect(g, cur, e, W, H, fps, cdur)

            cur = apply_transform(g, cur, L.get("transform", {}), W, H)

            t = L.get("transform", {})
            x, y = t.get("x", 0), t.get("y", 0)
        # Shift the layer stream onto the comp timeline. Overlay syncs
        # inputs by timestamp, so without this a layer with in>0 composites
        # the wrong content (or black). No-op for in=0. Placed after
        # effects/transform so hit ranges stay layer-local seconds.
        sh = g.lb("sh")
        g.emit(f"[{cur}]setpts=PTS+{lin}/TB[{sh}]")
        cur = sh
        co = g.lb("co")
        # NOTE: no :format on the overlay itself. Forcing the output format
        # makes libavfilter convert the overlay input to yuv420 *before*
        # blending, which drops its alpha (transparent plates/text render
        # as black boxes). Blend first, then convert explicitly.
        g.emit(f"[{prev}][{cur}]overlay={x}:{y}:"
               f"enable='between(t,{lin},{lout})':eof_action=pass[{co}]")
        cf = g.lb("cf")
        g.emit(f"[{co}]format=yuv420p[{cf}]")
        prev = cf

    g.emit(f"[{prev}]format=yuv420p[vout]")
    # build_audio appends inputs AND graph parts, so it runs before the
    # command list is frozen.
    aout = build_audio(g, comp, compdir, duration)
    # The graph rides in a script file, not on the command line: per-frame
    # keyframed layers produce thousands of filter lines, which blows past
    # the OS single-argument limit (ARG_MAX) as -filter_complex. The -/
    # prefix reads the option value from a file (replaces the deprecated
    # -filter_complex_script); behavior is identical for ffmpeg.
    graph_path = os.path.join(workdir, "filtergraph.txt")
    with open(graph_path, "w") as f:
        f.write(g.build())
    cmd = (["ffmpeg", "-y", "-v", "error"] + g.inputs +
           ["-/filter_complex", graph_path, "-map", "[vout]"])
    if aout:
        cmd += ["-map", f"[{aout}]", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-r", str(fps)]
    return cmd


# ---------------------------------------------------------------- commands
def load_comp(path):
    compdir = os.path.dirname(os.path.abspath(path))
    with open(path) as f:
        comp = json.load(f)
    for k in ("width", "height", "fps", "duration", "layers"):
        if k not in comp:
            raise ValueError(f"composition missing '{k}'")
    return comp, compdir


def probe_all(comp, compdir):
    probes = {}
    for L in comp["layers"]:
        if L["type"] == "video":
            src = L["src"] if os.path.isabs(L["src"]) \
                else os.path.join(compdir, L["src"])
            if src not in probes:
                probes[src] = probe(src)
    return probes


def cache_key(comp, compdir, probes):
    canon = canonical(comp, compdir, probes)
    # The compiler itself is part of the key: a vcompose.py change must
    # invalidate old renders (e.g. the 0.5s -> per-frame keyframe change),
    # otherwise stale cache entries would be served as current.
    with open(os.path.abspath(__file__), "rb") as f:
        compiler_hash = hashlib.sha256(f.read()).hexdigest()[:16]
    canon["compiler"] = compiler_hash
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()) \
        .hexdigest()[:16]


def cmd_render(args):
    ap = argparse.ArgumentParser()
    ap.add_argument("comp")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-tmp", action="store_true")
    ap.add_argument("--graph", action="store_true")
    a = ap.parse_args(args)

    comp, compdir = load_comp(a.comp)
    probes = probe_all(comp, compdir)
    if a.force:
        key = cache_key(comp, compdir, probes)
        gone = os.path.join(CACHE, f"vc-{key}.mp4")
        if os.path.exists(gone):
            os.remove(gone)
    cached, key = ensure_rendered(comp, compdir, probes,
                                  keep_tmp=a.keep_tmp, show_graph=a.graph)
    shutil.copy(cached, a.out)
    print(f"wrote {a.out}")


def ensure_rendered(comp, compdir, probes, key=None, keep_tmp=False,
                    show_graph=False):
    """Render to the content-addressed cache if missing; return (path, key)."""
    key = key or cache_key(comp, compdir, probes)
    cached = os.path.join(CACHE, f"vc-{key}.mp4")
    if os.path.exists(cached):
        print(f"[CACHE] vc-{key}")
        return cached, key
    workdir = tempfile.mkdtemp(prefix="vcompose-")
    try:
        cmd = compile_comp(comp, compdir, workdir, probes)
        if show_graph:
            gi = cmd.index("-/filter_complex") + 1
            print(open(cmd[gi]).read())
        tmp = os.path.join(CACHE, f"tmp-vc-{key}.mp4")
        run(cmd + [tmp])
        dur = ffprobe_dur(tmp)
        if abs(dur - comp["duration"]) > 0.15:
            raise RuntimeError(
                f"render duration {dur:.3f}s != declared "
                f"{comp['duration']}s — compiler bug, investigate")
        os.rename(tmp, cached)
        print(f"[BUILT] vc-{key} ({dur:.2f}s, verified)")
    finally:
        if not keep_tmp:
            shutil.rmtree(workdir, ignore_errors=True)
    return cached, key


def cmd_styleframe(args):
    """Stills before motion: extract frame(s) at the given comp time(s).

    Renders the comp through the normal cached path (cache reuse — no
    re-render when nothing changed), then seeks the cached render with an
    output-side -ss (frame-accurate) and writes one PNG per -t.

    Multiple -t values write suffixed files in -t order: -o frame.png with
    -t 2.5 -t 5.0 writes frame-1.png and frame-2.png. A single -t writes
    exactly to -o.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("comp")
    ap.add_argument("-t", "--time", action="append", type=float,
                    required=True, metavar="SEC",
                    help="comp timestamp(s); repeat for multiple stills")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args(args)

    comp, compdir = load_comp(a.comp)
    dur = float(comp["duration"])
    times = sorted(a.time)
    for t in times:
        if not (0.0 <= t <= dur):
            ap.error(f"-t {t} outside comp duration [0, {dur}]")
    probes = probe_all(comp, compdir)
    cached, _key = ensure_rendered(comp, compdir, probes)

    outs = []
    if len(times) == 1:
        outs = [(times[0], a.out)]
    else:
        base, ext = os.path.splitext(a.out)
        ext = ext or ".png"
        outs = [(t, f"{base}-{i}{ext}") for i, t in enumerate(times, 1)]
    for t, out in outs:
        run(["ffmpeg", "-y", "-v", "error", "-i", cached,
             "-ss", f"{t:.3f}", "-frames:v", "1", out])
        print(f"wrote {out}  (t={t:.3f}s)")


def cmd_check_assets(args):
    """Asset gate: verify image cutouts on a checkerboard before rendering.

    For every image layer src in the comp: scale to a max 480px preview,
    composite over a gray checkerboard (no pure white — transparent regions
    must never read as near-white), save per-asset PNGs plus a contact
    sheet, and run the rim heuristic:

    rim = outer 4% border pixels; a rim pixel counts when it is OPAQUE
    (alpha > 128) AND near-white (r, g, b all > 240). More than 20% of the
    rim counting -> WARN (background probably not cut out — the white-box
    sticker failure); otherwise PASS.

    Nuance (read before tuning the threshold): sticker-style white OUTLINES
    are intentional and only a few px wide, well under 20% of the rim. White
    FILL touching the edges is the bug this catches. Opaque photos that
    simply fill the frame (e.g. white sky at the top edge) can WARN — that
    is a prompt for human review, not a verdict.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("comp")
    ap.add_argument("-o", "--out", default=None,
                    help="contact sheet path "
                         "(default: <compdir>/check-assets.png)")
    a = ap.parse_args(args)
    comp, compdir = load_comp(a.comp)
    out = a.out or os.path.join(compdir, "check-assets.png")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    imgs = [(L.get("id", f"layer{i}"), L["src"])
            for i, L in enumerate(comp["layers"])
            if L["type"] == "image"]
    if not imgs:
        print("no image layers — nothing to check")
        return
    base, _ext = os.path.splitext(out)

    RIM_FRAC, WHITE_T, WARN_PCT = 0.04, 240, 0.20
    results, previews = [], []
    for lid, src in imgs:
        path = src if os.path.isabs(src) else os.path.join(compdir, src)
        if not os.path.exists(path):
            results.append((lid, src, "ERROR", "file not found"))
            print(f"[ERROR] {lid:16} {src} — file not found")
            continue
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", path], capture_output=True, text=True)
        w0, h0 = (int(x) for x in r.stdout.strip().split(","))
        sc = min(1.0, 480.0 / max(w0, h0))
        w, h = max(1, int(w0 * sc)), max(1, int(h0 * sc))
        prev_png = f"{base}-{lid}.png"
        # Gray checkerboard (128/200, no pure white) + image over it.
        geq = ("format=rgb24,geq=r='if(mod(trunc(X/24)+trunc(Y/24),2),200,128)':"
               "g='if(mod(trunc(X/24)+trunc(Y/24),2),200,128)':"
               "b='if(mod(trunc(X/24)+trunc(Y/24),2),200,128)'[bg]")
        fc = (f"[1:v]{geq};[0:v]format=rgba,scale={w}:{h}[fg];"
              f"[bg][fg]overlay=0:0,format=rgb24")
        run(["ffmpeg", "-y", "-v", "error", "-i", path,
             "-f", "lavfi", "-i", f"color=c=0x808080:s={w}x{h}:d=1:r=1",
             "-filter_complex", fc, "-frames:v", "1", prev_png])
        previews.append(prev_png)
        # Rim analysis on raw RGBA (stdlib only — no image decode needed).
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path,
             "-vf", f"format=rgba,scale={w}:{h}", "-f", "rawvideo",
             "-pix_fmt", "rgba", "-"], capture_output=True)
        if r.returncode != 0:
            results.append((lid, src, "ERROR", "ffmpeg decode failed"))
            print(f"[ERROR] {lid:16} {src} — decode failed")
            continue
        px = r.stdout
        rim, bad = 0, 0
        rx, ry = max(1, int(w * RIM_FRAC)), max(1, int(h * RIM_FRAC))
        for yy in range(h):
            edge_y = yy < ry or yy >= h - ry
            for xx in range(w):
                if not (edge_y or xx < rx or xx >= w - rx):
                    continue
                o = (yy * w + xx) * 4
                rr, gg, bb, aa = px[o], px[o + 1], px[o + 2], px[o + 3]
                rim += 1
                if aa > 128 and rr > WHITE_T and gg > WHITE_T \
                        and bb > WHITE_T:
                    bad += 1
        pct = bad / rim if rim else 0.0
        verdict = "WARN" if pct > WARN_PCT else "PASS"
        results.append((lid, src, verdict, f"{pct * 100:.1f}% rim white"))
        print(f"[{verdict:5}] {lid:16} {os.path.basename(src):28} "
              f"{pct * 100:5.1f}% opaque near-white rim")

    if previews:
        if len(previews) == 1:
            shutil.copy(previews[0], out)
        else:
            n = len(previews)
            cols = math.ceil(math.sqrt(n))
            # xstack grid: pad inputs to equal size first via scale+pad.
            ins, fcs = [], []
            for i, pp in enumerate(previews):
                ins += ["-i", pp]
                fcs.append(f"[{i}:v]scale=480:-1:force_original_aspect_ratio=decrease,"
                           f"pad=480:480:(ow-iw)/2:(oh-ih)/2:color=0x222222[v{i}]")
            xins = "".join(f"[v{i}]" for i in range(n))
            layout = "|".join(
                f"{(i % cols) * 480}_{(i // cols) * 480}" for i in range(n))
            fcs.append(f"{xins}xstack=inputs={n}:layout={layout}[sheet]")
            run(["ffmpeg", "-y", "-v", "error"] + ins +
                ["-filter_complex", ";".join(fcs),
                 "-map", "[sheet]", "-frames:v", "1", out])
        print(f"contact sheet: {out}")
    warns = sum(1 for _r in results if _r[2] == "WARN")
    errs = sum(1 for _r in results if _r[2] == "ERROR")
    print(f"{len(results)} assets: "
          f"{len(results) - warns - errs} PASS, {warns} WARN, {errs} ERROR")


def cmd_index(args):
    ap = argparse.ArgumentParser()
    ap.add_argument("comp")
    a = ap.parse_args(args)
    comp, compdir = load_comp(a.comp)
    probes = probe_all(comp, compdir)
    key = cache_key(comp, compdir, probes)
    hit = os.path.exists(os.path.join(CACHE, f"vc-{key}.mp4"))
    print(f"canvas {comp['width']}x{comp['height']}@{comp['fps']} "
          f"dur={comp['duration']}s  cache={'HIT' if hit else 'MISS'} {key}")
    print(f"{'layer':16} {'type':7} {'in':>6} {'out':>6} "
          f"{'content':>8} effects")
    for L in comp["layers"]:
        sd = None
        if L["type"] == "video":
            src = L["src"] if os.path.isabs(L["src"]) \
                else os.path.join(compdir, L["src"])
            sd = probes[src]["dur"]
        cd = content_duration(L, sd)
        effs = ",".join(e["type"] for e in L.get("effects", [])) or "-"
        ann = " [annotate]" if L.get("annotate") else ""
        print(f"{L['id']:16} {L['type']:7} {L['in']:6.2f} {L['out']:6.2f} "
              f"{cd:8.2f} {effs}{ann}")
    if comp.get("audio"):
        print("audio:")
        for A in comp["audio"]:
            dk = f" duck_by={A['duck_by']}" if A.get("duck_by") else ""
            print(f"  {A['id']:14} {A['in']:6.2f} {A['out']:6.2f} "
                  f"gain={A.get('gain', 1.0)}{dk}")


# ---------------------------------------------------------------- lint
# Style profiles: taste as data. Rules learned from real incidents, checked
# against the render. See style/STYLE-GUIDE.md. `intentional: true` on a
# layer marks a deliberate beat and exempts its [in, out] range.

STYLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "style")

FRAME_CHECK_TYPES = {"dead_viewport", "accidental_freeze", "near_white"}


def load_profile(name):
    path = os.path.join(STYLE_DIR, f"{name}.yaml")
    if not os.path.exists(path):
        raise ValueError(f"unknown style profile: {name} ({path} missing)")
    with open(path) as f:
        prof = yaml.safe_load(f)
    rules = {}
    if prof.get("inherits"):
        for r in load_profile(prof["inherits"])["rules"]:
            rules[r["id"]] = r
    for r in prof.get("rules", []):
        rules[r["id"]] = r
    prof["rules"] = list(rules.values())
    return prof


def intentional_ranges(comp):
    return [(L["in"], L["out"]) for L in comp["layers"]
            if L.get("intentional")]


def hold_ranges(comp):
    """Timeline ranges covered by declared holds (intent made data)."""
    out = []
    for L in comp["layers"]:
        if L.get("type") != "video":
            continue
        cuts = L.get("cuts") or []
        if not cuts:
            continue
        durs = [(c["src_out"] - c["src_in"]) / c.get("speed", 1)
                for c in cuts]
        t = L["in"]
        for i, d in enumerate(durs):
            t += d
            for h in L.get("holds", []):
                if h.get("at_cut", len(cuts) - 1) == i:
                    out.append((t, t + h["dur"]))
                    t += h["dur"]
    return out


def exempt(times, ranges, dt):
    """All frame times covered (with one-sample slop) by some range."""
    return any(all(a - dt <= t <= b + dt for t in times)
               for a, b in ranges)


def runs_of(flags):
    runs, s = [], None
    for i, f in enumerate(flags):
        if f and s is None:
            s = i
        elif not f and s is not None:
            runs.append((s, i - 1))
            s = None
    if s is not None:
        runs.append((s, len(flags) - 1))
    return runs


def static_checks(comp, profile):
    """Checks that need only the JSON (no render)."""
    viols = []
    for r in profile["rules"]:
        t = r["check"]["type"]
        if t == "annotation_effects":
            for L in comp["layers"]:
                if L.get("annotate") and L.get("effects"):
                    viols.append({"rule": r["id"],
                                  "severity": r["severity"],
                                  "where": f"layer '{L['id']}'",
                                  "detail": "annotation layer carries effects "
                                            "— content effects must never "
                                            "touch annotation"})
        elif t == "content_grade":
            for L in comp["layers"]:
                if (not L.get("annotate")
                        and L["type"] in ("video", "image")
                        and not L.get("effects")):
                    viols.append({"rule": r["id"],
                                  "severity": r["severity"],
                                  "where": f"layer '{L['id']}'",
                                  "detail": "content layer has no grade/effect "
                                            "— declare the look"})
        elif t == "glitch_hits":
            for L in comp["layers"]:
                for e in L.get("effects", []):
                    if e.get("type") == "glitch" and not e.get("hits"):
                        viols.append({"rule": r["id"],
                                      "severity": r["severity"],
                                      "where": f"layer '{L['id']}'",
                                      "detail": "glitch without hits blankets "
                                                "the whole layer — declare "
                                                "hit ranges"})
    return viols


def sample_frames(path, sample_fps=1, size=64):
    """1fps (configurable) mean-brightness + inter-frame diff per sample."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf",
           f"fps={sample_fps},scale={size}:{size}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    out = subprocess.run(cmd, capture_output=True).stdout
    px = size * size * 3
    n = len(out) // px
    frames, prev = [], None
    for i in range(n):
        buf = out[i * px:(i + 1) * px]
        mean = sum(buf) / len(buf)
        diff = (sum(abs(a - b) for a, b in zip(buf, prev)) / len(buf)
                if prev is not None else None)
        frames.append({"t": i / sample_fps, "mean": mean, "diff": diff})
        prev = buf
    return frames


def frame_checks(frames, profile, comp):
    viols = []
    intentional = intentional_ranges(comp)
    holds = hold_ranges(comp)
    for r in profile["rules"]:
        c = r["check"]
        t = c["type"]
        if t not in FRAME_CHECK_TYPES:
            continue
        sf = c.get("sample_fps", 1)
        dt = 1.0 / sf
        if t == "dead_viewport":
            flags = [f["mean"] < c["dark_below"]
                     or f["mean"] > c["bright_above"] for f in frames]
            for i, j in runs_of(flags):
                dur = (j - i + 1) * dt
                times = [frames[k]["t"] for k in range(i, j + 1)]
                if dur > c["min_seconds"] and not exempt(
                        times, intentional, dt):
                    kind = ("black" if frames[i]["mean"] < c["dark_below"]
                            else "white")
                    viols.append({"rule": r["id"],
                                  "severity": r["severity"],
                                  "where": f"{times[0]:.1f}–{times[-1] + dt:.1f}s",
                                  "detail": f"dead viewport ({kind}, "
                                            f"{dur:.1f}s continuous)"})
        elif t == "near_white":
            flags = [f["mean"] > c["bright_above"] for f in frames]
            for i, j in runs_of(flags):
                dur = (j - i + 1) * dt
                times = [frames[k]["t"] for k in range(i, j + 1)]
                if dur >= c["min_seconds"] and not exempt(
                        times, intentional, dt):
                    viols.append({"rule": r["id"],
                                  "severity": r["severity"],
                                  "where": f"{times[0]:.1f}–{times[-1] + dt:.1f}s",
                                  "detail": "sustained near-white — likely a "
                                            "page load; cut, ramp, or cover it"})
        elif t == "accidental_freeze":
            flags = [f["diff"] is not None and f["diff"] < c["diff_below"]
                     for f in frames]
            ranges = intentional + (holds if c.get("exempt_holds") else [])
            for i, j in runs_of(flags):
                # diff run i..j => frames i-1..j are static
                n_static = (j - i + 1)
                times = [frames[k]["t"] for k in range(i - 1, j + 1)]
                if n_static >= c["min_seconds"] * sf and not exempt(
                        times, ranges, dt):
                    viols.append({"rule": r["id"],
                                  "severity": r["severity"],
                                  "where": f"{times[0]:.1f}–{times[-1] + dt:.1f}s",
                                  "detail": f"static frame "
                                            f"({n_static * dt:.1f}s) with no "
                                            f"intentional mark"})
    return viols


def cmd_lint(args):
    ap = argparse.ArgumentParser()
    ap.add_argument("comp")
    ap.add_argument("--profile", default="vandal-raw")
    a = ap.parse_args(args)

    comp, compdir = load_comp(a.comp)
    profile = load_profile(a.profile)
    print(f"lint {a.comp}  profile={profile['name']} "
          f"({len(profile['rules'])} rules)")

    viols = static_checks(comp, profile)

    frame_rules = [r for r in profile["rules"]
                   if r["check"]["type"] in FRAME_CHECK_TYPES]
    if frame_rules:
        probes = probe_all(comp, compdir)
        cached, _key = ensure_rendered(comp, compdir, probes)
        sf = max(r["check"].get("sample_fps", 1) for r in frame_rules)
        frames = sample_frames(cached, sample_fps=sf)
        viols += frame_checks(frames, profile, comp)

    errs = [v for v in viols if v["severity"] == "error"]
    warns = [v for v in viols if v["severity"] == "warn"]
    for v in viols:
        print(f"[{v['severity'].upper():5}] {v['rule']:22} {v['where']:16} "
              f"{v['detail']}")
    print(f"{len(errs)} errors, {len(warns)} warnings")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    cmd, rest = sys.argv[1], sys.argv[2:]
    {"render": cmd_render, "index": cmd_index, "lint": cmd_lint,
     "styleframe": cmd_styleframe, "check-assets": cmd_check_assets}[cmd](rest)
