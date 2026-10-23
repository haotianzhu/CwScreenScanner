"""One-room Free Market scan cycle: start at CH.1 and scan through CH.40."""
import ctypes
import json
import os
import random
import time
from pathlib import Path

from market_browser_bot import CLIENT_SIZE, Vision, run_live
from control_state import wait_if_paused
from market_channel_vision import (CHANNELS, button_box, channel_selector_visible,
                                   find_channel_confirm, find_game_menu_change,
                                   find_selected_channel)

STATE_PATH = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ClassworldFMBot' / 'channel_state.json'
SCAN_RETRY_ATTEMPTS = 3
CHANNEL_CHANGE_RETRY_ATTEMPTS = 3
INITIAL_CHANNEL_READ_ATTEMPTS = 5


def _cannot_retry(error, stop_event):
    """Keep explicit user stops and focus/safety failures immediately actionable."""
    if stop_event and stop_event.is_set():
        return True
    message = str(error).lower()
    return any(marker in message for marker in (
        'stopped from scanner', 'stopped with f9', 'mouse failsafe',
        'game lost foreground', 'no longer available', 'minimized or too small',
    ))


def _window_id(handle):
    return handle if isinstance(handle, int) else int(getattr(handle, 'value', 0) or 0)


class ChannelSwitcher:
    """Visible UI channel switcher with packet-state confirmation when present."""
    def __init__(self, hwnd, stop_event, state_path=STATE_PATH):
        import pyautogui as gui
        from ctypes import wintypes
        self.gui = gui
        self.hwnd = _window_id(hwnd)
        self.stop_event = stop_event
        self.state_path = Path(state_path)
        self.user = ctypes.windll.user32
        self.wintypes = wintypes
        self.user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user.GetForegroundWindow.restype = wintypes.HWND
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

    def check(self):
        wait_if_paused(self.stop_event)
        if self.stop_event and self.stop_event.is_set():
            raise RuntimeError('Stopped from scanner window')
        if self.user.GetAsyncKeyState(0x78) & 0x8000:
            raise RuntimeError('Stopped with F9')
        if _window_id(self.user.GetForegroundWindow()) != self.hwnd:
            raise RuntimeError('Game lost foreground; stopped')
        if self.gui.position().x == 0 and self.gui.position().y == 0:
            raise RuntimeError('Mouse failsafe')

    def pause(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check()
            time.sleep(.03)

    def capture(self):
        self.check()
        rect = self.wintypes.RECT()
        origin = self.wintypes.POINT(0, 0)
        if not self.user.GetClientRect(self.hwnd, ctypes.byref(rect)) or not self.user.ClientToScreen(self.hwnd, ctypes.byref(origin)):
            raise RuntimeError('Cannot locate game client')
        if (rect.right, rect.bottom) != CLIENT_SIZE:
            raise RuntimeError(f'Game client must be {CLIENT_SIZE[0]}x{CLIENT_SIZE[1]}; current size is {rect.right}x{rect.bottom}')
        return self.gui.screenshot(region=(origin.x, origin.y, rect.right, rect.bottom)), (origin.x, origin.y)

    def press_escape(self):
        from market_keyboard import send_key
        self.check()
        send_key('esc', True)
        try:
            self.pause(random.uniform(.025, .07))
        finally:
            send_key('esc', False)
        self.pause(random.uniform(.05, .11))

    def move_to(self, x, y):
        """Move with a varied two-part path instead of a fixed-speed line."""
        start = self.gui.position()
        dx, dy = x - start.x, y - start.y
        distance = (dx * dx + dy * dy) ** .5
        tweens = (self.gui.easeInQuad, self.gui.easeOutQuad, self.gui.easeInOutQuad)
        if distance > 220 and random.random() < .20:
            fraction = random.uniform(.35, .65)
            bend = random.uniform(-.06, .06)
            middle_x = round(start.x + dx * fraction - dy * bend)
            middle_y = round(start.y + dy * fraction + dx * bend)
            self.gui.moveTo(middle_x, middle_y, duration=random.triangular(.03, .12, .06), tween=random.choice(tweens))
        self.gui.moveTo(x, y, duration=random.triangular(.06, .25, .11), tween=random.choice(tweens))

    def click_box(self, box, origin):
        left, top, right, bottom = box
        self.pause(random.triangular(.015, .075, .03))
        x = (left + right) // 2 + random.randint(-4, 4)
        y = (top + bottom) // 2 + random.randint(-3, 3)
        self.move_to(origin[0] + x, origin[1] + y)
        self.pause(random.triangular(.012, .06, .025))
        self.check()
        self.gui.click()
        self.pause(random.triangular(.035, .13, .055))

    def wait_for(self, detector, timeout):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            image, origin = self.capture()
            try:
                return detector(image), origin
            except RuntimeError as error:
                last_error = error
            self.pause(.07)
        raise RuntimeError(f'Expected channel UI did not appear: {last_error or "no visual match"}')

    def open_dialog(self):
        self.press_escape()
        menu, origin = self.wait_for(find_game_menu_change, 2)
        self.click_box(menu, origin)
        selected, origin = self.wait_for(find_selected_channel, 2)
        return selected, origin

    def packet_channel(self, after):
        try:
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return None
        if not state.get('valid') or state.get('observed_at', 0) < after:
            return None
        channel = state.get('channel')
        return channel if isinstance(channel, int) and 1 <= channel <= CHANNELS else None

    def close_dialog(self):
        # A single Escape closes the selector and returns to the game view.
        self.press_escape()

    def reset_channel_ui(self):
        """Close a partially open ESC or channel menu before another attempt."""
        image, _ = self.capture()
        if channel_selector_visible(image):
            self.press_escape()
            self.pause(.12)
            return 'selector_closed'
        try:
            find_game_menu_change(image)
        except RuntimeError:
            return 'game_view'
        self.press_escape()
        self.pause(.12)
        return 'menu_closed'

    def ensure_store_closed(self):
        """Close an open visitor shop only by clicking LEAVE STORE.

        The game menu may use Esc only after two clear frames prove no visitor
        window remains.  This prevents an Esc intended for the menu from being
        consumed by a shop window.
        """
        vision = Vision()
        clear_frames = 0
        close_attempts = 0
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            image, origin = self.capture()
            state = vision.inspect(image)
            if state['owner']:
                raise RuntimeError('Own shop window is open; channel change was not attempted')
            if not state['leave']:
                clear_frames += 1
                if clear_frames >= 2:
                    return close_attempts
                self.pause(.07)
                continue
            clear_frames = 0
            if len(state['leave']) != 1:
                raise RuntimeError('Visitor shop LEAVE STORE button is ambiguous')
            if close_attempts >= 2:
                break
            close_attempts += 1
            left, top, width, height, _ = state['leave'][0]
            self.click_box((left, top, left + width, top + height), origin)
            self.pause(random.triangular(.08, .23, .12))
        raise RuntimeError('Shop window still visible after LEAVE STORE click')

    def humanize_after_channel_change(self):
        """Occasionally make one brief correction after a channel load."""
        if random.random() >= .18:
            return []
        from market_keyboard import send_key
        side = random.choice(('left', 'right'))
        duration = random.triangular(.03, .11, .05)
        self.check()
        send_key(side, True)
        try:
            self.pause(duration)
        finally:
            send_key(side, False)
        return [dict(key=side, jumps=0, seconds=round(duration, 3))]

    def current_channel(self):
        """Read the selected channel once before the first scan, then close it."""
        self.ensure_store_closed()
        (channel, _), _ = self.open_dialog()
        self.close_dialog()
        return channel

    def select_and_confirm(self, target):
        self.ensure_store_closed()
        (current, _), origin = self.open_dialog()
        self.click_box(button_box(target, CLIENT_SIZE[0], CLIENT_SIZE[1]), origin)
        (selected, _), origin = self.wait_for(find_selected_channel, 1.5)
        if selected != target:
            self.close_dialog()
            raise RuntimeError(f'Channel selection stayed at CH.{selected}, expected CH.{target}')
        image, origin = self.capture()
        confirm = find_channel_confirm(image)
        self.click_box(confirm, origin)
        return current

    def verify(self, target, started):
        """Prefer packet state; visual menu confirmation is the safe fallback."""
        # Only wait for packet confirmation while the passive collector's
        # two-second heartbeat is current. A stale state file from a previous
        # run must not add twelve seconds to every channel change.
        try:
            heartbeat = self.state_path.with_name('channel_capture_status.json')
            status = json.loads(heartbeat.read_text(encoding='utf-8'))
            packet_service_available = (
                status.get('mode') == 'windivert_sniff_recv_only'
                and time.time() - float(status.get('observed_at', 0)) < 5
            )
        except (OSError, ValueError, TypeError):
            packet_service_available = False
        # Let the reconnect animation complete before reopening the ESC menu.
        self.pause(random.uniform(1.15, 1.75))
        if packet_service_available:
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                channel = self.packet_channel(started)
                if channel == target:
                    return 'packet'
                self.pause(.25)
        # No new passive state is available. Reopen the selector and prove the
        # selected blue channel matches the requested target before scanning.
        (actual, _), _ = self.open_dialog()
        if actual == target:
            self.close_dialog()
            return 'visual'
        self.close_dialog()
        raise RuntimeError(f'Channel did not change to CH.{target}; current selection is CH.{actual}')


def _log(path, **event):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(time=time.time(), **event), ensure_ascii=False) + '\n')


