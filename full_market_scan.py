"""Resource-driven complete Free Market scan: rooms 1 through 20.

The runner reads the installed Map.wz data on demand.  It keeps no route
profile: every portal coordinate and every entrance-level transition comes
from the map currently shipped with the game client.
"""
import json
import time
from pathlib import Path

from free_market_route import Motion, PositionFeed, RouteError
from map_runtime_structure import describe
from market_channel_cycle import ChannelSwitcher, establish_capture_connection, run_room_channels
from market_keyboard import send_key
from position_state_collector import POSITION_STATE

ENTRANCE = 80002000
ROOM_FIRST = 80002001
ROOM_LAST = 80002020
# The entrance has four floors.  These portal names are verified against the
# local WZ data at run time before they are ever used.
LEVEL_Y = (3, -237, -477, -717)
UP_PORTALS = ('up00', 'up01', 'up02')
DOWN_PORTALS = (None, 'dn02', 'dn01', 'dn00')
ROUTE_RETRY_ATTEMPTS = 3


def _log(path, **event):
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(at=time.time(), **event), ensure_ascii=False) + '\n')


def _wait_for(feed, map_id, pause, check, timeout=20):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        check()
        try:
            return feed.read(map_id, max_age=25)
        except RouteError as error:
            last_error = error
            pause(.10)
    raise RouteError(f'Expected map {map_id} did not supply a usable position: {last_error}')


