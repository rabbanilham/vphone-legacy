#!/usr/bin/env python3
"""Generate a filmstrip PNG from a directory of framebuffer-at-*.ppm captures."""
import argparse
import glob
import os
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def make_strip(input_dir: Path, output_file: Path, thumb_w: int = 160):
    files = sorted(input_dir.glob('framebuffer-at-*.ppm'), key=lambda p: float(re.search(r'framebuffer-at-([\d.]+)s\.ppm', p.name).group(1)))
    if not files:
        print(f"No framebuffer-at-*.ppm found in {input_dir}")
        return
    images = []
    labels = []
    for f in files:
        m = re.search(r'framebuffer-at-([\d.]+)s\.ppm', f.name)
        sec = float(m.group(1))
        im = Image.open(f)
        aspect = im.height / im.width
        thumb_h = int(thumb_w * aspect)
        thumb = im.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        images.append(thumb)
        labels.append(f"{sec:.3f}s" if round(sec, 2) != round(sec, 3) else (f"{sec:.1f}s" if sec.is_integer() or (sec * 10).is_integer() else f"{sec:.2f}s"))

    pad = 4
    header_h = 24
    total_w = len(images) * (thumb_w + pad) + pad
    total_h = thumb_h + header_h + pad * 2

    strip = Image.new('RGB', (total_w, total_h), (25, 25, 25))
    draw = ImageDraw.Draw(strip)

    for idx, (im, lbl) in enumerate(zip(images, labels)):
        x = pad + idx * (thumb_w + pad)
        y = pad + header_h
        strip.paste(im, (x, y))
        draw.text((x + 4, pad + 4), lbl, fill=(255, 255, 255))

    output_file.parent.mkdir(parents=True, exist_ok=True)
    strip.save(output_file)
    print(f"Saved strip ({len(images)} frames) to {output_file}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input_dir', type=Path)
    parser.add_argument('output_file', type=Path)
    parser.add_argument('--width', type=int, default=160)
    args = parser.parse_args()
    make_strip(args.input_dir, args.output_file, args.width)
