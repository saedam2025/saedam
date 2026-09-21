from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


TILE_W = 240
TILE_H = 135
COLS = 7
ROWS = 7

EFFECTS = [
    "stars", "bubbles", "blobs", "orbs", "waves", "geometry", "rings",
    "polygons", "lines", "starPop", "starSprinkle", "glitterField",
    "cometTwinkle", "neonGeometryFlow", "snowfall", "fireflies", "aurora",
    "confetti", "lanterns", "rippleWaves", "meteorShower", "crystalPrisms",
    "cosmicDust", "floatingHearts", "cyberLines", "matrixDrops",
    "autumnLeaves", "circuitPaths", "electricSparks", "diamondPulse",
    "lightPillars", "magicRunes", "plasmaOrbs", "stardustTrails",
    "springBreeze", "cherryBlossoms", "summerVacation", "chuseokMoon",
    "seollalRibbons", "christmasMagic", "peperoDay", "roseDay",
    "christmasSeason2",
    "chuseokHangawi",
]

WHITE = (255, 255, 255, 210)
CYAN = (103, 232, 249, 220)
BLUE = (96, 165, 250, 210)
PINK = (244, 114, 182, 215)
PURPLE = (192, 132, 252, 215)
GOLD = (253, 224, 71, 220)
GREEN = (110, 231, 183, 215)
RED = (251, 113, 133, 220)


def rng_for(name: str) -> random.Random:
    return random.Random(f"saedam-theme-effect:{name}")


def star_points(cx: float, cy: float, outer: float, inner: float, points: int = 5):
    result = []
    for index in range(points * 2):
        angle = -math.pi / 2 + index * math.pi / points
        radius = outer if index % 2 == 0 else inner
        result.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return result


def random_dots(draw: ImageDraw.ImageDraw, rng: random.Random, count: int, colors, radius=(1, 4)):
    for _ in range(count):
        x = rng.randint(5, TILE_W - 5)
        y = rng.randint(5, TILE_H - 5)
        r = rng.randint(*radius)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=rng.choice(colors))


def glow_ellipses(tile: Image.Image, rng: random.Random, count: int, colors, large=False):
    glow = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    for _ in range(count):
        radius = rng.randint(15, 42) if large else rng.randint(6, 18)
        x, y = rng.randint(0, TILE_W), rng.randint(0, TILE_H)
        color = rng.choice(colors)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(13 if large else 7))
    tile.alpha_composite(glow)