def run_room_channels(folder, hwnd, start_event, stop_event, status_callback=None,
                      room=1, initial_channel=None):
    """Scan one room from its current channel through CH.40, then return CH.1.

    ``initial_channel`` is supplied by the full route after it has verified the
    channel at the FM entrance.  Each later room begins at CH.1 because the
    preceding room explicitly returned there.
    """
    folder = Path(folder)
    events = folder / 'channels.jsonl'
    ready = type(start_event)()
    ready.set()
    switcher = ChannelSwitcher(hwnd, stop_event)
    channel = initial_channel if initial_channel is not None else switcher.current_channel()
    if not isinstance(channel, int) or not 1 <= channel <= CHANNELS:
        raise RuntimeError(f'Invalid starting channel for room {room}: {channel!r}')
    _log(events, result='starting_channel_detected', room=room, channel=channel)
    scanned, skipped, visits = [], [], 0
    while channel <= CHANNELS:
        if stop_event and stop_event.is_set():
            return dict(reason='cancelled', room=room, scanned=scanned, skipped=skipped, visited=visits)
        if status_callback:
            status_callback(f'full_scanning_room_{room}_channel_{channel}')
            status_callback(('progress', dict(room=room, channel=channel, visited=visits, point=None)))
        run_folder = folder / f'room_{room:02d}_ch_{channel:02d}'
        run_folder.mkdir(parents=True, exist_ok=True)
        _log(events, result='channel_scan_started', room=room, channel=channel)

        def on_visit(point, active_channel=channel):
            nonlocal visits
            visits += 1
            if status_callback:
                status_callback(('progress', dict(room=room, channel=active_channel,
                                                   visited=visits,
                                                   point=(point['x'], point['y']))))

        outcome = None
        scan_failures = []
        for attempt in range(1, SCAN_RETRY_ATTEMPTS + 1):
            try:
                outcome = run_live(Vision(), run_folder, 300, start_event=ready,
                                   stop_event=stop_event, status_callback=status_callback,
                                   target_hwnd=hwnd, visit_callback=on_visit)
                break
            except RuntimeError as error:
                if _cannot_retry(error, stop_event):
                    raise
                scan_failures.append(str(error))
                _log(events, result='channel_scan_retry', room=room, channel=channel,
                     attempt=attempt, max_attempts=SCAN_RETRY_ATTEMPTS,
                     reason=str(error))
                if status_callback:
                    status_callback(f'full_retrying_scan_room_{room}_channel_{channel}_attempt_{attempt}')
                try:
                    switcher.ensure_store_closed()
                except RuntimeError as recovery_error:
                    if _cannot_retry(recovery_error, stop_event):
                        raise
                    _log(events, result='channel_scan_recovery_failed', room=room,
                         channel=channel, attempt=attempt, reason=str(recovery_error))
                try:
                    menu_state = switcher.reset_channel_ui()
                    _log(events, result='channel_ui_reset', room=room,
                         channel=channel, attempt=attempt, state=menu_state)
                except RuntimeError as recovery_error:
                    if _cannot_retry(recovery_error, stop_event):
                        raise
                    _log(events, result='channel_ui_reset_failed', room=room,
                         channel=channel, attempt=attempt, reason=str(recovery_error))
                if attempt < SCAN_RETRY_ATTEMPTS:
                    switcher.pause(.25 * attempt)
        if outcome is None:
            outcome = dict(reason='scan_failed_after_retries', failures=scan_failures)
        outcome = outcome or {}
        scanned.append(channel)
        _log(events, result='channel_scan_finished', room=room, channel=channel, outcome=outcome)
        if outcome.get('reason') == 'cancelled_before_start':
            return dict(reason='cancelled', room=room, scanned=scanned, skipped=skipped, visited=visits)
        if channel == CHANNELS:
            break
        target = channel + 1
        while target <= CHANNELS:
            if status_callback:
                status_callback(f'full_switching_room_{room}_channel_{target}')
            started = time.time()
            changed = None
            failures = []
            for attempt in range(1, CHANNEL_CHANGE_RETRY_ATTEMPTS + 1):
                try:
                    started = time.time()
                    previous = switcher.select_and_confirm(target)
                    source = switcher.verify(target, started)
                    changed = (previous, source)
                    break
                except RuntimeError as error:
                    if _cannot_retry(error, stop_event):
                        raise
                    failures.append(str(error))
                    _log(events, result='channel_change_retry', room=room,
                         target=target, attempt=attempt,
                         max_attempts=CHANNEL_CHANGE_RETRY_ATTEMPTS, reason=str(error))
                    if status_callback:
                        status_callback(f'full_retrying_channel_room_{room}_channel_{target}_attempt_{attempt}')
                    try:
                        switcher.ensure_store_closed()
                    except RuntimeError as recovery_error:
                        if _cannot_retry(recovery_error, stop_event):
                            raise
                        _log(events, result='channel_change_recovery_failed', room=room,
                             target=target, attempt=attempt, reason=str(recovery_error))
                    try:
                        menu_state = switcher.reset_channel_ui()
                        _log(events, result='channel_ui_reset', room=room,
                             channel=target, attempt=attempt, state=menu_state)
                    except RuntimeError as recovery_error:
                        if _cannot_retry(recovery_error, stop_event):
                            raise
                        _log(events, result='channel_ui_reset_failed', room=room,
                             channel=target, attempt=attempt, reason=str(recovery_error))
                    if attempt < CHANNEL_CHANGE_RETRY_ATTEMPTS:
                        switcher.pause(.20 * attempt)
            if changed is None:
                _log(events, result='channel_change_failed', room=room, target=target,
                     attempts=CHANNEL_CHANGE_RETRY_ATTEMPTS, failures=failures)
                skipped.append(target)
                target += 1
            else:
                previous, source = changed
                actions = switcher.humanize_after_channel_change()
                _log(events, result='channel_changed', room=room, previous=previous,
                     channel=target, confirmation=source, settling_actions=actions)
                channel = target
                break
        else:
            return dict(reason='no_remaining_channel', room=room, scanned=scanned,
                        skipped=skipped, visited=visits)

    # Prefer CH.1 for the next room.  If that change is unavailable, keep
    # trying CH.2…CH.40 and exit through the first confirmed channel instead
    # of stranding the route at CH.40.
    return_failures = []
    for return_channel in range(1, CHANNELS + 1):
        if status_callback:
            status_callback(f'full_returning_room_{room}_channel_{return_channel}')
        changed = None
        failures = []
        for attempt in range(1, CHANNEL_CHANGE_RETRY_ATTEMPTS + 1):
            try:
                started = time.time()
                previous = switcher.select_and_confirm(return_channel)
                source = switcher.verify(return_channel, started)
                changed = (previous, source)
                break
            except RuntimeError as error:
                if _cannot_retry(error, stop_event):
                    raise
                failures.append(str(error))
                _log(events, result='return_channel_change_retry', room=room,
                     target=return_channel, attempt=attempt,
                     max_attempts=CHANNEL_CHANGE_RETRY_ATTEMPTS, reason=str(error))
                if status_callback:
                    status_callback(f'full_retrying_return_room_{room}_channel_{return_channel}_attempt_{attempt}')
                try:
                    switcher.ensure_store_closed()
                except RuntimeError as recovery_error:
                    if _cannot_retry(recovery_error, stop_event):
                        raise
                    _log(events, result='return_channel_recovery_failed', room=room,
                         target=return_channel, attempt=attempt, reason=str(recovery_error))
                try:
                    menu_state = switcher.reset_channel_ui()
                    _log(events, result='channel_ui_reset', room=room,
                         channel=return_channel, attempt=attempt, state=menu_state)
                except RuntimeError as recovery_error:
                    if _cannot_retry(recovery_error, stop_event):
                        raise
                    _log(events, result='channel_ui_reset_failed', room=room,
                         channel=return_channel, attempt=attempt, reason=str(recovery_error))
                if attempt < CHANNEL_CHANGE_RETRY_ATTEMPTS:
                    switcher.pause(.20 * attempt)
        if changed is None:
            return_failures.append(dict(channel=return_channel, failures=failures))
            _log(events, result='return_channel_change_failed', room=room,
                 target=return_channel, attempts=CHANNEL_CHANGE_RETRY_ATTEMPTS,
                 failures=failures)
            continue
        previous, source = changed
        actions = switcher.humanize_after_channel_change()
        _log(events, result='returned_to_exit_channel', room=room, previous=previous,
             channel=return_channel, confirmation=source, settling_actions=actions,
             failed_return_channels=return_failures)
        return dict(reason='finished_through_ch_40_returned_exit_channel', room=room,
                    return_channel=return_channel,
                    started_channel=scanned[0] if scanned else channel,
                    scanned=scanned, skipped=skipped, visited=visits,
                    failed_return_channels=return_failures)
    return dict(reason='failed_to_find_exit_channel', room=room,
                started_channel=scanned[0] if scanned else channel,
                scanned=scanned, skipped=skipped, visited=visits,
                failed_return_channels=return_failures)


