#!/usr/bin/env python3
"""vtimeline — segment-cached video timelines for the art engine.

A project is a JSON timeline of segments. Each segment renders through a
registered op; renders are content-addressed and cached under
~/workspace/art-engine/video-cache/, so changing one beat re-renders ONLY
that beat. Assembly concats cached segments with stream copy (fast, no
generational quality loss).

    vtimeline.py render project.json -o out.mp4
    vtimeline.py index project.json          # timecode map + cache status
    vtimeline.py gc project.json             # prune cache entries the project
                                            # no longer references

Project JSON:
    {"width": 1080, "height": 1920, "fps": 30,
     "segments": [{"id": "beat-1", "op": "still",
                   "params": {"src": "base.png", "dur": 3.0}}]}
Paths in params are relative to the project file's directory.

Op registry lives in OPS below. Add a new op = add a function that takes
(params, ctx) and returns the path of a rendered mp4 (any res — the
assembler normalizes to the project canvas).
"""
import sys, os, json, hashlib, subprocess, shutil

CACHE = os.path.expanduser("~/workspace/art-engine/video-cache")
os.makedirs(CACHE, exist_ok=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{r.stderr[-2000:]}")
    return r


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve(params, projdir):
    """Absolutize known file params + collect source hashes for the key."""
    out = dict(params)
    hashes = {}
    for k, v in params.items():
        if k in ("src", "base", "shirt") and isinstance(v, str):
            p = v if os.path.isabs(v) else os.path.join(projdir, v)
            out[k] = p
            hashes[k] = sha_file(p)
    return out, hashes


# ---------------------------------------------------------------- ops
def op_still(p, ctx):
    """A still image held for dur seconds, optional slow zoom."""
    W, H, fps = ctx["W"], ctx["H"], ctx["fps"]
    dur, frames = p["dur"], int(p["dur"] * ctx["fps"])
    z = p.get("zoom")  # {from, to, yoff}
    if z:
        filt = (f"[0:v]scale={W*2}:{H*2},"
                f"zoompan=z='{z['from']}+({z['to']}-{z['from']})*on/{frames}':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2){z.get('yoff','')}':"
                f"d={frames}:s={W}x{H}:fps={fps},setsar=1[v]")
    else:
        filt = (f"[0:v]scale={W}:{H},loop=loop={frames}:size=1:start=0,"
                f"fps={fps},setsar=1,format=yuv420p[v]")
    return filt, ["-loop", "1", "-i", p["src"]], dur


def op_hold(p, ctx):
    """Deliberate stillness: a static frame, zero movement."""
    return op_still({**p, "zoom": None}, ctx)


def op_card(p, ctx):
    """Hard-cut text card: text slams in/out, no fades. Ever."""
    W, H, fps = ctx["W"], ctx["H"], ctx["fps"]
    t0, t1 = p.get("show", [1.0, 2.0])
    safe = p["text"].replace("'", r"'\''")
    filt = (f"color=black:{W}x{H}:d={p['dur']},fps={fps},"
            f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
            f"text='{safe}':fontsize={p.get('fontsize', 44)}:"
            f"fontcolor={p.get('color', '#f5f0e6')}:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,{t0},{t1})',"
            f"format=yuv420p[v]")
    return filt, ["-f", "lavfi", "-i", "color=black:16x16:d=0.1"], p["dur"]


def op_cutout_isolation(p, ctx):
    """Anchored cutout isolation: the base frame sits static, then a black
    layer fades up over EVERYTHING while the subject cutout stays on top —
    its center pixel-locked, optionally growing. No cuts: the subject never
    leaves the frame; the world dissolves out from behind it.

    params: base (full-canvas png), shirt (rgba cutout png, already at
    canvas scale), cx, cy (shirt center in canvas px), hold1, fade, hold2,
    grow (e.g. 1.8 = ends at 2.8x, growth runs across fade+hold2).
    """
    W, H, fps = ctx["W"], ctx["H"], ctx["fps"]
    hold1, fade, hold2 = p["hold1"], p["fade"], p["hold2"]
    dur = hold1 + fade + hold2
    grow = p.get("grow", 0.0)
    cx, cy = p["cx"], p["cy"]
    gt = f"min(max((t-{hold1})/({fade}+{hold2})\\,0)\\,1)"
    shirt = (f"[1:v]format=rgba,"
             f"scale=w='iw*(1+{grow}*{gt})':h='ih*(1+{grow}*{gt})':eval=frame,"
             f"fps={fps}[sh]")
    filt = (f"[0:v]scale={W}:{H},fps={fps},format=yuv420p[bg];"
            f"{shirt};"
            f"color=black:{W}x{H}:d={dur},fps={fps},format=rgba,"
            f"fade=t=in:st={hold1}:d={fade}:alpha=1[blk];"
            f"[bg][blk]overlay=0:0:format=yuv420[comp];"
            f"[comp][sh]overlay=x='{cx}-w/2':y='{cy}-h/2':"
            f"eof_action=pass:format=yuv420[v]")
    return filt, ["-loop", "1", "-i", p["base"], "-loop", "1", "-i", p["shirt"]], dur


def op_clip(p, ctx):
    """A prebuilt clip, normalized to the project canvas (for segments
    rendered by bespoke scripts, e.g. glitch scrolls)."""
    W, H, fps = ctx["W"], ctx["H"], ctx["fps"]
    filt = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={fps},setsar=1,format=yuv420p[v]")
    return filt, ["-i", p["src"]], p["dur"]


def op_slam(p, ctx):
    """Sticker-slam: one or more rgba stickers slap onto the bg, each
    scaling from `from_scale` to 1 over `slam_dur`, with its own delay.
    Angles/shadows are pre-baked into the sticker PNGs.

    params: bg, dur, stickers=[{src, cx, cy, delay, from_scale}],
            slam_dur=0.35
    """
    W, H, fps = ctx["W"], ctx["H"], ctx["fps"]
    slam_dur = p.get("slam_dur", 0.35)
    filt = f"[0:v]scale={W}:{H},fps={fps},format=yuv420p[bg]"
    inputs = ["-loop", "1", "-i", p["bg"]]
    prev = "bg"
    for i, st in enumerate(p["stickers"]):
        dly = st.get("delay", 0.0)
        fs = st.get("from_scale", 2.4)
        cx, cy = st["cx"], st["cy"]
        s = (f"({fs}+(1-{fs})*min(max((t-{dly})/{slam_dur}\\,0)\\,1))")
        filt += (f";[{i+1}:v]format=rgba,"
                 f"scale=w='iw*{s}':h='ih*{s}':eval=frame,"
                 f"fps={fps}[st{i}]")
        filt += (f";[{prev}][st{i}]overlay=x='{cx}-w/2':y='{cy}-h/2':"
                 f"enable='gte(t\\,{dly})':eof_action=pass:"
                 f"format=yuv420[c{i}]")
        inputs += ["-loop", "1", "-i", st["src"]]
        prev = f"c{i}"
    filt += f";[{prev}]null[v]"
    return filt, inputs, p["dur"]


OPS = {"still": op_still, "hold": op_hold, "card": op_card,
       "cutout_isolation": op_cutout_isolation,
       "clip": op_clip, "slam": op_slam}


# ---------------------------------------------------------------- render
def render_segment(seg, ctx, projdir):
    op = seg["op"]
    params, hashes = resolve(seg.get("params", {}), projdir)
    key = hashlib.sha256(json.dumps(
        {"op": op, "params": {k: v for k, v in seg.get("params", {}).items()},
         "hashes": hashes, "canvas": [ctx["W"], ctx["H"], ctx["fps"]]},
        sort_keys=True).encode()).hexdigest()[:16]
    cached = os.path.join(CACHE, f"{key}.mp4")
    if os.path.exists(cached):
        return cached, True, key
    filt, inputs, dur = OPS[op](params, ctx)
    tmp = os.path.join(CACHE, f"tmp-{key}.mp4")
    run(["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", filt, "-map", "[v]",
         "-t", str(dur), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-r", str(ctx["fps"]), tmp])
    os.rename(tmp, cached)
    return cached, False, key


def cmd_render(args):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args(args)
    projdir = os.path.dirname(os.path.abspath(a.project))
    with open(a.project) as f:
        proj = json.load(f)
    ctx = {"W": proj.get("width", 1080), "H": proj.get("height", 1920),
           "fps": proj.get("fps", 30)}
    paths, hits = [], 0
    for seg in proj["segments"]:
        path, hit, key = render_segment(seg, ctx, projdir)
        paths.append(path)
        hits += hit
        print(f"[{'CACHE' if hit else 'BUILT'}] {seg['id']} ({key})")
    lst = os.path.join(CACHE, "concat.txt")
    with open(lst, "w") as f:
        for pth in paths:
            f.write(f"file '{pth}'\n")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", lst, "-c", "copy", a.out])
    print(f"assembled {a.out} — {hits}/{len(paths)} segments from cache")


def cmd_index(args):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    a = ap.parse_args(args)
    projdir = os.path.dirname(os.path.abspath(a.project))
    with open(a.project) as f:
        proj = json.load(f)
    ctx = {"W": proj.get("width", 1080), "H": proj.get("height", 1920),
           "fps": proj.get("fps", 30)}
    t = 0.0
    print(f"{'id':24} {'op':18} {'in':>8} {'out':>8} cache")
    for seg in proj["segments"]:
        params, hashes = resolve(seg.get("params", {}), projdir)
        key = hashlib.sha256(json.dumps(
            {"op": seg["op"],
             "params": {k: v for k, v in seg.get("params", {}).items()},
             "hashes": hashes, "canvas": [ctx["W"], ctx["H"], ctx["fps"]]},
            sort_keys=True).encode()).hexdigest()[:16]
        cached = os.path.exists(os.path.join(CACHE, f"{key}.mp4"))
        # duration estimate per op
        prm = seg.get("params", {})
        dur = prm.get("dur", prm.get("hold1", 0) + prm.get("fade", 0) + prm.get("hold2", 0))
        print(f"{seg['id']:24} {seg['op']:18} {t:8.2f} {t+dur:8.2f} "
              f"{'HIT ' if cached else 'MISS'} {key}")
        t += dur


def cmd_gc(args):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    a = ap.parse_args(args)
    projdir = os.path.dirname(os.path.abspath(a.project))
    with open(a.project) as f:
        proj = json.load(f)
    ctx = {"W": proj.get("width", 1080), "H": proj.get("height", 1920),
           "fps": proj.get("fps", 30)}
    live = set()
    for seg in proj["segments"]:
        params, hashes = resolve(seg.get("params", {}), projdir)
        key = hashlib.sha256(json.dumps(
            {"op": seg["op"],
             "params": {k: v for k, v in seg.get("params", {}).items()},
             "hashes": hashes, "canvas": [ctx["W"], ctx["H"], ctx["fps"]]},
            sort_keys=True).encode()).hexdigest()[:16]
        live.add(f"{key}.mp4")
    freed = 0
    for fn in os.listdir(CACHE):
        if fn.endswith(".mp4") and fn not in live and not fn.startswith("tmp-"):
            sz = os.path.getsize(os.path.join(CACHE, fn))
            os.remove(os.path.join(CACHE, fn))
            freed += sz
    print(f"pruned cache: freed {freed/1e6:.1f} MB")


if __name__ == "__main__":
    cmd, rest = sys.argv[1], sys.argv[2:]
    {"render": cmd_render, "index": cmd_index, "gc": cmd_gc}[cmd](rest)