def draw_effect(name: str) -> Image.Image:
    tile = Image.new("RGBA", (TILE_W, TILE_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    rng = rng_for(name)

    if name in {"stars", "starPop", "starSprinkle", "glitterField", "fireflies", "stardustTrails"}:
        palette = [WHITE, GOLD, CYAN] if name != "fireflies" else [GOLD, GREEN, (255, 255, 180, 230)]
        random_dots(draw, rng, 35 if name != "starPop" else 18, palette, (1, 3))
        count = 5 if name == "starPop" else 2
        for _ in range(count):
            x, y = rng.randint(18, TILE_W - 18), rng.randint(14, TILE_H - 14)
            size = rng.randint(7, 14)
            draw.polygon(star_points(x, y, size, size * .42), fill=rng.choice(palette))

    elif name == "bubbles":
        for _ in range(16):
            r = rng.randint(5, 17)
            x, y = rng.randint(r, TILE_W - r), rng.randint(r, TILE_H - r)
            color = rng.choice([WHITE, CYAN, PINK])
            draw.ellipse((x-r, y-r, x+r, y+r), outline=color, width=2)
            draw.ellipse((x-r*.45, y-r*.55, x-r*.15, y-r*.25), fill=(255, 255, 255, 185))

    elif name in {"blobs", "orbs", "cosmicDust", "plasmaOrbs"}:
        glow_ellipses(tile, rng, 10 if name == "cosmicDust" else 6,
                      [CYAN, PINK, PURPLE, BLUE], large=name in {"blobs", "plasmaOrbs"})
        draw = ImageDraw.Draw(tile)
        random_dots(draw, rng, 22 if name == "cosmicDust" else 8, [WHITE, CYAN], (1, 2))

    elif name in {"waves", "rippleWaves", "aurora"}:
        colors = [CYAN, PURPLE, PINK, WHITE]
        if name == "rippleWaves":
            for index in range(5):
                box = (48-index*9, 30-index*5, 192+index*9, 106+index*5)
                draw.ellipse(box, outline=colors[index % len(colors)], width=2)
        else:
            for row in range(4):
                points = []
                for x in range(-10, TILE_W + 11, 5):
                    y = 31 + row * 22 + math.sin(x / 23 + row * 1.2) * (9 + row * 2)
                    points.append((x, y))
                draw.line(points, fill=colors[row], width=5 if name == "aurora" else 2)

    elif name in {"geometry", "polygons", "neonGeometryFlow", "crystalPrisms", "diamondPulse"}:
        palette = [CYAN, PINK, PURPLE, GOLD]
        for index in range(7):
            x, y = rng.randint(15, TILE_W - 15), rng.randint(13, TILE_H - 13)
            size = rng.randint(8, 23)
            sides = 4 if name == "diamondPulse" else rng.choice([3, 4, 6])
            angle_offset = math.pi / 4 if name == "diamondPulse" else rng.random() * math.pi
            points = [
                (x + math.cos(angle_offset + i * 2 * math.pi / sides) * size,
                 y + math.sin(angle_offset + i * 2 * math.pi / sides) * size)
                for i in range(sides)
            ]
            draw.polygon(points, outline=palette[index % len(palette)], width=2)

    elif name == "rings":
        for index in range(8):
            r = rng.randint(7, 24)
            x, y = rng.randint(r, TILE_W-r), rng.randint(r, TILE_H-r)
            draw.ellipse((x-r, y-r, x+r, y+r), outline=rng.choice([CYAN, PINK, WHITE]), width=2)

    elif name in {"lines", "cyberLines", "circuitPaths", "lightPillars"}:
        palette = [CYAN, BLUE, PINK, WHITE]
        if name == "lightPillars":
            glow = Image.new("RGBA", tile.size, (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            for index, x in enumerate([35, 82, 130, 184, 218]):
                gd.rectangle((x-6, 10, x+6, 128), fill=(*palette[index % 4][:3], 120))
            tile.alpha_composite(glow.filter(ImageFilter.GaussianBlur(8)))
        else:
            for index in range(9):
                x1, y1 = rng.randint(0, 50), rng.randint(8, TILE_H-8)
                x2, y2 = rng.randint(185, TILE_W), rng.randint(8, TILE_H-8)
                if name == "circuitPaths":
                    mid = rng.randint(70, 170)
                    draw.line([(x1,y1),(mid,y1),(mid,y2),(x2,y2)], fill=palette[index % 4], width=2)
                    draw.ellipse((mid-3,y2-3,mid+3,y2+3), fill=palette[index % 4])
                else:
                    draw.line((x1, y1, x2, y2), fill=palette[index % 4], width=2)

    elif name in {"cometTwinkle", "meteorShower", "electricSparks"}:
        palette = [WHITE, CYAN, GOLD, PINK]
        for index in range(7):
            x = rng.randint(55, TILE_W - 8)
            y = rng.randint(8, TILE_H - 30)
            if name == "electricSparks":
                pts = [(x, y)]
                for step in range(5):
                    pts.append((x - (step+1)*rng.randint(7, 13), y + (step+1)*rng.randint(3, 7) + rng.randint(-8, 8)))
                draw.line(pts, fill=palette[index % 4], width=2)
            else:
                length = rng.randint(24, 58)
                draw.line((x-length, y+length*.55, x, y), fill=palette[index % 4], width=2)
                draw.ellipse((x-3, y-3, x+3, y+3), fill=WHITE)

    elif name == "snowfall":
        for _ in range(30):
            x, y = rng.randint(5, TILE_W-5), rng.randint(4, TILE_H-4)
            r = rng.randint(1, 3)
            draw.line((x-r*2,y,x+r*2,y), fill=WHITE, width=1)
            draw.line((x,y-r*2,x,y+r*2), fill=WHITE, width=1)

    elif name == "confetti":
        palette = [CYAN, PINK, GOLD, GREEN, PURPLE]
        for _ in range(34):
            x, y = rng.randint(4, TILE_W-8), rng.randint(4, TILE_H-10)
            w, h = rng.randint(3, 7), rng.randint(7, 13)
            draw.rectangle((x, y, x+w, y+h), fill=rng.choice(palette))

    elif name == "lanterns":
        for x, y, scale in [(40, 25, 1), (112, 45, .8), (183, 17, 1.1)]:
            w, h = 25*scale, 34*scale
            draw.line((x, 0, x, y), fill=(255,255,255,150), width=1)
            draw.rounded_rectangle((x-w/2,y,x+w/2,y+h), radius=5, fill=(251,113,133,185), outline=GOLD, width=2)
            draw.ellipse((x-5,y+h*.35,x+5,y+h*.7), fill=(255,245,180,225))

    elif name == "matrixDrops":
        for x in range(10, TILE_W, 16):
            start = rng.randint(-25, 60)
            length = rng.randint(35, 85)
            for y in range(start, min(start + length, TILE_H), 9):
                alpha = max(55, 230 - (y-start)*3)
                draw.rectangle((x, y, x+2, y+5), fill=(110, 255, 170, alpha))

    elif name in {"autumnLeaves", "springBreeze", "cherryBlossoms"}:
        palette = [GOLD, RED, (251,146,60,220)] if name == "autumnLeaves" else [PINK, WHITE, (253,164,175,220)]
        for _ in range(22):
            x, y = rng.randint(7, TILE_W-7), rng.randint(6, TILE_H-6)
            s = rng.randint(4, 9)
            if name == "springBreeze":
                draw.arc((x-s*3,y-s,x+s*3,y+s), 190, 350, fill=GREEN, width=2)
            elif name == "cherryBlossoms":
                color = rng.choice(palette)
                for angle in range(0, 360, 72):
                    dx, dy = math.cos(math.radians(angle))*s*.65, math.sin(math.radians(angle))*s*.65
                    draw.ellipse((x+dx-s*.45,y+dy-s*.45,x+dx+s*.45,y+dy+s*.45), fill=color)
                draw.ellipse((x-1,y-1,x+1,y+1), fill=GOLD)
            else:
                draw.polygon([(x,y-s),(x+s,y),(x,y+s),(x-s*.55,y)], fill=rng.choice(palette))

    elif name == "magicRunes":
        for x, y, r in [(55,66,32),(128,45,22),(185,78,28)]:
            draw.ellipse((x-r,y-r,x+r,y+r), outline=PURPLE, width=2)
            draw.polygon(star_points(x,y,r*.72,r*.34,6), outline=CYAN)
            draw.ellipse((x-3,y-3,x+3,y+3), fill=WHITE)

    elif name == "floatingHearts" or name == "roseDay":
        palette = [PINK, RED, WHITE]
        for _ in range(15):
            x, y, s = rng.randint(10,TILE_W-10), rng.randint(8,TILE_H-8), rng.randint(5,11)
            if name == "roseDay" and rng.random() < .45:
                draw.ellipse((x-s,y-s,x+s,y+s), outline=RED, width=2)
                draw.arc((x-s*.65,y-s*.65,x+s*.65,y+s*.65), 20, 310, fill=PINK, width=2)
            else:
                color = rng.choice(palette)
                draw.ellipse((x-s,y-s*.65,x,y+s*.35), fill=color)
                draw.ellipse((x,y-s*.65,x+s,y+s*.35), fill=color)
                draw.polygon([(x-s,y),(x+s,y),(x,y+s*1.35)], fill=color)

    elif name == "summerVacation":
        draw.ellipse((172,17,210,55), fill=GOLD)
        for radius, color in [(62,(103,232,249,130)),(48,(96,165,250,155)),(34,(255,255,255,130))]:
            draw.arc((120-radius, 75-radius*.35, 120+radius, 75+radius*.55), 5, 175, fill=color, width=7)
        draw.polygon([(20,117),(72,57),(124,117)], fill=(255,255,255,150))

    elif name == "chuseokMoon":
        glow_ellipses(tile, rng, 2, [(253,224,71,150)], large=True)
        draw = ImageDraw.Draw(tile)
        draw.ellipse((76,14,166,104), fill=(255,239,155,215), outline=WHITE, width=2)
        draw.arc((18,87,222,142), 188, 352, fill=WHITE, width=2)

    elif name == "chuseokHangawi":
        glow_ellipses(tile, rng, 2, [(252, 211, 77, 140)], large=True)
        draw = ImageDraw.Draw(tile)
        draw.ellipse((88, 12, 158, 82), fill=(255, 243, 191, 225), outline=WHITE, width=2)
        for cx, cy, r in [(60, 52, 22), (168, 40, 17), (110, 100, 27)]:
            draw.ellipse((cx - r * 1.7, cy - r * 0.6, cx + r * 1.7, cy + r * 0.6), fill=(255, 255, 255, 120))
            draw.ellipse((cx - r * 0.8, cy - r, cx + r * 0.5, cy + r * 0.4), fill=(255, 255, 255, 120))
        for x, y, s_ in [(34, 24, 13), (206, 74, 15), (52, 112, 11), (150, 124, 12), (192, 16, 9)]:
            draw.polygon(star_points(x, y, s_, s_ * 0.3, 4), fill=(255, 251, 235, 235))
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=WHITE)
        random_dots(draw, rng, 10, [WHITE, GOLD], (1, 2))

    elif name == "seollalRibbons":
        for index, color in enumerate([RED, GOLD, CYAN, PINK]):
            pts = []
            for x in range(-15, TILE_W+16, 5):
                pts.append((x, 27+index*24+math.sin(x/20+index)*12))
            draw.line(pts, fill=color, width=5)

    elif name in {"christmasMagic", "christmasSeason2"}:
        random_dots(draw, rng, 26, [WHITE, GOLD], (1, 2))
        for x, y, s in [(55,97,39),(124,108,52),(191,100,34)]:
            draw.polygon([(x,y-s),(x-s*.75,y+s*.35),(x-s*.28,y+s*.2),(x-s*.8,y+s),(x+s*.8,y+s),(x+s*.28,y+s*.2),(x+s*.75,y+s*.35)], fill=(52,211,153,195))
            draw.rectangle((x-3,y+s,x+3,y+s+10), fill=(180,120,70,210))
        draw.polygon(star_points(124,44,10,4), fill=GOLD)

    elif name == "peperoDay":
        for index, x in enumerate([58,91,124,157,190]):
            tilt = [-6,4,-3,5,-5][index]
            draw.line((x,25,x+tilt,112), fill=(255,232,190,230), width=9)
            draw.line((x,25,x+tilt*.55,72), fill=(120,72,45,235), width=9)
            draw.ellipse((x-5,20,x+5,29), fill=(120,72,45,235))

    else:
        random_dots(draw, rng, 26, [WHITE, CYAN, PINK, GOLD], (1, 4))

    return tile


def main() -> None:
    sprite = Image.new("RGBA", (TILE_W * COLS, TILE_H * ROWS), (0, 0, 0, 0))
    for index, effect in enumerate(EFFECTS):
        tile = draw_effect(effect)
        x = (index % COLS) * TILE_W
        y = (index // COLS) * TILE_H
        sprite.alpha_composite(tile, (x, y))

    output = Path(__file__).resolve().parents[1] / "static" / "images" / "theme-effects" / "effect-sprite.webp"
    output.parent.mkdir(parents=True, exist_ok=True)
    sprite.save(output, "WEBP", lossless=True, method=6)
    print(f"generated {output} ({sprite.width}x{sprite.height}, {len(EFFECTS)} effects)")


if __name__ == "__main__":
    main()
