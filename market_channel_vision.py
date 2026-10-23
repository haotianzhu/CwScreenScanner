"""Visual detection for the MapleStory channel menu at a 1920x1080 client size."""
import cv2
import numpy as np
from PIL import Image

COLUMNS = 8
CHANNELS = 40
# Proportions measured from the native channel selector. A small window border
# in a desktop screenshot does not change the client-relative positions.
GRID_LEFT = 0.299
GRID_TOP = 0.449
GRID_PITCH_X = 0.0514
GRID_PITCH_Y = 0.0285
GRID_BUTTON_W = 0.0494
GRID_BUTTON_H = 0.0252


def _rgb(image):
    return np.asarray(image.convert('RGB')) if isinstance(image, Image.Image) else image


def _blue_components(rgb):
    red, green, blue = (rgb[:, :, index] for index in range(3))
    mask = ((blue > 155) & (green > 115) & (red < 100) &
            ((blue.astype(np.int16) - red.astype(np.int16)) > 75)).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return [tuple(map(int, row)) for row in stats[1:count]]


def _blue_button(rgb, x_range, y_range, width_range, height_range):
    height, width = rgb.shape[:2]
    candidates = []
    for x, y, box_width, box_height, area in _blue_components(rgb):
        if not (x_range[0] * width <= x <= x_range[1] * width and
                y_range[0] * height <= y <= y_range[1] * height and
                width_range[0] * width <= box_width <= width_range[1] * width and
                height_range[0] * height <= box_height <= height_range[1] * height):
            continue
        if area < box_width * box_height * .35:
            continue
        candidates.append((x, y, box_width, box_height, area))
    if len(candidates) != 1:
        raise RuntimeError(f'Expected one blue button, found {len(candidates)}')
    x, y, box_width, box_height, _ = candidates[0]
    return x, y, x + box_width, y + box_height


def find_game_menu_change(image):
    """Return the visible CHANGE CHANNEL button in the ESC game menu."""
    return _blue_button(_rgb(image), (.66, .76), (.72, .76), (.045, .09), (.016, .045))


def find_channel_confirm(image):
    """Return the CHANGE CHANNEL confirm button in the channel selector."""
    return _blue_button(_rgb(image), (.53, .69), (.56, .69), (.06, .11), (.016, .045))


def find_selected_channel(image):
    """Locate the blue selected channel button and return its 1-based number."""
    rgb = _rgb(image)
    candidates = _selected_button_candidates(rgb)
    if len(candidates) != 1:
        raise RuntimeError(f'Expected one selected channel button, found {len(candidates)}')
    x, y, box_width, box_height, _, row, column = candidates[0]
    return row * COLUMNS + column + 1, (x, y, x + box_width, y + box_height)


def _selected_button_candidates(rgb):
    """Blue components anchored to one of the 40 physical grid cells only."""
    height, width = rgb.shape[:2]
    candidates = []
    first_center_x = (GRID_LEFT + GRID_BUTTON_W / 2) * width
    first_center_y = (GRID_TOP + GRID_BUTTON_H / 2) * height
    for x, y, box_width, box_height, area in _blue_components(rgb):
        if not (.032 * width <= box_width <= .070 * width and
                .014 * height <= box_height <= .045 * height and
                area >= box_width * box_height * .35):
            continue
        center_x, center_y = x + box_width / 2, y + box_height / 2
        column = round((center_x - first_center_x) / (GRID_PITCH_X * width))
        row = round((center_y - first_center_y) / (GRID_PITCH_Y * height))
        if not (0 <= row < 5 and 0 <= column < COLUMNS):
            continue
        expected_x = first_center_x + column * GRID_PITCH_X * width
        expected_y = first_center_y + row * GRID_PITCH_Y * height
        # Scene effects cannot pass this test: the component must be centred
        # on an actual channel cell, not merely have a blue button-like size.
        if (abs(center_x - expected_x) > GRID_BUTTON_W * width * .35 or
                abs(center_y - expected_y) > GRID_BUTTON_H * height * .45):
            continue
        candidates.append((x, y, box_width, box_height, area, row, column))
    return candidates


def channel_selector_visible(image):
    """Recognize the selector even when the selected-blue-button count is noisy.

    Recovery only needs to know whether Escape should close the selector.  It
    must therefore tolerate the transient duplicate blue component that makes
    ``find_selected_channel`` intentionally reject an ambiguous selection.
    """
    return bool(_selected_button_candidates(_rgb(image)))


def button_box(channel, width, height):
    if not 1 <= channel <= CHANNELS:
        raise ValueError('Channel must be 1..40')
    row, column = divmod(channel - 1, COLUMNS)
    left = round((GRID_LEFT + column * GRID_PITCH_X) * width)
    top = round((GRID_TOP + row * GRID_PITCH_Y) * height)
    return left, top, round(left + GRID_BUTTON_W * width), round(top + GRID_BUTTON_H * height)
