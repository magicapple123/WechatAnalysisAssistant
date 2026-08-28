"""Generate the checked-in Windows desktop icon from simple vector shapes."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "desktop" / "assets"
CANVAS_SIZE = 1024


def _vertical_gradient(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pixels = image.load()
    start = (7, 139, 94)
    end = (14, 92, 72)
    for y in range(size):
        amount = y / max(1, size - 1)
        colour = tuple(round(a + (b - a) * amount) for a, b in zip(start, end))
        for x in range(size):
            pixels[x, y] = (*colour, 255)
    return image


def build_icon() -> Image.Image:
    image = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))

    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((64, 78, 960, 974), radius=218, fill=(0, 35, 27, 105))
    shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    image.alpha_composite(shadow)

    tile_mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(tile_mask).rounded_rectangle((56, 56, 968, 968), radius=220, fill=255)
    gradient = _vertical_gradient(CANVAS_SIZE)
    image.alpha_composite(Image.composite(gradient, Image.new("RGBA", image.size), tile_mask))

    highlight = Image.new("RGBA", image.size, (0, 0, 0, 0))
    highlight_draw = ImageDraw.Draw(highlight)
    highlight_draw.ellipse((115, -230, 920, 475), fill=(255, 255, 255, 23))
    image.alpha_composite(Image.composite(highlight, Image.new("RGBA", image.size), tile_mask))

    bubble_shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    bubble_shadow_draw = ImageDraw.Draw(bubble_shadow)
    bubble_shadow_draw.rounded_rectangle((204, 246, 820, 714), radius=150, fill=(0, 45, 31, 75))
    bubble_shadow_draw.polygon(((330, 672), (252, 832), (500, 706)), fill=(0, 45, 31, 75))
    bubble_shadow = bubble_shadow.filter(ImageFilter.GaussianBlur(18))
    image.alpha_composite(bubble_shadow)

    draw = ImageDraw.Draw(image)
    white = (245, 252, 249, 255)
    draw.rounded_rectangle((200, 224, 816, 692), radius=150, fill=white)
    draw.polygon(((326, 650), (248, 812), (500, 686)), fill=white)

    bar_colour = (8, 116, 82, 255)
    bars = (
        (342, 474, 420, 592),
        (473, 390, 551, 592),
        (604, 310, 682, 592),
    )
    for bounds in bars:
        draw.rounded_rectangle(bounds, radius=39, fill=bar_colour)

    return image


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    icon = build_icon()
    icon.save(ASSET_DIR / "icon.png", format="PNG", optimize=True)
    icon.save(
        ASSET_DIR / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"Desktop icons written to {ASSET_DIR}")


if __name__ == "__main__":
    main()
