"""Position-feedback navigation primitives for the Free Market room cycle.

A route profile supplies portal coordinates from a live map observation.  The
controller never advances by elapsed time alone: each pulse waits for a new,
continuous client position.  It deliberately approaches a portal from one
side, passes it slightly, walks back a small distance, then presses Up.
"""
import json
import random
import time
from pathlib import Path


class RouteError(RuntimeError):
    pass


class PositionFeed:
    def __init__(self, path):
        self.path = Path(path)

    def read(self, expected_map=None, max_age=2.5):
        try:
            state = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise RouteError(f'Position feed unavailable: {error}') from error
        if not state.get('valid'):
            raise RouteError(state.get('reason', 'Position feed is not valid'))
        if time.time() - float(state.get('observed_at', 0)) > max_age:
            raise RouteError('Position feed is stale')
        if expected_map is not None and state.get('map_id') != expected_map:
            raise RouteError('Position belongs to another map')
        if not all(isinstance(state.get(key), int) for key in ('x', 'y', 'foothold', 'sequence', 'map_id')):
            raise RouteError('Position feed is incomplete')
        return state


class Motion:
    """Small key pulses backed by the passive position feed.

    ``pulse(keys, seconds)`` must release all keys before returning. ``pause``
    and ``check`` come from the active game runner, so F9 and foreground loss
    continue to stop movement immediately.
    """
    def __init__(self, feed, pulse, pause, check, log=lambda **event: None):
        self.feed = feed
        self.pulse = pulse
        self.pause = pause
        self.check = check
        self.log = log

    def _changed(self, sequence, expected_map, timeout=1.8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            # A shop scan can leave the most recent coordinate a few
            # seconds old.  The sequence comparison below, rather than wall
            # clock age alone, is what confirms this input produced movement.
            state = self.feed.read(expected_map, max_age=25)
            if state['sequence'] != sequence:
                return state
            self.pause(.08)
        raise RouteError('No fresh position after movement pulse')

    def _walk_pulse(self, side, state, expected_map):
        self.check()
        seconds = random.triangular(.045, .19, .085)
        sequence = state['sequence']
        self.pulse([side], seconds)
        changed = self._changed(sequence, expected_map)
        self.log(result='route_walk_pulse', key=side, seconds=round(seconds, 3),
                 position=[changed['x'], changed['y']], map_id=changed['map_id'])
        return changed

    def walk_to_x(self, portal_x, expected_map, direction):
        """Approach from a side, then reverse slightly before pressing Up."""
        if direction not in ('left', 'right'):
            raise RouteError('Portal direction must be left or right')
        sign = 1 if direction == 'right' else -1
        # A confirmed coordinate remains useful while the player is
        # reading a shop or a channel panel. Freshness is enforced again after
        # every input pulse by ``_changed``.
        state = self.feed.read(expected_map, max_age=20)
        # Passing the portal by a short human-sized distance prevents a fixed
        # dead-centre key release, which can miss narrow portal hitboxes.
        passed_x = portal_x + sign * random.randint(16, 34)
        for _ in range(90):
            if (sign > 0 and state['x'] >= passed_x) or (sign < 0 and state['x'] <= passed_x):
                break
            state = self._walk_pulse(direction, state, expected_map)
        else:
            raise RouteError('Portal approach exceeded pulse limit')
        settle_x = portal_x - sign * random.randint(7, 15)
        reverse = 'left' if direction == 'right' else 'right'
        for _ in range(32):
            # Portal hitboxes are narrow.  Once a report is close to the real
            # portal coordinate, stop instead of allowing the next random
            # reverse pulse to cross it by a large amount.
            if abs(state['x'] - portal_x) <= 10 or abs(state['x'] - settle_x) <= 5:
                self.pause(random.triangular(.08, .36, .16))
                return state
            if (sign > 0 and state['x'] <= settle_x) or (sign < 0 and state['x'] >= settle_x):
                # A large reverse pulse crossed the settle point.  Preserve a
                # bounded human correction toward the true portal coordinate.
                correction = 'left' if state['x'] > portal_x else 'right'
                state = self._walk_pulse(correction, state, expected_map)
                continue
            state = self._walk_pulse(reverse, state, expected_map)
        raise RouteError('Portal back-off exceeded pulse limit')

    def enter_portal(self, portal, expected_map, expected_next_map=None):
        state = self.walk_to_x(portal['x'], expected_map, portal['direction'])
        self.check()
        before = state['map_id']
        before_epoch = state.get('map_epoch')
        self.pulse(['up'], random.triangular(.11, .26, .16))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.check()
            try:
                now = self.feed.read()
            except RouteError:
                self.pause(.1)
                continue
            transitioned = now['map_id'] != before or now.get('map_epoch') != before_epoch
            if transitioned:
                if expected_next_map is not None and now['map_id'] != expected_next_map:
                    raise RouteError('Portal entered an unexpected map')
                self.log(result='portal_entered', from_map=before, to_map=now['map_id'],
                         from_epoch=before_epoch, to_epoch=now.get('map_epoch'),
                         from_position=[state['x'], state['y']])
                return now
            self.pause(.1)
        raise RouteError('Portal transition was not confirmed')

    def drop_to_y(self, target_y, expected_map, repetitions=None):
        """Use bounded down+jump drops until the portal level is reached."""
        state = self.feed.read(expected_map, max_age=20)
        if state['y'] >= target_y - 45:
            return state
        total = repetitions if repetitions is not None else random.randint(3, 5)
        for _ in range(max(3, total)):
            self.check()
            sequence = state['sequence']
            self.pulse(['down', 'space'], random.triangular(.11, .24, .16))
            self.pause(random.triangular(.10, .32, .17))
            try:
                state = self._changed(sequence, expected_map, timeout=1.8)
            except RouteError as error:
                # A drop can have no final absolute fragment.  Keep the last
                # trusted coordinate and complete the agreed 3+ reset inputs;
                # the following horizontal portal approach will require a new
                # confirmed movement report before it can enter a portal.
                self.log(result='route_drop_fragment_pending', reason=str(error),
                         position=[state['x'], state['y']], map_id=expected_map)
            if state['y'] >= target_y - 45:
                self.log(result='route_drop_level_confirmed',
                         position=[state['x'], state['y']], map_id=expected_map)
                return state
        self.log(result='route_drop_reset_complete_unconfirmed',
                 position=[state['x'], state['y']], map_id=expected_map)
        return state

    def walk_to_edge(self, side, expected_map):
        """Walk until three consecutive valid reports show no horizontal gain."""
        if side not in ('left', 'right'):
            raise RouteError('Edge direction must be left or right')
        state = self.feed.read(expected_map)
        stalled = 0
        for _ in range(120):
            prior = state['x']
            state = self._walk_pulse(side, state, expected_map)
            gain = state['x'] - prior
            advancing = gain > 1 if side == 'right' else gain < -1
            stalled = 0 if advancing else stalled + 1
            if stalled >= 3:
                self.log(result='route_edge_reached', side=side,
                         position=[state['x'], state['y']], map_id=expected_map)
                return state
        raise RouteError('Map edge was not reached')

    def reset_to_left_exit(self, expected_map, drops=None):
        """Fallback for a room with a left-side exit after a lower reset.

        The reset is only used when the normal portal coordinate is unavailable.
        It performs the agreed down+jump reset at least three times, establishes
        the right boundary with feedback, then returns to the left boundary.
        """
        repetitions = drops if drops is not None else random.randint(3, 5)
        for _ in range(max(3, repetitions)):
            self.check()
            self.pulse(['down', 'space'], random.triangular(.11, .25, .16))
            self.pause(random.triangular(.10, .33, .17))
        self.walk_to_edge('right', expected_map)
        return self.walk_to_edge('left', expected_map)
