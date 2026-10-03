#!/usr/bin/env python3
"""art-engine: layered image/video compositing for social posts.

Stateless ops; the agent manages versioning. See SKILL.md.
"""
import argparse, json, math, os, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

# ---------- helpers ----------

def parse_color(s):
    s = s.strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    if len(s) == 6:
        return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))
    if len(s) == 8:
        return tuple(int(s[i:i+2], 16) for i in (0, 2, 4, 6))
    raise ValueError(f'bad color: {s}')

def parse_size(s):
    w, h = s.lower().replace('x', ' ').split()
    return int(w), int(h)

def find_font(name):
    if os.path.isfile(name):
        return name
    cands = [
        f'/usr/share/fonts/truetype/dejavu/{name}.ttf',
        f'/usr/share/fonts/truetype/liberation/{name}.ttf',
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    try:
        out = subprocess.run(['fc-match', name, '--format=%{file}\n'],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        if out and os.path.isfile(out[0]):
            return out[0]
    except Exception:
        pass
    return '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

ANCHORS = {'top-left': (0, 0), 'top': (0.5, 0), 'top-right': (1, 0),
           'left': (0, 0.5), 'center': (0.5, 0.5), 'right': (1, 0.5),
           'bottom-left': (0, 1), 'bottom': (0.5, 1), 'bottom-right': (1, 1)}

def anchor_pos(anchor, cw, ch, lw, lh, margin=0):
    ax, ay = ANCHORS[anchor]
    return int(ax * (cw - lw) + (margin if ax == 0 else -margin if ax == 1 else 0)), \
           int(ay * (ch - lh) + (margin if ay == 0 else -margin if ay == 1 else 0))

def feather_alpha(img, px):
    if px and px > 0:
        a = img.getchannel('A').filter(ImageFilter.GaussianBlur(px))
        img.putalpha(a)
    return img

def run_ffmpeg(args):
    r = subprocess.run(['ffmpeg', '-y', *args], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:], file=sys.stderr)
        sys.exit(1)

# ---------- cutout ----------

def op_cutout(a):
    img = Image.open(a.input).convert('RGB')
    w, h = img.size
    if a.mode == 'chroma':
        key = np.array(parse_color(a.color), dtype=np.float32)
        arr = np.asarray(img).astype(np.float32)
        dist = np.linalg.norm(arr - key, axis=2)
        alpha = np.clip((dist - a.inner) / max(a.outer - a.inner, 1e-6), 0, 1)
        out = img.convert('RGBA')
        out.putalpha(Image.fromarray((alpha * 255).astype(np.uint8)))
    elif a.mode == 'luma':
        g = np.asarray(img.convert('L')).astype(np.float32)
        if a.dark:
            alpha = np.clip((g - a.inner) / max(a.outer - a.inner, 1e-6), 0, 1)
        else:
            alpha = np.clip(((255 - g) - a.inner) / max(a.outer - a.inner, 1e-6), 0, 1)
        out = img.convert('RGBA')
        out.putalpha(Image.fromarray((alpha * 255).astype(np.uint8)))
    elif a.mode in ('rounded-rect', 'ellipse', 'circle'):
        mask = Image.new('L', (w, h), 0)
        d = ImageDraw.Draw(mask)
        m = a.margin
        if a.mode == 'rounded-rect':
            d.rounded_rectangle([m, m, w - m, h - m], radius=a.radius, fill=255)
        else:
            d.ellipse([m, m, w - m, h - m], fill=255)
        out = img.convert('RGBA')
        out.putalpha(mask)
    else:
        sys.exit(f'unknown cutout mode: {a.mode}')
    feather_alpha(out, a.feather)
    out.save(a.out)
    print(f'cutout -> {a.out}')

# ---------- composite ----------

def op_composite(a):
    spec = json.load(open(a.spec)) if a.spec else {}
    layers_cli = json.loads(a.layers) if a.layers else []
    layers = spec.get('layers', []) + layers_cli
    cw, ch = parse_size(a.size or spec.get('canvas', '1080x1920'))
    bg = spec.get('bg', {})
    if bg.get('image'):
        canvas = Image.open(bg['image']).convert('RGB')
        canvas = ImageOps.fit(canvas, (cw, ch), Image.LANCZOS)
    else:
        canvas = Image.new('RGB', (cw, ch), parse_color(bg.get('color', '#000000')))
    canvas = canvas.convert('RGBA')
    for L in layers:
        li = Image.open(L['img']).convert('RGBA')
        if L.get('scale'):
            nw = max(1, int(li.width * L['scale']))
            nh = max(1, int(li.height * L['scale']))
            li = li.resize((nw, nh), Image.LANCZOS)
        if L.get('width'):
            nw = int(L['width'])
            nh = int(nw * li.height / li.width)
            li = li.resize((nw, nh), Image.LANCZOS)
        if L.get('rotate'):
            li = li.rotate(L['rotate'], expand=True, resample=Image.BICUBIC)
        if L.get('opacity', 1) < 1:
            al = li.getchannel('A').point(lambda v: int(v * L['opacity']))
            li.putalpha(al)
        feather_alpha(li, L.get('feather', 0))
        if L.get('pos'):
            x, y = int(L['pos'][0]), int(L['pos'][1])
        else:
            x, y = anchor_pos(L.get('anchor', 'center'), cw, ch, li.width, li.height,
                              L.get('margin', 40))
        if L.get('shadow'):
            sh = Image.new('RGBA', li.size, (0, 0, 0, 0))
            sa = li.getchannel('A').point(lambda v: int(v * 0.55))
            sh.putalpha(sa.filter(ImageFilter.GaussianBlur(18)))
            canvas.alpha_composite(sh, (x + 14, y + 18))
        blend = L.get('blend', 'normal')
        if blend == 'normal':
            canvas.alpha_composite(li, (x, y))
        else:
            region = canvas.crop((x, y, x + li.width, y + li.height)).convert('RGB')
            fg = li.convert('RGB')
            la = np.asarray(li.getchannel('A'), dtype=np.float32) / 255.0
            A = np.asarray(region).astype(np.float32) / 255.0
            B = np.asarray(fg).astype(np.float32) / 255.0
            if blend == 'screen':
                C = 1 - (1 - A) * (1 - B)
            elif blend == 'multiply':
                C = A * B
            elif blend == 'overlay':
                C = np.where(A < 0.5, 2 * A * B, 1 - 2 * (1 - A) * (1 - B))
            else:
                sys.exit(f'unknown blend: {blend}')
            C = (np.clip(C, 0, 1) * 255).astype(np.uint8)
            mixed = (A * 255 * (1 - la[..., None]) + C * la[..., None]).astype(np.uint8)
            canvas.paste(Image.fromarray(mixed), (x, y))
    canvas.convert('RGB').save(a.out, quality=95)
    print(f'composite -> {a.out}')

# ---------- grade ----------

def op_grade(a):
    img = Image.open(a.input).convert('RGB')
    if a.saturation != 1:
        img = ImageEnhance.Color(img).enhance(a.saturation)
    if a.contrast != 1:
        img = ImageEnhance.Contrast(img).enhance(a.contrast)
    if a.brightness != 1:
        img = ImageEnhance.Brightness(img).enhance(a.brightness)
    if a.sharpness != 1:
        img = ImageEnhance.Sharpness(img).enhance(a.sharpness)
    if a.warmth:
        arr = np.asarray(img).astype(np.float32)
        arr[..., 0] = np.clip(arr[..., 0] * (1 + a.warmth / 200), 0, 255)
        arr[..., 2] = np.clip(arr[..., 2] * (1 - a.warmth / 200), 0, 255)
        img = Image.fromarray(arr.astype(np.uint8))
    if a.fade:
        img = Image.blend(img, Image.new('RGB', img.size, (128, 128, 128)), a.fade * 0.6)
    if a.duotone:
        c1, c2 = parse_color(a.duotone[0]), parse_color(a.duotone[1])
        g = np.asarray(img.convert('L')).astype(np.float32) / 255.0
        arr = (np.array(c1)[None, None, :] * (1 - g[..., None]) +
               np.array(c2)[None, None, :] * g[..., None])
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    if a.vignette:
        w, h = img.size
        yy, xx = np.mgrid[0:h, 0:w]
        dist = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / math.sqrt(2)
        mask = np.clip(1 - a.vignette * dist ** 2, 0, 1)
        arr = np.asarray(img).astype(np.float32) * mask[..., None]
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    if a.grain:
        arr = np.asarray(img).astype(np.float32)
        noise = np.random.normal(0, a.grain * 255, arr.shape[:2])
        arr = np.clip(arr + noise[..., None], 0, 255)
        img = Image.fromarray(arr.astype(np.uint8))
    img.save(a.out, quality=95)
    print(f'grade -> {a.out}')

# ---------- text ----------

def wrap_text(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ''
    for wd in words:
        t = (cur + ' ' + wd).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines

def op_text(a):
    img = Image.open(a.input).convert('RGBA')
    font = ImageFont.truetype(find_font(a.font), a.size)
    draw = ImageDraw.Draw(img)
    max_w = int(a.max_width) if a.max_width else img.width - 2 * a.margin
    lines = wrap_text(draw, a.text, font, max_w)
    lh = int(a.size * (a.line_spacing or 1.15))
    block_h = lh * len(lines)
    tw = max(draw.textlength(l, font=font) for l in lines)
    if a.pos:
        x, y = int(a.pos[0]), int(a.pos[1])
    else:
        x, y = anchor_pos(a.anchor, img.width, img.height, int(tw), block_h, a.margin)
    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=font)
        lx = x if a.align == 'left' else x + (tw - lw) / 2 if a.align == 'center' else x + tw - lw
        ly = y + i * lh
        if a.shadow:
            draw.text((lx + 3, ly + 3), line, font=font, fill=(0, 0, 0, 160))
        draw.text((lx, ly), line, font=font, fill=parse_color(a.color) + (int(255 * a.opacity),),
                  stroke_width=a.stroke_width, stroke_fill=parse_color(a.stroke_color or '#000000'))
    img.convert('RGB').save(a.out, quality=95)
    print(f'text -> {a.out}')

# ---------- format ----------

def op_format(a):
    sizes = {'story': (1080, 1920), 'feed': (1080, 1350), 'square': (1080, 1080),
             'reel_cover': (1080, 1920)}
    if a.to in sizes:
        w, h = sizes[a.to]
    else:
        w, h = parse_size(a.to)
    img = Image.open(a.input).convert('RGB')
    if a.mode == 'cover':
        out = ImageOps.fit(img, (w, h), Image.LANCZOS)
    else:
        out = img.copy()
        out.thumbnail((w, h), Image.LANCZOS)
        bg = ImageOps.fit(img.filter(ImageFilter.GaussianBlur(40)), (w, h), Image.LANCZOS)
        bg = ImageEnhance.Brightness(bg).enhance(0.55)
        bg.paste(out, ((w - out.width) // 2, (h - out.height) // 2))
        out = bg
    out.save(a.out, quality=95)
    print(f'format -> {a.out}')

# ---------- video ----------

def op_kenburns(a):
    w, h = 1080, 1920
    frames = int(a.dur * 30)
    zin = 'zoom+0.0012' if a.zoom == 'in' else 'max(zoom-0.0012,1.0)'
    z0 = '1.0' if a.zoom == 'in' else '1.25'
    vf = (f"scale=2160:-2,zoompan=z='{zin}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={w}x{h}:fps=30")
    run_ffmpeg(['-loop', '1', '-i', a.input, '-vf', vf, '-t', str(a.dur),
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', a.out])
    print(f'kenburns -> {a.out}')

def norm_v(inp, idx):
    return (f"[{idx}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1,fps=30[v{idx}]")

def op_concat(a):
    ins = a.inputs.split(',')
    fc = ';'.join(norm_v(p, i) for i, p in enumerate(ins))
    fc += ';' + ''.join(f"[v{i}]" for i in range(len(ins))) + f"concat=n={len(ins)}:v=1:a=0[out]"
    args = []
    for p in ins:
        args += ['-i', p]
    args += ['-filter_complex', fc, '-map', '[out]', '-c:v', 'libx264',
             '-pix_fmt', 'yuv420p', '-movflags', '+faststart', a.out]
    run_ffmpeg(args)
    print(f'concat -> {a.out}')

def op_overlay(a):
    x, y = a.pos
    en = f"between(t,{a.start},{a.start + a.dur})"
    fades = ''
    if a.fade:
        fades = (f",fade=t=in:st={a.start}:d={a.fade}:alpha=1"
                 f",fade=t=out:st={a.start + a.dur - a.fade}:d={a.fade}:alpha=1")
    fc = (f"[1:v]format=rgba{fades}[ov];"
          f"[0:v][ov]overlay={x}:{y}:enable='{en}':format=yuv420[out]")
    run_ffmpeg(['-i', a.input, '-i', a.overlay, '-filter_complex', fc,
                '-map', '[out]', '-map', '0:a?', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-movflags', '+faststart', '-shortest', a.out])
    print(f'overlay -> {a.out}')

def op_muxaudio(a):
    run_ffmpeg(['-i', a.input, '-i', a.audio, '-c:v', 'copy', '-c:a', 'aac',
                '-map', '0:v:0', '-map', '1:a:0', '-shortest',
                '-movflags', '+faststart', a.out])
    print(f'muxaudio -> {a.out}')

# ---------- artboard (widget WYSIWYG render) ----------

ARTBOARD_FORMATS = {
    'story': (1080, 1920),      # IG/TikTok story, reels
    'portrait': (1080, 1350),   # IG feed 4:5
    'square': (1080, 1080),     # IG feed 1:1
    'landscape': (1920, 1080),  # X / general 16:9
    'pin': (1000, 1500),        # Pinterest 2:3
    'thumb': (1280, 720),       # video thumbnail 16:9
}

def _paste_clipped(canvas, li, x, y):
    cw, ch = canvas.size
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(li.width, cw - x), min(li.height, ch - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    region = li.crop((sx0, sy0, sx1, sy1))
    canvas.alpha_composite(region, (x + sx0, y + sy0))

def op_artboard(a):
    st = json.load(open(a.state))
    if a.size:
        cw, ch = [int(v) for v in a.size.lower().split('x')]
    elif a.format:
        cw, ch = ARTBOARD_FORMATS[a.format]
    else:
        cw, ch = ARTBOARD_FORMATS.get(st.get('format', 'story'), (1080, 1920))
    ref_w, ref_h = st.get('canvas', [1080, 1920])
    bg = st.get('bg', {})
    if bg.get('image'):
        canvas = Image.open(bg['image']).convert('RGB')
        canvas = ImageOps.fit(canvas, (cw, ch), Image.LANCZOS)
    else:
        canvas = Image.new('RGB', (cw, ch), parse_color(bg.get('color', '#111111')))
    canvas = canvas.convert('RGBA')
    for L in st.get('layers', []):
        if 'fx' in L:  # fractional coords (v2 widget)
            fx, fy, fw = L['fx'], L['fy'], L['fw']
        else:          # v1 absolute coords -> normalize
            fx, fy, fw = L.get('cx', ref_w / 2) / ref_w, L.get('cy', ref_h / 2) / ref_h, L.get('base_w', 520) / ref_w
        cx, cy, bw = fx * cw, fy * ch, fw * cw
        li = Image.open(L['img']).convert('RGBA')
        nw = max(1, int(bw * L.get('scale', 1.0)))
        nh = max(1, int(nw * li.height / li.width))
        li = li.resize((nw, nh), Image.LANCZOS)
        if L.get('rotate'):
            li = li.rotate(L['rotate'], expand=True, resample=Image.BICUBIC)
        op = L.get('opacity', 1)
        if op < 1:
            al = li.getchannel('A').point(lambda v, o=op: int(v * o))
            li.putalpha(al)
        x = int(cx - li.width / 2)
        y = int(cy - li.height / 2)
        if L.get('shadow', True):
            sh = Image.new('RGBA', li.size, (0, 0, 0, 0))
            sa = li.getchannel('A').point(lambda v: int(v * 0.5))
            sh.putalpha(sa.filter(ImageFilter.GaussianBlur(16)))
            _paste_clipped(canvas, sh, x + 12, y + 16)
        _paste_clipped(canvas, li, x, y)
    canvas.convert('RGB').save(a.out, quality=95)
    print(f'artboard -> {a.out} ({cw}x{ch})')

# ---------- cli ----------

def main():
    p = argparse.ArgumentParser(prog='ae.py')
    sub = p.add_subparsers(dest='op', required=True)

    c = sub.add_parser('cutout')
    c.add_argument('--in', dest='input', required=True)
    c.add_argument('--mode', default='chroma', choices=['chroma', 'luma', 'rounded-rect', 'ellipse', 'circle'])
    c.add_argument('--color', default='#00ff00')
    c.add_argument('--inner', type=float, default=30)
    c.add_argument('--outer', type=float, default=90)
    c.add_argument('--dark', action='store_true', help='luma: key out dark')
    c.add_argument('--margin', type=int, default=0)
    c.add_argument('--radius', type=int, default=60)
    c.add_argument('--feather', type=float, default=2)
    c.add_argument('--out', required=True)

    c = sub.add_parser('composite')
    c.add_argument('--spec', help='JSON spec file')
    c.add_argument('--layers', default='', help='JSON list of extra layers')
    c.add_argument('--size', default='')
    c.add_argument('--out', required=True)

    c = sub.add_parser('grade')
    c.add_argument('--in', dest='input', required=True)
    c.add_argument('--saturation', type=float, default=1)
    c.add_argument('--contrast', type=float, default=1)
    c.add_argument('--brightness', type=float, default=1)
    c.add_argument('--sharpness', type=float, default=1)
    c.add_argument('--warmth', type=float, default=0)
    c.add_argument('--fade', type=float, default=0)
    c.add_argument('--duotone', nargs=2, default=None)
    c.add_argument('--vignette', type=float, default=0)
    c.add_argument('--grain', type=float, default=0)
    c.add_argument('--out', required=True)

    c = sub.add_parser('text')
    c.add_argument('--in', dest='input', required=True)
    c.add_argument('--text', required=True)
    c.add_argument('--font', default='DejaVuSans-Bold')
    c.add_argument('--size', type=int, default=90)
    c.add_argument('--color', default='#ffffff')
    c.add_argument('--stroke-width', type=int, default=3)
    c.add_argument('--stroke-color', default='#000000')
    c.add_argument('--opacity', type=float, default=1)
    c.add_argument('--anchor', default='bottom')
    c.add_argument('--pos', nargs=2, type=int, default=None)
    c.add_argument('--align', default='center', choices=['left', 'center', 'right'])
    c.add_argument('--margin', type=int, default=70)
    c.add_argument('--max-width', type=int, default=0)
    c.add_argument('--line-spacing', type=float, default=1.15)
    c.add_argument('--shadow', action='store_true')
    c.add_argument('--out', required=True)

    c = sub.add_parser('format')
    c.add_argument('--in', dest='input', required=True)
    c.add_argument('--to', default='story')
    c.add_argument('--mode', default='cover', choices=['cover', 'contain'])
    c.add_argument('--out', required=True)

    c = sub.add_parser('kenburns')
    c.add_argument('--in', dest='input', required=True)
    c.add_argument('--dur', type=float, default=8)
    c.add_argument('--zoom', default='in', choices=['in', 'out'])
    c.add_argument('--out', required=True)

    c = sub.add_parser('concat')
    c.add_argument('--inputs', required=True, help='comma-separated mp4s')
    c.add_argument('--out', required=True)

    c = sub.add_parser('overlay')
    c.add_argument('--in', dest='input', required=True)
    c.add_argument('--overlay', required=True)
    c.add_argument('--pos', nargs=2, type=int, default=[0, 0])
    c.add_argument('--start', type=float, default=0)
    c.add_argument('--dur', type=float, default=3)
    c.add_argument('--fade', type=float, default=0.5)
    c.add_argument('--out', required=True)

    c = sub.add_parser('muxaudio')
    c.add_argument('--in', dest='input', required=True)
    c.add_argument('--audio', required=True)
    c.add_argument('--out', required=True)

    c = sub.add_parser('artboard')
    c.add_argument('--state', required=True, help='widget state JSON file')
    c.add_argument('--format', choices=['story', 'portrait', 'square', 'landscape', 'pin', 'thumb'],
                   help='named output format (default: state format or story)')
    c.add_argument('--size', help='explicit WxH override, e.g. 1080x1080')
    c.add_argument('--out', required=True)

    a = p.parse_args()
    {'cutout': op_cutout, 'composite': op_composite, 'grade': op_grade,
     'text': op_text, 'format': op_format, 'kenburns': op_kenburns,
     'concat': op_concat, 'overlay': op_overlay, 'muxaudio': op_muxaudio,
     'artboard': op_artboard}[a.op](a)

if __name__ == '__main__':
    main()
