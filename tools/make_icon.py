"""FH2 Manager icon: three stacked layers (mods stacked on the game), teal on the app's dark background."""
import sys

from PIL import Image, ImageDraw

BG, EDGE = (26, 31, 36, 255), (43, 52, 59, 255)
TEALS = [(0, 129, 138, 255), (0, 173, 181, 255), (0, 239, 209, 255)]   # bottom -> top, darker to brighter


def layer(d, cx, cy, w, h, fill, outline, ow):
    """a flat diamond (a layer seen from above at an angle)"""
    pts = [(cx, cy - h / 2), (cx + w / 2, cy), (cx, cy + h / 2), (cx - w / 2, cy)]
    d.polygon(pts, fill=fill, outline=outline, width=ow)


def draw(size):
    s = 4                                           # supersample, then shrink: smooth edges at every size
    n = size * s
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = int(n * 0.2)
    d.rounded_rectangle([0, 0, n - 1, n - 1], radius=r, fill=BG, outline=EDGE, width=max(1, n // 64))
    w, h = n * 0.70, n * 0.34
    step = n * 0.15 if size > 24 else n * 0.17
    cy0 = n * 0.5 + step
    ow = max(2, n // 48)
    for k, teal in enumerate(TEALS):
        layer(d, n / 2, cy0 - k * step, w, h, teal, BG, ow)
    return im.resize((size, size), Image.LANCZOS)


out_ico, out_png = sys.argv[1], sys.argv[2]
sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
imgs = [draw(s) for s in sizes]
imgs[-1].save(out_ico, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
sheet = Image.new("RGBA", (sum(sizes[:-1]) + 256 + 10 * len(sizes), 266), (18, 21, 24, 255))
x = 5
for im in imgs:
    sheet.paste(im, (x, 261 - im.size[1]), im)
    x += im.size[0] + 10
sheet.save(out_png)
print("icon written:", out_ico, sizes)