def run_room_one_channels(folder, hwnd, start_event, stop_event, status_callback=None):
    """Compatibility wrapper for callers that request only room 1."""
    return run_room_channels(folder, hwnd, start_event, stop_event,
                             status_callback=status_callback, room=1)

def establish_capture_connection(hwnd, stop_event, status_callback=None):
    """Create a fresh passive-capture session without treating one CH failure as fatal.

    Starting from the visibly selected channel, attempt every following channel
    in order.  The first confirmed change supplies a complete client handshake
    for passive decoding and remains selected, so the room scan naturally
    continues from that channel instead of moving back to the original one.
    """
    if status_callback:
        status_callback('route_calibration_connecting')
    switcher = ChannelSwitcher(hwnd, stop_event)
    current = None
    read_failures = []
    for attempt in range(1, INITIAL_CHANNEL_READ_ATTEMPTS + 1):
        try:
            current = switcher.current_channel()
            break
        except RuntimeError as error:
            if _cannot_retry(error, stop_event):
                raise
            read_failures.append(str(error))
            _log(STATE_PATH.with_suffix('.jsonl'), result='initial_channel_read_retry',
                 attempt=attempt, max_attempts=INITIAL_CHANNEL_READ_ATTEMPTS,
                 reason=str(error))
            if status_callback:
                status_callback(f'route_calibration_retrying_read_channel_{attempt}')
            try:
                switcher.reset_channel_ui()
            except RuntimeError as recovery_error:
                if _cannot_retry(recovery_error, stop_event):
                    raise
                _log(STATE_PATH.with_suffix('.jsonl'), result='initial_channel_ui_reset_failed',
                     attempt=attempt, reason=str(recovery_error))
            if attempt < INITIAL_CHANNEL_READ_ATTEMPTS:
                switcher.pause(.30 * attempt)
    if current is None:
        raise RuntimeError(
            f'Could not read the current channel after {INITIAL_CHANNEL_READ_ATTEMPTS} retries: '
            f'{read_failures[-1] if read_failures else "unknown error"}'
        )
    candidates = list(range(current + 1, CHANNELS + 1)) + list(range(1, current))
    failures = []
    for target in candidates:
        if status_callback:
            status_callback(f'route_calibration_trying_channel_{target}')
        target_failures = []
        connected = False
        for attempt in range(1, CHANNEL_CHANGE_RETRY_ATTEMPTS + 1):
            try:
                started = time.time()
                switcher.select_and_confirm(target)
                switcher.verify(target, started)
                connected = True
                break
            except RuntimeError as error:
                if _cannot_retry(error, stop_event):
                    raise
                target_failures.append(str(error))
                _log(STATE_PATH.with_suffix('.jsonl'), result='capture_connection_retry',
                     target=target, attempt=attempt,
                     max_attempts=CHANNEL_CHANGE_RETRY_ATTEMPTS, reason=str(error))
                try:
                    switcher.reset_channel_ui()
                except RuntimeError as recovery_error:
                    if _cannot_retry(recovery_error, stop_event):
                        raise
                if attempt < CHANNEL_CHANGE_RETRY_ATTEMPTS:
                    switcher.pause(.20 * attempt)
        if not connected:
            failures.append(dict(channel=target, failures=target_failures))
            continue
        if status_callback:
            status_callback(f'route_calibration_connection_ready_{target}')
        return target
    detail = '; '.join(f'CH.{item["channel"]}: {item["reason"]}' for item in failures)
    raise RuntimeError(f'No channel change could be confirmed after CH.{current}: {detail}')

