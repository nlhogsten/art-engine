#!/usr/bin/env python3
"""Build an artboard.html widget from any folder of images.

Usage:
  build_artboard.py --assets DIR --project DIR [--layout pile|grid] [--seed seed.json] [--covers a,b,c]

  --assets   folder of jpg/png/webp images (one layer per image)
  --project  project dir; writes <project>/artboard.html
  --layout   auto-arrange: 'pile' (scattered, default) or 'grid'
  --seed     JSON list of {id, fx, fy, fw, rotate} for an exact arrangement
  --covers   comma-separated basenames (no extension), in order; default: all, sorted
"""
import argparse
import base64
import hashlib
import io
import json
import os
from PIL import Image

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTS = ('.jpg', '.jpeg', '.png', '.webp')


def frand(key, salt):
    h = hashlib.md5(f'{key}:{salt}'.encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def auto_layout(ids, style):
    n = len(ids)
    layers = []
    if style == 'grid':
        cols = 2 if n <= 6 else 3
        rows = (n + cols - 1) // cols
        for i, cid in enumerate(ids):
            r, c = divmod(i, cols)
            layers.append({
                'id': cid, 'fx': (c + 0.5) / cols, 'fy': 0.12 + 0.76 * (r + 0.5) / rows,
                'fw': 0.92 / cols, 'rotate': 0,
            })
    else:  # pile: loose scattered arrangement, deterministic per filename
        cols = 2 if n <= 6 else 3
        rows = (n + cols - 1) // cols
        for i, cid in enumerate(ids):
            r, c = divmod(i, cols)
            layers.append({
                'id': cid,
                'fx': min(0.95, max(0.05, (c + 0.5) / cols + (frand(cid, 'x') - 0.5) * 0.12)),
                'fy': min(0.93, max(0.07, 0.12 + 0.76 * (r + 0.5) / rows + (frand(cid, 'y') - 0.5) * 0.08)),
                'fw': (0.88 + frand(cid, 'w') * 0.14) / cols,
                'rotate': round((frand(cid, 'r') - 0.5) * 14),
            })
    return layers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--assets', required=True)
    ap.add_argument('--project', required=True)
    ap.add_argument('--layout', default='pile', choices=['pile', 'grid'])
    ap.add_argument('--seed', default=None)
    ap.add_argument('--covers', default=None)
    a = ap.parse_args()

    assets = os.path.expanduser(a.assets)
    project = os.path.expanduser(a.project)
    files = sorted(f for f in os.listdir(assets)
                   if f.lower().endswith(EXTS) and os.path.isfile(os.path.join(assets, f)))
    by_id = {os.path.splitext(f)[0]: f for f in files}
    if a.covers:
        ids = [c.strip() for c in a.covers.split(',') if c.strip() in by_id]
    elif a.seed:
        seed_ids = [s['id'] for s in json.load(open(os.path.expanduser(a.seed))) if s['id'] in by_id]
        ids = seed_ids + [c for c in sorted(by_id) if c not in seed_ids]
    else:
        ids = sorted(by_id)
    if not ids:
        raise SystemExit('no images found in ' + assets)

    thumbs = {}
    for cid in ids:
        im = Image.open(os.path.join(assets, by_id[cid])).convert('RGB')
        im.thumbnail((300, 300), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=60)
        thumbs[cid] = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()

    if a.seed:
        seed = {s['id']: s for s in json.load(open(os.path.expanduser(a.seed)))}
        layout = []
        for cid in ids:
            s = seed.get(cid, {})
            layout.append({'id': cid, 'fx': s.get('fx', 0.5), 'fy': s.get('fy', 0.5),
                           'fw': s.get('fw', 0.48), 'rotate': s.get('rotate', 0)})
    else:
        layout = auto_layout(ids, a.layout)

    layers = [{'id': l['id'], 'img': os.path.join(assets, by_id[l['id']]),
               'fx': l['fx'], 'fy': l['fy'], 'fw': l['fw'],
               'scale': 1.0, 'rotate': l['rotate'], 'opacity': 1.0}
              for l in layout]

    tpl = open(os.path.join(SKILL, 'artboard_template.html')).read()
    html = tpl.replace('%%THUMBS%%', json.dumps(thumbs)).replace('%%LAYERS%%', json.dumps(layers))
    os.makedirs(project, exist_ok=True)
    out = os.path.join(project, 'artboard.html')
    open(out, 'w').write(html)
    print(f'{out} ({len(ids)} layers, {os.path.getsize(out)} bytes)')


if __name__ == '__main__':
    main()
