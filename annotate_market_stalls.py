"""Mark detected Free Market stalls in a screenshot.

Example:
    python annotate_market_stalls.py screenshot.png
    python annotate_market_stalls.py screenshot.png --output detected.png
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from market_browser_bot import Vision


def output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.stalls.png")


def main() -> None:
    parser = argparse.ArgumentParser(description='Draw red boxes around detected Free Market stalls.')
    parser.add_argument('image', type=Path, help='Input screenshot (PNG, JPG, or BMP)')
    parser.add_argument('--output', '-o', type=Path, help='Annotated PNG output path')
    args = parser.parse_args()

    if not args.image.is_file():
        parser.error(f'Input image does not exist: {args.image}')

    image = Image.open(args.image).convert('RGB')
    result = Vision().inspect(image)
    marked = image.copy()
    draw = ImageDraw.Draw(marked)

    for index, target in enumerate(result['targets'], start=1):
        left, top, right, bottom = target['box']
        draw.rectangle((left, top, right, bottom), outline='red', width=3)
        draw.text((left + 4, max(0, top - 18)), f"{index} {target['kind']}", fill='red')

    destination = args.output or output_path(args.image)
    destination.parent.mkdir(parents=True, exist_ok=True)
    marked.save(destination)
    print(f"Detected {len(result['targets'])} stalls")
    print(f"Annotated image: {destination}")


if __name__ == '__main__':
    main()
