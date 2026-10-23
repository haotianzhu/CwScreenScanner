"""Short, feedback-verified Free Market route test for room 1.

The test generates visible keyboard/mouse input only.  It always verifies map
transitions from the passive local position feed and stops on any mismatch.
No route profile is written or reused.
"""
import json
import time
from pathlib import Path

from free_market_route import Motion, PositionFeed, RouteError
from market_browser_bot import Vision, run_live
from market_channel_cycle import ChannelSwitcher
from market_keyboard import send_key
from position_state_collector import POSITION_STATE

ENTRANCE = 80002000
ROOM_ONE = 80002001
ENTRANCE_PORTAL = dict(x=524)
ROOM_EXIT = dict(x=793, y=33)
TEST_CHANNELS = (40, 1)


def _log(path, **event):
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(at=time.time(), **event), ensure_ascii=False) + '\n')


def _wait_for(feed, map_id, pause, check, timeout=15):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        check()
        try:
            return feed.read(map_id, max_age=20)
        except RouteError as error:
            last_error = error
            pause(.10)
    raise RouteError(f'Expected map {map_id} did not supply a fresh position: {last_error}')


def run(folder, hwnd, start_event, stop_event, status_callback=None):
    """Run a one-shop-per-channel route through room 1 and back to FM."""
    folder = Path(folder)
    events = folder / 'flow_test.jsonl'
    switcher = ChannelSwitcher(hwnd, stop_event)
    feed = PositionFeed(POSITION_STATE)

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

    motion = Motion(feed, pulse, switcher.pause, switcher.check, motion_log)

    def choose(target):
        status(f'flow_switching_to_channel_{target}')
        started = time.time()
        previous = switcher.select_and_confirm(target)
        confirmation = switcher.verify(target, started)
        actions = switcher.humanize_after_channel_change()
        _log(events, result='channel_changed', previous=previous, channel=target,
             confirmation=confirmation, settling_actions=actions)

    def scan_one(channel):
        status(f'flow_scanning_channel_{channel}')
        ready = type(start_event)()
        ready.set()
        scan_folder = folder / f'room_01_ch_{channel:02d}'
        scan_folder.mkdir(parents=True, exist_ok=True)
        outcome = run_live(Vision(), scan_folder, 1,
                           start_event=ready, stop_event=stop_event,
                           status_callback=status_callback, target_hwnd=hwnd,
                           max_open_attempts=12)
        _log(events, result='one_shop_scan_finished', channel=channel, outcome=outcome or {})
        if not outcome or outcome.get('visited') != 1:
            raise RuntimeError(f'CH.{channel} did not complete one confirmed shop visit')

    try:
        status('flow_preparing_channel_40')
        current = switcher.current_channel()
        # Begin the short test in the quieter CH.40.  If it is already active,
        # make one visible round trip so passive capture receives a new key.
        if current == 40:
            choose(1)
        choose(40)
        entrance = _wait_for(feed, ENTRANCE, switcher.pause, switcher.check)
        direction = 'right' if entrance['x'] < ENTRANCE_PORTAL['x'] else 'left'
        status('flow_entering_room_1')
        arrived = motion.enter_portal(dict(ENTRANCE_PORTAL, direction=direction), ENTRANCE, ROOM_ONE)
        _log(events, result='entered_room_1', position=[arrived['x'], arrived['y']])

        scan_one(40)
        choose(1)
        _wait_for(feed, ROOM_ONE, switcher.pause, switcher.check)
        scan_one(1)
        switcher.ensure_store_closed()
        room = _wait_for(feed, ROOM_ONE, switcher.pause, switcher.check)
        if room['y'] < ROOM_EXIT['y'] - 45:
            status('flow_resetting_to_exit_level')
            room = motion.drop_to_y(ROOM_EXIT['y'], ROOM_ONE)
        direction = 'right' if room['x'] < ROOM_EXIT['x'] else 'left'
        status('flow_exiting_room_1')
        returned = motion.enter_portal(dict(ROOM_EXIT, direction=direction), ROOM_ONE, ENTRANCE)
        _log(events, result='returned_to_entrance', position=[returned['x'], returned['y']])
        # The next room entrance is on the same ground platform.  Entering it
        # proves the cross-room transition while keeping this a short test.
        status('flow_entering_room_2')
        direction = 'right' if returned['x'] < 674 else 'left'
        room_two = motion.enter_portal(dict(x=674, direction=direction), ENTRANCE, 80002002)
        _log(events, result='entered_room_2', position=[room_two['x'], room_two['y']])
        return dict(reason='flow_test_complete', room=2, channels=list(TEST_CHANNELS), visited=2)
    except Exception as error:
        _log(events, result='flow_test_stopped', reason=str(error))
        raise
    finally:
        for key in ('left', 'right', 'up', 'down', 'space'):
            send_key(key, False)
