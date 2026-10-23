"""Read the currently loaded map's local WZ structure without caching routes."""
import os
from functools import lru_cache
from pathlib import Path

from market_map_resources import extract_map

DEFAULT_ROOT = r'C:\Nexon\Library\maplestorycw\appdata\Data\Map\Map'


def _map_file(map_id, root):
    digits = f'{map_id:09d}'
    group = digits[0]
    return root / f'Map{group}' / f'Map{group}_000.wz'


def _platform_count(footholds):
    """Count joined horizontal walkable runs; exclude vertical collision lines."""
    lines = sorted((item['y1'], item['x1'], item['x2']) for item in footholds
                   if item.get('y1') == item.get('y2') and item.get('x1', 0) < item.get('x2', 0))
    count = 0
    previous = None
    for y, left, right in lines:
        if previous is None or y != previous[0] or left > previous[2]:
            count += 1
            previous = [y, left, right]
        else:
            previous[2] = max(previous[2], right)
    return count


@lru_cache(maxsize=32)
def describe(map_id, root_text=None):
    """Return only navigation metadata for one map ID.

    This reads client resources on demand.  The result is kept only in process
    memory and is intentionally not used as a saved route profile.
    """
    if not isinstance(map_id, int) or not 1 <= map_id <= 999999999:
        return dict(available=False, reason='invalid_map_id')
    root = Path(root_text or os.environ.get('MAPLESTORY_MAP_WZ_ROOT', DEFAULT_ROOT))
    source = _map_file(map_id, root)
    if not source.exists():
        return dict(available=False, reason='map_resource_not_found', map_file=str(source))
    try:
        resource = extract_map(source, map_id)
    except (OSError, ValueError, KeyError) as error:
        return dict(available=False, reason='map_resource_parse_failed', error=str(error), map_file=str(source))
    info = resource.get('info', {})
    portals = []
    for portal in resource.get('portals', []):
        portals.append(dict(name=portal.get('pn'), type=portal.get('pt'),
                            x=portal.get('x'), y=portal.get('y'),
                            target_map=portal.get('tm'), target_name=portal.get('tn')))
    return dict(available=True, map_id=map_id, return_map=info.get('returnMap'),
                bounds=dict(left=info.get('VRLeft'), top=info.get('VRTop'),
                            right=info.get('VRRight'), bottom=info.get('VRBottom')),
                platforms=_platform_count(resource.get('footholds', [])),
                footholds=len(resource.get('footholds', [])),
                ladders=[dict(x=item.get('x'), top=item.get('y1'), bottom=item.get('y2'))
                         for item in resource.get('ladders', [])],
                portals=portals)
