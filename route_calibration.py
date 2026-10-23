"""Learn a Free Market entrance/room route from one manual 1→20 walk.

No game input is generated here.  It observes only the derived local position
state created by ``position_state_collector`` and writes portal coordinates
immediately before confirmed map transitions.
"""
import json
import time
from pathlib import Path

from position_state_collector import POSITION_STATE


class RouteCalibration:
    def __init__(self, output, position_path=POSITION_STATE, rooms=20):
        self.output = Path(output)
        self.position_path = Path(position_path)
        self.rooms = rooms
        self.profile = dict(version=1, state='calibrating', entrance_map_id=None,
                            rooms={}, transitions=[], updated_at=time.time())
        self.last_valid = None
        self.previous_valid = None
        self.last_map = None
        self.room_number = 1
        self.next_action = 'enter_room'
        self.seen = set()

    def _read(self):
        try:
            return json.loads(self.position_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def _save(self):
        self.profile['updated_at'] = time.time()
        temporary = self.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.profile, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.output)

    def _direction(self):
        if not self.previous_valid or not self.last_valid:
            return 'right'
        return 'right' if self.last_valid['x'] >= self.previous_valid['x'] else 'left'

    def _transition(self, destination_map, observed_at):
        source = self.last_valid
        if not source or source.get('map_id') == destination_map:
            return None
        key = (source.get('map_id'), source.get('sequence'), destination_map)
        if key in self.seen:
            return None
        self.seen.add(key)
        portal = dict(x=source['x'], y=source['y'], foothold=source['foothold'],
                      direction=self._direction())
        event = dict(at=observed_at, from_map_id=source['map_id'], to_map_id=destination_map,
                     portal=portal, step=self.next_action, room=self.room_number)
        self.profile['transitions'].append(event)
        if self.next_action == 'enter_room':
            if self.profile['entrance_map_id'] is None:
                self.profile['entrance_map_id'] = source['map_id']
            elif source['map_id'] != self.profile['entrance_map_id']:
                event['rejected'] = 'expected_entrance_map'
                self._save()
                return event
            self.profile['rooms'][str(self.room_number)] = dict(map_id=destination_map,
                                                                  entry=portal)
            self.next_action = 'exit_room'
        else:
            room = self.profile['rooms'].get(str(self.room_number))
            if not room or source['map_id'] != room['map_id']:
                event['rejected'] = 'expected_current_room_map'
                self._save()
                return event
            room['exit'] = portal
            self.room_number += 1
            self.next_action = 'enter_room'
            if self.room_number > self.rooms:
                self.profile['state'] = 'complete'
        self._save()
        return event

    def tick(self):
        state = self._read()
        if not state:
            return None
        map_id = state.get('map_id')
        if not isinstance(map_id, int):
            return None
        event = None
        if self.last_map is not None and map_id != self.last_map:
            event = self._transition(map_id, time.time())
        self.last_map = map_id
        if state.get('valid') and all(isinstance(state.get(key), int)
                                      for key in ('x', 'y', 'foothold', 'sequence')):
            if self.last_valid is None or state['sequence'] != self.last_valid['sequence']:
                self.previous_valid, self.last_valid = self.last_valid, state
        return event


def run(folder, stop_event, callback=None, rooms=20):
    folder = Path(folder)
    profile_path = folder / 'free_market_route.json'
    calibration = RouteCalibration(profile_path, rooms=rooms)
    if callback:
        callback('route_calibration_waiting_for_entrance')
    while not stop_event.is_set() and calibration.profile['state'] != 'complete':
        event = calibration.tick()
        if event and callback:
            callback(('route_calibration_transition', event))
        time.sleep(.08)
    calibration._save()
    return dict(profile=profile_path, state=calibration.profile['state'],
                rooms=len(calibration.profile['rooms']), next_room=calibration.room_number)
