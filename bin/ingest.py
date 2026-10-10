#!/usr/bin/env python3
"""Video ingest for the art engine.

Formal intake for submitted videos: probe, scene-change keyframes,
a 1fps contact sheet, and a review template the agent fills with
timestamped beats. Later, `grab` / `clip` pull exact frames or clips
by timestamp — the dynamic screenshot/clip capability.

Usage:
  ingest.py ingest <video.mp4> --out <dir> [--scene 0.4] [--thumb-fps 1]
  ingest.py grab   <video.mp4> --at 12.5 --out frame.png
  ingest.py clip   <video.mp4> --from 12.5 --to 16.0 --out clip.mp4 [--copy]
"""
import argparse, json, math, os, subprocess, sys
from PIL import Image


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f'{" ".join(cmd[:3])} failed: {r.stderr[:400]}')
    return r


def probe(path):
    r = run(['ffprobe', '-v', 'error',
             '-show_entries', 'format=duration,size:stream=width,height,avg_frame_rate,codec_name',
             '-of', 'json', path])
    j = json.loads(r.stdout)
    st = j['streams'][0]
    num, den = st.get('avg_frame_rate', '30/1').split('/')
    return {
        'duration': float(j['format']['duration']),
        'size': int(j['format']['size']),
        'width': st['width'], 'height': st['height'],
        'fps': float(num) / float(den) if float(den) else 30.0,
        'codec': st.get('codec_name', '?'),
    }


def cmd_ingest(a):
    meta = probe(a.video)
    os.makedirs(a.out, exist_ok=True)
    kf_dir = os.path.join(a.out, 'keyframes')
    os.makedirs(kf_dir, exist_ok=True)

    # Scene-change keyframes at source resolution, pts in filename.
    run(['ffmpeg', '-y', '-v', 'error', '-i', a.video,
         '-vf', f"select='gt(scene,{a.scene})'",
         '-vsync', 'vfr', '-frame_pts', '1',
         os.path.join(kf_dir, 'kf_%d.png')])
    fps = meta['fps']
    kfs = []
    for f in sorted(os.listdir(kf_dir)):
        if f.startswith('kf_') and f.endswith('.png'):
            pts = int(f[3:-4])
            t = round(pts / fps, 2)
            new = f'kf_{t:07.2f}.png'
            os.rename(os.path.join(kf_dir, f), os.path.join(kf_dir, new))
            kfs.append({'t': t, 'file': f'keyframes/{new}'})
    # Cap: keep evenly spaced if too many.
    if len(kfs) > 48:
        step = len(kfs) / 48
        kfs = [kfs[int(i * step)] for i in range(48)]
    meta['keyframes'] = kfs
    meta['scene_threshold'] = a.scene

    # 1fps thumbnails -> contact sheet.
    th_dir = os.path.join(a.out, '.thumbs')
    os.makedirs(th_dir, exist_ok=True)
    run(['ffmpeg', '-y', '-v', 'error', '-i', a.video,
         '-vf', f'fps={a.thumb_fps},scale=240:-1',
         os.path.join(th_dir, 'th_%03d.jpg')])
    thumbs = sorted(f for f in os.listdir(th_dir) if f.endswith('.jpg'))
    imgs = [Image.open(os.path.join(th_dir, f)) for f in thumbs]
    cols = 6
    rows = math.ceil(len(imgs) / cols)
    tw, th = imgs[0].size
    sheet = Image.new('RGB', (cols * tw, rows * th), 'black')
    for i, im in enumerate(imgs):
        sheet.paste(im, ((i % cols) * tw, (i // cols) * th))
    sheet.save(os.path.join(a.out, 'contact.jpg'), quality=82)
    for f in thumbs:
        os.remove(os.path.join(th_dir, f))
    os.rmdir(th_dir)
    meta['contact'] = 'contact.jpg'
    meta['thumb_fps'] = a.thumb_fps

    with open(os.path.join(a.out, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)
    review = os.path.join(a.out, 'review.json')
    if not os.path.exists(review):
        with open(review, 'w') as f:
            json.dump({'beats': []}, f, indent=1)
    print(f'ingested {a.video}: {meta["duration"]:.1f}s, '
          f'{len(kfs)} keyframes, contact sheet, review template -> {a.out}')


def cmd_grab(a):
    run(['ffmpeg', '-y', '-v', 'error', '-ss', str(a.at), '-i', a.video,
         '-frames:v', '1', a.out])
    print(f'frame @ {a.at}s -> {a.out}')


def cmd_clip(a):
    cmd = ['ffmpeg', '-y', '-v', 'error', '-ss', str(a.From), '-to', str(a.to),
           '-i', a.video]
    cmd += ['-c', 'copy'] if a.copy else ['-c:v', 'libx264', '-crf', '18',
                                          '-preset', 'fast', '-c:a', 'aac']
    cmd.append(a.out)
    run(cmd)
    print(f'clip {a.From}s-{a.to}s -> {a.out}')


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    pi = sub.add_parser('ingest')
    pi.add_argument('video')
    pi.add_argument('--out', required=True)
    pi.add_argument('--scene', type=float, default=0.4)
    pi.add_argument('--thumb-fps', type=float, default=1.0)
    pg = sub.add_parser('grab')
    pg.add_argument('video')
    pg.add_argument('--at', type=float, required=True)
    pg.add_argument('--out', required=True)
    pc = sub.add_parser('clip')
    pc.add_argument('video')
    pc.add_argument('--from', dest='From', type=float, required=True)
    pc.add_argument('--to', type=float, required=True)
    pc.add_argument('--out', required=True)
    pc.add_argument('--copy', action='store_true')
    a = p.parse_args()
    {'ingest': cmd_ingest, 'grab': cmd_grab, 'clip': cmd_clip}[a.cmd](a)


if __name__ == '__main__':
    main()
