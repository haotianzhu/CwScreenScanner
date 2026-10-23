"""One-run Free Market map-load probe.

The probe reads only the derived state emitted by ``position_state_collector``.
It does not save a route or generate game input.  A completed probe contains:
  * entrance-map start and exit coordinates,
  * room-map start and exit coordinates,
  * map-load epochs and any safely decoded map IDs.
"""
import json
import time
from pathlib import Path

from position_state_collector import POSITION_STATE
from map_runtime_structure import describe


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _compact(segment):
    return {key: segment.get(key) for key in (
        'map_epoch', 'map_id', 'parser', 'entered_at', 'exited_at',
        'start_position', 'exit_position', 'last_position'
    ) if segment.get(key) is not None}


def run(stop_event, callback=None, position_path=POSITION_STATE):
    """Wait for a CH reconnect, one room entry, then one room exit.

    The reconnect supplies a fresh session key to the passive decoder.  The
    three observed map loads are: entry map after the CH change, room entry,
    then return to the entrance map.  The outcome stays in memory and the run
    log; no calibration/profile file is created.
    """
    status_path = Path(position_path).with_name('player_position_status.json')
    if callback:
        callback('map_probe_waiting_for_channel_change')
    last_signature = None
    while not stop_event.is_set():
        status = _read(status_path)
        segments = status.get('map_segments') or []
        signature = tuple((item.get('map_epoch'), bool(item.get('start_position')),
                           bool(item.get('exit_position'))) for item in segments)
        if signature != last_signature:
            last_signature = signature
            compacted = [_compact(item) for item in segments]
            latest = compacted[-1] if compacted else {}
            if isinstance(latest.get('map_id'), int):
                latest['structure'] = describe(latest['map_id'])
            print(json.dumps(dict(event='map_probe_update', segments=compacted), ensure_ascii=False), flush=True)
            if callback:
                callback(('map_probe_update', compacted))
        # Epoch 1 is the entrance map loaded by the required manual CH change;
        # epoch 2 is the room; epoch 3 closes the room and proves its exit.
        if len(segments) >= 3:
            room = segments[-2]
            if room.get('start_position') and room.get('exit_position'):
                outcome = dict(reason='room_round_trip_observed', room=_compact(room),
                               entrance_before=_compact(segments[-3]),
                               entrance_after=_compact(segments[-1]))
                print(json.dumps(dict(event='map_probe_complete', **outcome), ensure_ascii=False), flush=True)
                return outcome
        time.sleep(.10)
    return dict(reason='cancelled', diagnostics=status.get('map_segments', []))