def _wait_for_supported_map(feed, pause, check, timeout=20):
    """Return a fresh position from the entrance or any supported room."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        check()
        try:
            state = feed.read(max_age=25)
            if state['map_id'] == ENTRANCE or ROOM_FIRST <= state['map_id'] <= ROOM_LAST:
                return state
            last_error = RouteError(f'Unsupported map {state["map_id"]}')
        except RouteError as error:
            last_error = error
        pause(.10)
    raise RouteError(f'Current Free Market map could not be identified: {last_error}')


def _map(map_id):
    data = describe(map_id)
    if not data.get('available'):
        raise RouteError(f'Map {map_id} resource unavailable: {data.get("reason")}')
    return data


def _portal(data, *, name=None, target_map=None):
    matches = [item for item in data['portals']
               if (name is None or item.get('name') == name)
               and (target_map is None or item.get('target_map') == target_map)]
    if len(matches) != 1:
        label = f'name={name!r}, target={target_map!r}'
        raise RouteError(f'Expected exactly one portal ({label}); found {len(matches)}')
    portal = matches[0]
    if not all(isinstance(portal.get(key), int) for key in ('x', 'y', 'target_map')):
        raise RouteError(f'Portal {portal.get("name")} has incomplete coordinates')
    return portal


def _direction(state, portal):
    return 'right' if state['x'] < portal['x'] else 'left'


def run(folder, hwnd, start_event, stop_event, status_callback=None):
    """Scan the current room/channel onward through all 20 Free Market rooms."""
    folder = Path(folder)
    events = folder / 'full_scan.jsonl'
    feed = PositionFeed(POSITION_STATE)
    switcher = ChannelSwitcher(hwnd, stop_event)

    def status(value):
        if status_callback:
            status_callback(value)

    def pulse(keys, seconds):
        for key in keys:
            switcher.check()
            send_key(key, True)
        try:
            switcher.pause(seconds)
        finally:
            for key in reversed(keys):
                send_key(key, False)

    def motion_log(**event):
        _log(events, **event)

    def retry_route_step(step, action):
        """Recover a transient route failure in place before ending a scan."""
        failures = []
        for attempt in range(1, ROUTE_RETRY_ATTEMPTS + 1):
            try:
                return action()
            except (RouteError, RuntimeError) as error:
                if stop_event.is_set() or 'stopped with f9' in str(error).lower() or 'stopped from scanner' in str(error).lower():
                    raise
                failures.append(str(error))
                _log(events, result='route_step_retry', step=step, attempt=attempt,
                     max_attempts=ROUTE_RETRY_ATTEMPTS, reason=str(error))
                status(f'full_retrying_route_{step}_attempt_{attempt}')
                try:
                    switcher.ensure_store_closed()
                except RuntimeError as recovery_error:
                    if stop_event.is_set() or 'stopped with f9' in str(recovery_error).lower() or 'stopped from scanner' in str(recovery_error).lower():
                        raise
                    _log(events, result='route_step_recovery_failed', step=step,
                         attempt=attempt, reason=str(recovery_error))
                if attempt < ROUTE_RETRY_ATTEMPTS:
                    switcher.pause(.35 * attempt)
        raise RouteError(
            f'{step} failed after {ROUTE_RETRY_ATTEMPTS} retries: {failures[-1] if failures else "unknown error"}'
        )

    motion = Motion(feed, pulse, switcher.pause, switcher.check, motion_log)
    entrance_map = _map(ENTRANCE)

    def enter(portal, expected_map, next_map):
        state = _wait_for(feed, expected_map, switcher.pause, switcher.check)
        return motion.enter_portal(dict(portal, direction=_direction(state, portal)),
                                   expected_map, next_map)

    def entrance_level(state):
        distance, level = min((abs(state['y'] - y), i) for i, y in enumerate(LEVEL_Y))
        if distance > 70:
            raise RouteError(f'Entrance position Y={state["y"]} is not on a known level')
        return level

    def move_entrance_level(target_level):
        state = _wait_for(feed, ENTRANCE, switcher.pause, switcher.check)
        level = entrance_level(state)
        while level < target_level:
            portal = _portal(entrance_map, name=UP_PORTALS[level], target_map=ENTRANCE)
            status(f'full_entrance_level_up_{level + 1}')
            state = enter(portal, ENTRANCE, ENTRANCE)
            level += 1
        while level > target_level:
            portal = _portal(entrance_map, name=DOWN_PORTALS[level], target_map=ENTRANCE)
            status(f'full_entrance_level_down_{level - 1}')
            state = enter(portal, ENTRANCE, ENTRANCE)
            level -= 1
        return state

    def enter_room(room):
        map_id = ROOM_FIRST + room - 1
        portal = _portal(entrance_map, target_map=map_id)
        target_level = min(range(len(LEVEL_Y)), key=lambda i: abs(portal['y'] - LEVEL_Y[i]))
        move_entrance_level(target_level)
        status(f'full_entering_room_{room}')
        arrived = enter(portal, ENTRANCE, map_id)
        _log(events, result='entered_room', room=room, map_id=map_id,
             position=[arrived['x'], arrived['y']], entrance_portal=portal['name'])
        return arrived

    def exit_room(room):
        map_id = ROOM_FIRST + room - 1
        data = _map(map_id)
        portal = _portal(data, target_map=ENTRANCE)
        switcher.ensure_store_closed()
        state = _wait_for(feed, map_id, switcher.pause, switcher.check)
        # Room spawns may vary after a channel change.  Descend through the
        # room before making the final horizontal approach to out00.
        if state['y'] < portal['y'] - 45:
            status(f'full_resetting_room_{room}_exit_level')
            state = motion.drop_to_y(portal['y'], map_id)
        status(f'full_exiting_room_{room}')
        returned = enter(portal, map_id, ENTRANCE)
        _log(events, result='exited_room', room=room, map_id=map_id,
             position=[returned['x'], returned['y']], exit_portal=portal['name'])
        return returned

    rooms = []
    total_visits = 0
    completed_cycles = 0
    next_room = 1
    room_ready = False
    try:
        status('full_preparing_capture')
        # A successful visible channel change establishes packet decoding. The
        # new map packet then tells us whether Start was pressed at the FM
        # entrance or inside any one of the twenty rooms.
        starting_channel = establish_capture_connection(hwnd, stop_event, status_callback=status_callback)
        initial = _wait_for_supported_map(feed, switcher.pause, switcher.check)
        if initial['map_id'] == ENTRANCE:
            next_room = 1
        else:
            next_room = initial['map_id'] - ROOM_FIRST + 1
            room_ready = True
        _log(events, result='starting_map_detected', map_id=initial['map_id'],
             room=next_room if room_ready else None, channel=starting_channel,
             position=[initial['x'], initial['y']])
        status(f'full_starting_room_{next_room}' if room_ready else 'full_starting_entrance')
        while not stop_event.is_set():
            cycle = completed_cycles + 1
            _log(events, result='cycle_started', cycle=cycle,
                 starting_room=next_room, starting_channel=starting_channel)
            status(f'full_cycle_started_{cycle}')
            cycle_start_room = next_room
            for room in range(cycle_start_room, 21):
                if stop_event.is_set():
                    return dict(reason='cancelled', rooms=rooms, visited=total_visits,
                                completed_cycles=completed_cycles)
                # Resume directly in a detected room, or reuse room 1 after a
                # completed cycle; every other room is entered from FM.
                if not (room == next_room and room_ready):
                    retry_route_step(f'enter_room_{room}', lambda room=room: enter_room(room))
                room_ready = False

                def room_status(value, prior_visits=total_visits):
                    if isinstance(value, tuple) and value[0] == 'progress':
                        progress = dict(value[1])
                        progress['visited'] = prior_visits + progress.get('visited', 0)
                        progress['cycle'] = cycle
                        status(('progress', progress))
                    else:
                        status(value)

                outcome = run_room_channels(folder, hwnd, start_event, stop_event,
                                            status_callback=room_status, room=room,
                                            initial_channel=starting_channel)
                rooms.append(dict(cycle=cycle, **outcome))
                total_visits += outcome.get('visited', 0)
                _log(events, result='room_scan_finished', cycle=cycle, room=room, outcome=outcome)
                if outcome.get('reason') != 'finished_through_ch_40_returned_exit_channel':
                    return dict(reason='room_scan_incomplete', room=room, cycle=cycle,
                                rooms=rooms, visited=total_visits, outcome=outcome,
                                completed_cycles=completed_cycles)
                # Portals retain the confirmed exit channel.  Carry it into
                # the next room, including the next full loop if CH.1 was not
                # available after room 20.
                starting_channel = outcome['return_channel']
                retry_route_step(f'exit_room_{room}', lambda room=room: exit_room(room))
            retry_route_step('enter_room_1', lambda: enter_room(1))
            room_ready = True
            next_room = 1
            completed_cycles += 1
            _log(events, result='cycle_complete_room_1', cycle=completed_cycles,
                 channel=starting_channel, visited=total_visits)
            status(f'full_cycle_complete_{completed_cycles}_channel_{starting_channel}')
        return dict(reason='cancelled', rooms=rooms, visited=total_visits,
                    completed_cycles=completed_cycles)
    except Exception as error:
        _log(events, result='full_scan_stopped', reason=str(error))
        raise
    finally:
        for key in ('left', 'right', 'up', 'down', 'space'):
            send_key(key, False)
