#!/usr/bin/env python3
"""vboard — HTTP bridge for the video board editor (Phase 2).

Serves composition/board.html and exposes the composition plus vcompose
renders over a tiny localhost API. The board never touches ffmpeg; this
bridge does.

    vboard.py comp.json [--port 8901] [--open] [--host 127.0.0.1]

API:
    GET  /                     board.html
    GET  /api/comp             {path, comp, probes:{src:{w,h,dur,fps}}}
    POST /api/comp             {comp} -> validates + saves to disk
    POST /api/proxy            {comp} -> renders 270px proxy -> {url, duration}
                                 (proxy includes the mixed audio track)
    POST /api/render           {comp?, out?} -> full-res vcompose render
    GET  /api/file?path=..     serve an allowed local file (layer ghost)
    GET  /api/poster?src=..    JPEG poster frame of a video source
    GET  /p/<name>             serve rendered outputs
"""
import argparse
import copy
import hashlib
import http.server
import json
import mimetypes
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL, "bin"))
import vcompose  # noqa: E402  (reuse probe/run/cache helpers)

BOARD_HTML = os.path.join(SKILL, "composition", "board.html")
WORK = os.path.join(os.path.expanduser("~"), ".cache", "vboard")
os.makedirs(WORK, exist_ok=True)
PROXY_W = 270

ALLOWED_PREFIXES = ("/home/hatch/workspace/", "/home/hatch/.cache/",
                    "/tmp/")


def allowed(path):
    ap = os.path.abspath(path)
    return ap.startswith(ALLOWED_PREFIXES) and os.path.isfile(ap)


def sha16(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()) \
        .hexdigest()[:16]


def sxp_val(v, s, minimum=None):
    """Scale one px-denominated value (scalar or [dx,dy] pair)."""
    def one(x):
        y = round(x * s, 2)
        return max(minimum, y) if minimum is not None else y
    if isinstance(v, (list, tuple)):
        return [one(x) for x in v]
    return one(v)


def sx_param(v, s, minimum=None):
    """Scale a px param that may be a scalar or a keyframe list
    [{t, value, ease}]. Keyframe values (incl. [dx,dy] pairs) scale too."""
    if isinstance(v, list) and v and isinstance(v[0], dict) \
            and "t" in v[0]:
        return [{**kf, "value": sxp_val(kf["value"], s, minimum)}
                for kf in v]
    return sxp_val(v, s, minimum)


def scale_comp(comp, target_w, compdir):
    """Return a canvas-scaled copy for proxy renders. Timeline-affecting
    values (cuts src_in/out, hits, in/out, durations) are untouched; only
    spatial px values scale. src paths are absolutized so the proxy JSON
    can live outside the comp directory."""
    c = copy.deepcopy(comp)
    s = target_w / comp["width"]
    c["width"] = max(2, int(round(comp["width"] * s)))
    c["height"] = max(2, int(round(comp["height"] * s)))

    def sx(v):
        return round(v * s, 2)

    for L in c["layers"]:
        if L.get("src") and not os.path.isabs(L["src"]):
            L["src"] = os.path.join(compdir, L["src"])
        t = L.get("transform")
        if t:
            for k in ("x", "y"):
                if k in t:
                    t[k] = sx_param(t[k], s)
        fr = L.get("frame")
        if fr:
            fr["w"] = sx(fr["w"])
            fr["h"] = sx(fr["h"])
        if L["type"] == "text" and "font_size" in L:
            L["font_size"] = max(6, sx(L["font_size"]))
        for e in L.get("effects", []):
            et = e["type"]
            if et == "glitch" and "shift" in e:
                e["shift"] = sx_param(e["shift"], s, 1)
            if et == "blur" and "sigma" in e:
                e["sigma"] = sx_param(e["sigma"], s, 0.5)
            if et == "rgbshift":
                for k in ("r", "g", "b"):
                    if k in e:
                        e[k] = sx_param(e[k], s)
            if et == "shake" and "amp" in e:
                e["amp"] = sx_param(e["amp"], s)
            if et == "scanlines" and "spacing" in e:
                e["spacing"] = sx_param(e["spacing"], s, 1)
    # audio srcs must be absolute too: the proxy JSON lives in ~/.cache
    for a in c.get("audio", []):
        if a.get("src") and not os.path.isabs(a["src"]):
            a["src"] = os.path.join(compdir, a["src"])
    return c, s


