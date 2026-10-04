from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


FONT_PATH = Path(r"C:\Windows\Fonts\msjhbd.ttc")


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int) -> ImageFont.FreeTypeFont:
    for size in range(start_size, 7, -1):
        font = ImageFont.truetype(str(FONT_PATH), size)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        if box[2] - box[0] <= max_width:
            return font
    return ImageFont.truetype(str(FONT_PATH), 8)


def vertical_panel(source: Path, target: Path) -> None:
    image = Image.open(source).convert("RGBA")
    width, height = image.size
    original_region = (round(width * 0.25), round(height * 0.025), round(width * 0.91), round(height * 0.90))
    blurred = image.crop(original_region).filter(ImageFilter.GaussianBlur(radius=max(6, width // 38)))
    image.paste(blurred, original_region[:2])
    text = "那名女性站在公寓前。她顯得有些百無聊賴，偶爾稍微變換站姿。她的模樣讓人感受到歲月流逝。"
    column_count = 3
    chunk = (len(text) + column_count - 1) // column_count
    columns = [text[index:index + chunk] for index in range(0, len(text), chunk)]
    font_size = max(16, round(width * 0.082))
    font = ImageFont.truetype(str(FONT_PATH), font_size)
    line_gap = font_size + max(3, font_size // 5)
    column_gap = font_size + max(9, font_size // 3)
    padding_x, padding_y = max(7, width // 45), max(8, height // 90)
    text_width = column_gap * (len(columns) - 1) + font_size
    text_height = max(len(column) for column in columns) * line_gap
    center_x = round(width * 0.60)
    top = round(height * 0.035)
    left = center_x - text_width // 2 - padding_x
    right = center_x + text_width // 2 + padding_x
    bottom = top + text_height + padding_y * 2
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    panel = ImageDraw.Draw(overlay)
    panel.rounded_rectangle((left, top, right, bottom), radius=max(7, width // 34), fill=(17, 24, 20, 232))
    image = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(image)
    x = right - padding_x - font_size // 2
    y_start = top + padding_y
    for text in columns:
        y = y_start
        for character in text:
            draw.text((x, y), character, font=font, anchor="ma", fill="white", stroke_width=1, stroke_fill=(0, 0, 0, 255))
            y += line_gap
        x -= column_gap
    target.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(target, quality=95)


def sfx_overlay(source: Path, target: Path, *, preserve_original: bool) -> None:
    image = Image.open(source).convert("RGBA")
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if preserve_original:
        box = (round(width * 0.55), round(height * 0.68), round(width * 0.96), round(height * 0.97))
        alpha = 205
    else:
        erase_box = (round(width * 0.24), round(height * 0.16), round(width * 0.75), round(height * 0.80))
        blurred = image.crop(erase_box).filter(ImageFilter.GaussianBlur(radius=max(4, width // 22)))
        image.paste(blurred, erase_box[:2])
        label = "啪！"
        probe_font = ImageFont.truetype(str(FONT_PATH), max(18, width // 5))
        probe = draw.textbbox((0, 0), label, font=probe_font, stroke_width=1)
        text_width, text_height = probe[2] - probe[0], probe[3] - probe[1]
        padding = max(4, width // 30)
        center_x, center_y = round(width * 0.49), round(height * 0.48)
        box = (center_x - text_width // 2 - padding, center_y - text_height // 2 - padding,
               center_x + text_width // 2 + padding, center_y + text_height // 2 + padding)
        alpha = 232
    draw.rounded_rectangle(box, radius=max(5, width // 18), fill=(18, 20, 20, alpha), outline=(255, 255, 255, 225), width=max(1, width // 80))
    label = "啪！"
    font = fit_font(draw, label, box[2] - box[0] - 8, max(18, width // 5))
    draw.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label, font=font, anchor="mm", fill="white", stroke_width=1, stroke_fill="black")
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(image, overlay).convert("RGB").save(target, quality=95)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["vertical", "sfx-cover", "sfx-note"])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    if args.mode == "vertical":
        vertical_panel(args.source, args.target)
    else:
        sfx_overlay(args.source, args.target, preserve_original=args.mode == "sfx-note")


if __name__ == "__main__":
    main()
