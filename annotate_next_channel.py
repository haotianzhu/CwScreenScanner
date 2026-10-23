"""Read the selected channel in a MapleStory channel dialog and mark the next one.

This is an offline verifier. It never sends input to the game.
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw
from market_channel_vision import CHANNELS, button_box, find_selected_channel


def annotate(source: Path, output: Path):
    image = Image.open(source).convert('RGB')
    current, _ = find_selected_channel(image)
    target = current % CHANNELS + 1
    box = button_box(target, image.width, image.height)
    drawing = ImageDraw.Draw(image)
    drawing.rectangle(box, outline='red', width=4)
    drawing.text((box[0], max(0, box[1] - 18)), f'NEXT: CH.{target}', fill='red', stroke_width=1, stroke_fill='white')
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    return current, target, box


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = args.output or args.image.with_name(args.image.stem + '.next-channel.png')
    current, target, box = annotate(args.image, output)
    print(f'Current channel: CH.{current}; next channel: CH.{target}; box: {box}')
    print(f'Annotated image: {output}')


if __name__ == '__main__':
    main()