def visible_comp(comp):
    """Copy with _hidden layers removed (board-only metadata)."""
    c = copy.deepcopy(comp)
    c["layers"] = [L for L in c["layers"] if not L.get("_hidden")]
    return c


class BoardState:
    def __init__(self, comp_path):
        self.path = os.path.abspath(comp_path)
        self.compdir = os.path.dirname(self.path)
        self.lock = threading.Lock()
        self.render_lock = threading.Lock()
        self.load()

    def load(self):
        with open(self.path) as f:
            self.comp = json.load(f)
        for k in ("width", "height", "fps", "duration", "layers"):
            if k not in self.comp:
                raise ValueError(f"composition missing '{k}'")

    def save(self, comp):
        for k in ("width", "height", "fps", "duration", "layers"):
            if k not in comp:
                raise ValueError(f"composition missing '{k}'")
        with open(self.path, "w") as f:
            json.dump(comp, f, indent=1)
        self.comp = comp

    def probes(self):
        out = {}
        for L in self.comp["layers"]:
            if L["type"] == "video":
                src = L["src"] if os.path.isabs(L["src"]) \
                    else os.path.join(self.compdir, L["src"])
                if src not in out and os.path.isfile(src):
                    try:
                        out[src] = vcompose.probe(src)
                    except Exception:
                        pass
        for a in self.comp.get("audio", []):
            src = a["src"] if os.path.isabs(a["src"]) \
                else os.path.join(self.compdir, a["src"])
            if src not in out and os.path.isfile(src):
                try:
                    out[src] = {"dur": vcompose.ffprobe_dur(src)}
                except Exception:
                    pass
        return out

    def render_proxy(self, comp):
        """Render a 270px proxy of the (visible) composition."""
        comp = visible_comp(comp)
        scaled, s = scale_comp(comp, PROXY_W, self.compdir)
        key = sha16(scaled)
        out = os.path.join(WORK, f"proxy-{key}.mp4")
        if not os.path.exists(out):
            tmp = os.path.join(WORK, f"proxy-{key}.json")
            with open(tmp, "w") as f:
                json.dump(scaled, f)
            with self.render_lock:
                if not os.path.exists(out):  # recheck under lock
                    r = subprocess.run(
                        [sys.executable,
                         os.path.join(SKILL, "bin", "vcompose.py"),
                         "render", tmp, "-o", out],
                        capture_output=True, text=True)
                    if r.returncode != 0:
                        raise RuntimeError(
                            "proxy render failed:\n" + r.stderr[-1500:])
            try:
                os.remove(tmp)
            except OSError:
                pass
        dur = vcompose.ffprobe_dur(out)
        return f"/p/proxy-{key}.mp4", dur, s

    def render_full(self, out_path=None):
        """Full-res render of the comp as currently saved on disk
        (_hidden layers excluded)."""
        if out_path is None:
            base = os.path.splitext(os.path.basename(self.path))[0]
            out_path = os.path.join(self.compdir, base + ".board.mp4")
        filtered = visible_comp(self.comp)
        key = sha16(filtered)
        tmp = os.path.join(self.compdir, f".vboard-full-{key}.json")
        with open(tmp, "w") as f:
            json.dump(filtered, f)
        try:
            with self.render_lock:
                r = subprocess.run(
                    [sys.executable,
                     os.path.join(SKILL, "bin", "vcompose.py"),
                     "render", tmp, "-o", out_path, "--force"],
                    capture_output=True, text=True,
                    stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    raise RuntimeError("render failed:\n" + r.stderr[-1500:])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        # serve a copy for download
        served = os.path.join(WORK, f"render-{key}.mp4")
        shutil.copy(out_path, served)
        dur = vcompose.ffprobe_dur(out_path)
        return out_path, f"/p/render-{key}.mp4", dur


class Handler(http.server.BaseHTTPRequestHandler):
    state = None

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=500):
        self._json({"ok": False, "error": msg}, code)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                data = open(BOARD_HTML, "rb").read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/comp":
                with self.state.lock:
                    comp = copy.deepcopy(self.state.comp)
                    probes = self.state.probes()
                self._json({"ok": True, "path": self.state.path,
                            "comp": comp, "probes": probes})
            elif u.path == "/api/file":
                p = q.get("path", [None])[0]
                if not p or not allowed(p):
                    return self._err("file not allowed", 403)
                mime = mimetypes.guess_type(p)[0] or \
                    "application/octet-stream"
                data = open(p, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/poster":
                src = q.get("src", [None])[0]
                if not src or not allowed(src):
                    return self._err("file not allowed", 403)
                key = "poster-" + sha16(src) + ".jpg"
                out = os.path.join(WORK, key)
                if not os.path.exists(out):
                    pr = vcompose.probe(src)
                    t = max(0.1, pr["dur"] * 0.1)
                    # output-side seek (accurate); never input-seek
                    r = subprocess.run(
                        ["ffmpeg", "-y", "-v", "error", "-i", src,
                         "-ss", str(t), "-frames:v", "1",
                         "-vf", f"scale={PROXY_W}:-2", out],
                        capture_output=True, text=True)
                    if r.returncode != 0:
                        return self._err("poster failed", 500)
                data = open(out, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path.startswith("/p/"):
                name = os.path.basename(u.path)
                p = os.path.join(WORK, name)
                if not os.path.isfile(p):
                    return self._err("not found", 404)
                data = open(p, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._err("not found", 404)
        except Exception as e:  # noqa: BLE001
            self._err(str(e)[:500])

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/comp":
                comp = body.get("comp")
                if not isinstance(comp, dict):
                    return self._err("missing comp", 400)
                with self.state.lock:
                    self.state.save(comp)
                self._json({"ok": True})
            elif u.path == "/api/proxy":
                comp = body.get("comp")
                if not isinstance(comp, dict):
                    with self.state.lock:
                        comp = copy.deepcopy(self.state.comp)
                url, dur, s = self.state.render_proxy(comp)
                self._json({"ok": True, "url": url, "duration": dur,
                            "proxy_scale": s})
            elif u.path == "/api/render":
                comp = body.get("comp")
                out = body.get("out")
                with self.state.lock:
                    if isinstance(comp, dict):
                        self.state.save(comp)
                    path, url, dur = self.state.render_full(out)
                self._json({"ok": True, "path": path, "url": url,
                            "duration": dur})
            else:
                self._err("not found", 404)
        except Exception as e:  # noqa: BLE001
            self._err(str(e)[:800])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("comp", help="composition JSON to edit")
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true",
                    help="open the board in a browser")
    a = ap.parse_args()
    if not os.path.exists(BOARD_HTML):
        raise SystemExit(f"board.html missing: {BOARD_HTML}")
    Handler.state = BoardState(a.comp)
    # warm the proxy so first paint is instant
    try:
        url, dur, s = Handler.state.render_proxy(Handler.state.comp)
        print(f"proxy ready: {dur:.2f}s")
    except Exception as e:  # noqa: BLE001
        print(f"proxy warmup failed (will retry on load): {e}")
    srv = socketserver.ThreadingTCPServer(
        (a.host, a.port), Handler, bind_and_activate=False)
    srv.allow_reuse_address = True
    srv.server_bind()
    srv.server_activate()
    url = f"http://{a.host}:{a.port}/"
    print(f"video board -> {url}")
    print(f"editing {Handler.state.path}")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
