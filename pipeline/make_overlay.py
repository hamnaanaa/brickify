#!/usr/bin/env python3
"""Corner thumbnails for the assembly clips: top-left the source photo, top-right the 3D model render.
usage: make_overlay.py photo.png model.png out.png [--size 240 --margin 32]"""
import argparse
from PIL import Image, ImageDraw, ImageFilter

def square(im):
    w, h = im.size; s = min(w, h)
    return im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))

def card(im, size, radius, frame, on_white):
    im = im.convert("RGBA")
    if on_white:
        # crop a transparent render to its own bounds plus a small margin so the model fills the card
        bbox = im.getchannel("A").getbbox()
        if bbox:
            w, h = im.size; m = int(0.06 * max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
            im = im.crop((max(0, bbox[0] - m), max(0, bbox[1] - m), min(w, bbox[2] + m), min(h, bbox[3] + m)))
            side = max(im.size); pad = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            pad.paste(im, ((side - im.size[0]) // 2, (side - im.size[1]) // 2)); im = pad
    im = square(im).resize((size - 2 * frame, size - 2 * frame), Image.LANCZOS)
    base = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    if on_white:
        inner = Image.new("RGBA", im.size, (255, 255, 255, 255)); inner.alpha_composite(im); im = inner
    base.paste(im, (frame, frame))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    base.putalpha(mask)
    return base

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("photo"); ap.add_argument("model"); ap.add_argument("out")
    ap.add_argument("--canvas", type=int, default=1080); ap.add_argument("--size", type=int, default=360)
    ap.add_argument("--margin", type=int, default=32); ap.add_argument("--frame", type=int, default=6); ap.add_argument("--radius", type=int, default=26)
    a = ap.parse_args()
    canvas = Image.new("RGBA", (a.canvas, a.canvas), (0, 0, 0, 0))
    cards = [(card(Image.open(a.photo), a.size, a.radius, a.frame, False), (a.margin, a.margin)),
             (card(Image.open(a.model), a.size, a.radius, a.frame, True), (a.canvas - a.margin - a.size, a.margin))]
    # soft shadow
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for c, (x, y) in cards:
        sd.rounded_rectangle((x + 4, y + 8, x + a.size + 4, y + a.size + 8), radius=a.radius, fill=(10, 30, 50, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(shadow)
    for c, (x, y) in cards:
        canvas.alpha_composite(c, (x, y))
    canvas.save(a.out)
    print("wrote", a.out)

if __name__ == "__main__":
    main()
