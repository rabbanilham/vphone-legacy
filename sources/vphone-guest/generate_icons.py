#!/usr/bin/env python3
"""Generate non-copyrighted simple app icons for the Hello iOS 7 guest app.

Produces:
- 120x120 PNG (Retina iPhone 5s / iOS 7 @2x icon)
- 57x57 PNG (Standard legacy / fallback icon)
"""
import argparse
from pathlib import Path
from PIL import Image, ImageDraw


def create_icon(size: int) -> Image.Image:
    # 4x supersampling for high-quality antialiased geometric drawing
    scale = 4
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Background rounded rectangle (iOS 7 springboard clips to squircle, but rectangular or rounded PNG is standard)
    corner_radius = int(canvas_size * 0.22)
    bg_color = (24, 30, 48, 255)  # Dark slate navy
    draw.rounded_rectangle(
        [(0, 0), (canvas_size - 1, canvas_size - 1)],
        radius=corner_radius,
        fill=bg_color,
    )

    # Accent decorative circular aura
    center_x = canvas_size // 2
    center_y = canvas_size // 2
    aura_radius = int(canvas_size * 0.38)
    draw.ellipse(
        [
            (center_x - aura_radius, center_y - aura_radius),
            (center_x + aura_radius, center_y + aura_radius),
        ],
        fill=(0, 122, 255, 60),  # Subtle iOS blue tint
    )

    # Clean geometric 'V' (representing vphone)
    stroke_width = max(2, int(canvas_size * 0.08))
    top_y = int(canvas_size * 0.28)
    bottom_y = int(canvas_size * 0.72)
    left_x = int(canvas_size * 0.28)
    right_x = int(canvas_size * 0.72)
    mid_x = canvas_size // 2

    # Draw 'V' line segments
    draw.line(
        [(left_x, top_y), (mid_x, bottom_y)],
        fill=(255, 255, 255, 255),
        width=stroke_width,
    )
    draw.line(
        [(mid_x, bottom_y), (right_x, top_y)],
        fill=(0, 190, 255, 255),  # Cyan accent stroke
        width=stroke_width,
    )

    # Small top caps for smooth stroke ends
    cap_radius = stroke_width // 2
    draw.ellipse(
        [(left_x - cap_radius, top_y - cap_radius), (left_x + cap_radius, top_y + cap_radius)],
        fill=(255, 255, 255, 255),
    )
    draw.ellipse(
        [(right_x - cap_radius, top_y - cap_radius), (right_x + cap_radius, top_y + cap_radius)],
        fill=(0, 190, 255, 255),
    )
    draw.ellipse(
        [(mid_x - cap_radius, bottom_y - cap_radius), (mid_x + cap_radius, bottom_y + cap_radius)],
        fill=(255, 255, 255, 255),
    )

    # Downsample with Lanczos filter for crisp antialiasing
    resample_filter = getattr(Image, "Resampling", Image).LANCZOS
    icon = image.resize((size, size), resample=resample_filter)
    return icon


def generate_all_icons(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    icon_120 = create_icon(120)
    icon_120.save(output_dir / "AppIcon60x60@2x.png", format="PNG")
    icon_120.save(output_dir / "Icon-60@2x.png", format="PNG")

    icon_57 = create_icon(57)
    icon_57.save(output_dir / "AppIcon57x57.png", format="PNG")
    icon_57.save(output_dir / "Icon.png", format="PNG")

    print(f"Generated icons in {output_dir}:")
    print(f"  - AppIcon60x60@2x.png (120x120)")
    print(f"  - Icon-60@2x.png      (120x120)")
    print(f"  - AppIcon57x57.png    (57x57)")
    print(f"  - Icon.png            (57x57)")


def main():
    parser = argparse.ArgumentParser(description="Generate Hello app icons")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path(__file__).parent / "assets",
        help="Directory to save generated PNG icons",
    )
    args = parser.parse_args()
    generate_all_icons(args.output)


if __name__ == "__main__":
    main()
