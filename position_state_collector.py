"""Passive, derived-only player position feed for the Free Market scanner.

The collector opens WinDivert in SNIFF | RECV_ONLY mode.  It never sends,
forwards, blocks, or persists game packets.  It writes only validated current
location and movement coordinates to ``player_position.json``.
"""
import ctypes
import json
import os
import queue
import struct
import threading
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:  # cryptography before 43
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

from channel_state_collector import (DEFAULT_STATE, LIMIT, SNIFF_RECV_ONLY,
                                     Sniffer, advance, atomic_json, game_owner,
                                     inspect_map, MAP_ENTRY_CODES)

POSITION_STATE = DEFAULT_STATE.with_name('player_position.json')
CHANNEL_MAP = 0x32
MOVEMENT_INDEX = 0x14
FILTER = ('ip and tcp and !loopback and ((inbound and tcp.SrcPort >= 8500 and '
          'tcp.SrcPort <= 8599) or (outbound and tcp.DstPort >= 8500 and '
          'tcp.DstPort <= 8599))')
AES_KEY = bytes.fromhex('0f0000001b000000c500000046000000f3000000be000000ff00000075000000')
DES_KEY = b'G0dD@mnN#H@ckEr!'


def frame_header(length, iv, client):
    if len(iv) != 4 or not 2 <= length <= 65535:
        raise ValueError('Invalid frame header')
    first = int.from_bytes(iv[2:4], 'little') ^ (223 if client else 65534)
    return struct.pack('<HH', first, first ^ length)


def aes_payload(data, iv):
    """ClassicWorld client-frame transform; used only to read local input."""
    if len(iv) != 4:
        raise ValueError('Invalid client IV')
    output = bytearray()
    offset, chunk = 0, 1456
    while offset < len(data):
        block = data[offset:offset + chunk]
        cipher = Cipher(algorithms.AES(AES_KEY), modes.OFB(iv * 4)).encryptor()
        output.extend(cipher.update(block) + cipher.finalize())
        offset += len(block)
        chunk = 1460
    return bytes(output)


def opcode_table(plain):
    if len(plain) < 6 or plain[:2] != b'\x32\0':
        raise ValueError('Not an opcode table')
    size = struct.unpack_from('<I', plain, 2)[0]
    if size != len(plain) - 6 or size % 8:
        raise ValueError('Invalid opcode table size')
    cipher = Cipher(TripleDES(DES_KEY + DES_KEY[:8]), modes.ECB()).decryptor()
    raw = (cipher.update(plain[6:]) + cipher.finalize()).split(b'\0', 1)[0].rstrip(b' ')
    fields = raw.split(b'|')
    if not fields or not all(value.isdigit() for value in fields):
        raise ValueError('Invalid opcode table contents')
    count, *values = map(int, fields)
    if count != len(values) or len(set(values)) != count or any(value > 65535 for value in values):
        raise ValueError('Invalid opcode table entries')
    return values


def parse_movement(plain):
    """Parse only the observed complete movement layout; reject all others."""
    if len(plain) < 36:
        raise ValueError('Short movement message')
    x, y, vx, vy, count = struct.unpack_from('<hhhhH', plain, 16)
    if not 1 <= count <= 256:
        raise ValueError('Invalid movement fragment count')
    offset, fragments = 26, []
    for _ in range(count):
        if offset >= len(plain):
            raise ValueError('Truncated movement fragment')
        command = plain[offset]
        # ClassicWorld uses the original MapleStory movement family.  During
        # a down-jump it emits an alternate relative/absolute subtype rather
        # than the ordinary walking subtype.  These layouts carry the same
        # field widths; only their action code differs.
        absolute_commands = {0, 5, 17, 21, 22}
        relative_commands = {1, 2, 6, 12, 13, 16}
        action_commands = {3, 4, 7, 8, 9, 10, 14, 15}
        if command in absolute_commands:
            size = 21
        elif command in relative_commands:
            size = 9
        elif command in action_commands:
            size = 2
        else:
            raise ValueError(f'Unverified movement fragment type {command}')
        if offset + size > len(plain) - 10:
            raise ValueError('Truncated movement fragment')
        part = dict(command=command)
        if command in absolute_commands:
            px, py, pvx, pvy, foothold = struct.unpack_from('<hhhhh', plain, offset + 1)
            part.update(x=px, y=py, vx=pvx, vy=pvy, foothold=foothold,
                        stance=plain[offset + 17])
        fragments.append(part)
        offset += size
    if len(plain) - offset != 10 or plain[offset] != 0x11:
        raise ValueError('Unverified movement trailer')
    absolute = [index for index, part in enumerate(fragments) if part['command'] == 0]
    if not absolute or any(part['command'] == 1 for part in fragments[absolute[-1] + 1:]):
        # Relative fragments after the final absolute location do not establish
        # a final world coordinate. Conservatively wait for the next report.
        raise ValueError('No final absolute position')
    return dict(start=dict(x=x, y=y, vx=vx, vy=vy), end=fragments[absolute[-1]])


def packet(raw):
    if len(raw) < 40 or raw[0] >> 4 != 4 or raw[9] != 6:
        return None
    ip_size = (raw[0] & 15) * 4
    total = int.from_bytes(raw[2:4], 'big')
    if ip_size < 20 or total > len(raw) or total < ip_size + 20 or int.from_bytes(raw[6:8], 'big') & 0x3fff:
        return None
    tcp = raw[ip_size:total]
    header = (tcp[12] >> 4) * 4
    if header < 20 or header > len(tcp):
        return None
    source_port, destination_port, sequence = struct.unpack_from('!HHI', tcp)
    source = '.'.join(map(str, raw[12:16])); destination = '.'.join(map(str, raw[16:20]))
    if 8500 <= source_port <= 8599:
        return (source, source_port, destination, destination_port), 'server', sequence, tcp[13], tcp[header:]
    if 8500 <= destination_port <= 8599:
        return (destination, destination_port, source, source_port), 'client', sequence, tcp[13], tcp[header:]
    return None


class Stream:
    def __init__(self, sequence):
        self.next = sequence & 0xffffffff
        self.pending = []
        self.gap_since = None

    def feed(self, sequence, data, now):
        offset = ((sequence - self.next + 2**31) % 2**32) - 2**31
        if offset < 0:
            data = data[min(-offset, len(data)):]
            offset = 0
        if data:
            if offset > LIMIT:
                raise ValueError('TCP gap exceeds limit')
            for position, other in self.pending:
                left, right = max(position, offset), min(position + len(other), offset + len(data))
                if right > left and other[left - position:right - position] != data[left - offset:right - offset]:
                    raise ValueError('Conflicting TCP overlap')
            if (offset, data) not in self.pending:
                self.pending.append((offset, data))
        if len(self.pending) > 512 or sum(len(data) for _, data in self.pending) > LIMIT:
            raise ValueError('TCP reassembly limit')
        result = bytearray()
        self.pending.sort(key=lambda entry: entry[0])
        while self.pending and self.pending[0][0] <= len(result):
            position, data = self.pending.pop(0)
            result.extend(data[max(0, len(result) - position):])
        self.next = (self.next + len(result)) & 0xffffffff
        self.pending = [(position - len(result), data) for position, data in self.pending]
        self.gap_since = self.gap_since if self.pending and self.gap_since is not None else (now if self.pending else None)
        return bytes(result)


class PositionTracker:
    def __init__(self, output):
        self.output = Path(output)
        self.active = None
        self.sessions = {}
        self.sequence = 0
        self.map_loads = 0
        self.valid_positions = 0
        self.last_error = None
        self.segments = []
        self.write(False, 'waiting_for_game_connection')

    def write(self, valid, reason, **fields):
        atomic_json(self.output, dict(version=1, valid=valid, reason=reason,
                                      observed_at=time.time(), **fields))

    def started(self, session):
        self.active = session
        self.sessions[session] = dict(location=None, last=None, segment=None)
        self.write(False, 'waiting_for_map_and_movement', session=session)

    def location(self, session, location):
        state = self.sessions.get(session)
        if state is None:
            return
        if session == self.active and state.get('segment'):
            # This is the observed endpoint of the map we are leaving.  It is
            # diagnostic state for this run, never a reusable route profile.
            state['segment']['exit_position'] = state.get('last')
            state['segment']['exited_at'] = time.time()
        self.map_loads += 1
        segment = dict(map_epoch=location.get('map_epoch'), map_id=location.get('map_id'),
                       parser=location.get('parser'), entered_at=time.time(),
                       start_position=None, exit_position=None)
        self.segments.append(segment)
        self.segments = self.segments[-8:]
        state.update(location=location, last=None, segment=segment)
        if session == self.active:
            self.write(False, 'map_changed_waiting_for_movement', session=session, **location)

    def movement(self, session, parsed):
        if session != self.active:
            return
        state = self.sessions.get(session)
        if not state or not state['location']:
            return
        previous = state['last']
        state['last'] = parsed['end']
        if previous and any(previous[key] != parsed['start'][key] for key in ('x', 'y')):
            state['last'] = None
            self.write(False, 'movement_continuity_changed', session=session, **state['location'])
            return
        self.sequence += 1
        self.valid_positions += 1
        position = dict(x=parsed['end']['x'], y=parsed['end']['y'],
                        foothold=parsed['end']['foothold'], sequence=self.sequence)
        if state.get('segment'):
            if state['segment']['start_position'] is None:
                state['segment']['start_position'] = position
            state['segment']['last_position'] = position
        location = dict(state['location'])
        # Map metadata and a movement report share generic field names such as
        # ``source`` and ``validation``.  Namespace every conflicting map
        # field so an update always remains a valid, continuously readable
        # position record.
        reserved = {'session', 'sequence', 'traffic_time', 'source', 'validation',
                    'x', 'y', 'vx', 'vy', 'foothold', 'stance'}
        for key in tuple(location):
            if key in reserved:
                location[f'map_{key}'] = location.pop(key)
        self.write(True, None, session=session, sequence=self.sequence,
                   traffic_time=time.time(), source='client_movement',
                   validation='movement_continuity_and_map_entry',
                   **location, **parsed['end'])

    def invalidate(self, session, reason):
        self.last_error = reason
        state = self.sessions.get(session, {})
        # A single unknown client movement subtype must not erase the last
        # independently validated location.  Subsequent recognised movement
        # packets resume normal updates; terminal capture errors still mark
        # the feed invalid below.
        transient = reason.startswith(('Unverified movement fragment type ',
                                       'Truncated movement fragment',
                                       'Unverified movement trailer',
                                       'No final absolute position'))
        if transient and state.get('last') and state.get('location'):
            return
        if session == self.active:
            self.write(False, reason, session=session, **(state.get('location') or {}))

    def diagnostics(self):
        return dict(active_session=self.active, map_loads=self.map_loads,
                    valid_positions=self.valid_positions, last_error=self.last_error,
                    map_segments=self.segments)


class Session:
    def __init__(self, session_id, tracker):
        self.id = session_id
        self.tracker = tracker
        self.buffers = {'client': bytearray(), 'server': bytearray()}
        self.ivs = None
        self.table = []
        self.map_epoch = 0

    def feed(self, direction, data):
        self.buffers[direction].extend(data)
        if any(len(buffer) > LIMIT for buffer in self.buffers.values()):
            raise ValueError('Frame buffer limit')
        if self.ivs is None:
            greeting = self.buffers['server']
            if len(greeting) < 21:
                return
            if greeting[:11].hex() != '1300010001000000010030':
                raise ValueError('Unsupported channel greeting')
            self.ivs = {'client': bytes(greeting[11:15]), 'server': bytes(greeting[15:19])}
            del greeting[:21]
            self.tracker.started(self.id)
        for side in ('server', 'client'):
            buffer = self.buffers[side]
            while len(buffer) >= 4:
                header = bytes(buffer[:4]); left, right = struct.unpack('<HH', header); size = left ^ right
                iv = self.ivs[side]
                if size < 2 or header != frame_header(size, iv, side == 'client'):
                    raise ValueError('Frame/IV mismatch')
                if len(buffer) < size + 4:
                    break
                wire = bytes(buffer[4:4 + size]); del buffer[:4 + size]
                plain = aes_payload(wire, iv) if side == 'client' else bytes((value - iv[0]) & 255 for value in wire)
                self.ivs[side] = advance(iv)
                opcode = int.from_bytes(plain[:2], 'little')
                if side == 'server' and opcode == CHANNEL_MAP:
                    self.table = opcode_table(plain)
                elif side == 'server' and opcode in MAP_ENTRY_CODES:
                    # This client uses 0x1A9 for map load instead of the old
                    # 0x1A6 layout. A fresh epoch is sufficient to invalidate
                    # old movement coordinates even when map-id fields differ.
                    self.map_epoch += 1
                    location = inspect_map(plain) or {}
                    location.update(map_epoch=self.map_epoch, source='server_map_load')
                    self.tracker.location(self.id, location)
                elif self.table and opcode in self.table and self.table.index(opcode) == MOVEMENT_INDEX:
                    try:
                        self.tracker.movement(self.id, parse_movement(plain))
                    except ValueError as error:
                        self.tracker.invalidate(self.id, str(error))


class Collector:
    def __init__(self, output):
        self.tracker = PositionTracker(output)
        self.flows = {}
        self.packets = 0
        self.last_status = 0

    def receive(self, raw, now=None):
        now = time.monotonic() if now is None else now
        parsed = packet(raw)
        if not parsed:
            return
        self.packets += 1
        connection, direction, sequence, flags, data = parsed
        flow = self.flows.get(connection)
        if flags & 2:
            if flow is None:
                flow = dict(streams={}, buffers={'client': bytearray(), 'server': bytearray()},
                            session=None, started=now, check=0, dead=False)
                self.flows[connection] = flow
            flow['streams'].setdefault(direction, Stream(sequence + 1))
        if not flow or flow['dead']:
            return
        try:
            if data:
                stream = flow['streams'].get(direction)
                if stream is None:
                    raise ValueError('Missing directional SYN')
                ordered = stream.feed(sequence + bool(flags & 2), data, now)
                if flow['session']:
                    flow['session'].feed(direction, ordered)
                else:
                    flow['buffers'][direction].extend(ordered)
                    if sum(map(len, flow['buffers'].values())) > LIMIT:
                        raise ValueError('Ownership buffer limit')
            if flags & 5:
                flow['dead'] = True
                if flow['session']:
                    self.tracker.invalidate(flow['session'].id, 'game_connection_closed')
        except ValueError as error:
            flow['dead'] = True
            if flow['session']:
                self.tracker.invalidate(flow['session'].id, str(error))

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        for connection, flow in list(self.flows.items()):
            if flow['dead']:
                del self.flows[connection]
                continue
            try:
                if flow['session'] is None and now >= flow['check']:
                    flow['check'] = now + .25
                    if game_owner(connection):
                        session_id = uuid.uuid4().hex[:12]
                        flow['session'] = Session(session_id, self.tracker)
                        for direction in ('server', 'client'):
                            flow['session'].feed(direction, flow['buffers'][direction])
                            flow['buffers'][direction].clear()
                    elif now - flow['started'] > 5:
                        flow['dead'] = True
                if any(stream.gap_since is not None and now - stream.gap_since > 5 for stream in flow['streams'].values()):
                    raise ValueError('TCP gap timeout')
            except (ValueError, OSError) as error:
                flow['dead'] = True
                if flow['session']:
                    self.tracker.invalidate(flow['session'].id, str(error))
        if now - self.last_status > 2:
            self.last_status = now
            atomic_json(self.tracker.output.with_name('player_position_status.json'),
                        dict(version=1, observed_at=time.time(), packets=self.packets,
                             connections=len(self.flows), mode='windivert_sniff_recv_only',
                             **self.tracker.diagnostics()))


def run(output=POSITION_STATE, stop_event=None):
    collector = Collector(output)
    sniffer = Sniffer(FILTER)
    packets = queue.Queue(maxsize=8192)
    overflow = threading.Event()

    def receive_loop():
        while sniffer.handle is not None:
            try:
                packets.put_nowait(sniffer.recv())
            except queue.Full:
                overflow.set()
            except OSError:
                return

    thread = threading.Thread(target=receive_loop, name='position-capture', daemon=True)
    thread.start()
    try:
        while stop_event is None or not stop_event.is_set():
            if overflow.is_set():
                for flow in collector.flows.values():
                    flow['dead'] = True
                    if flow['session']:
                        collector.tracker.invalidate(flow['session'].id, 'local_capture_queue_overflow')
                overflow.clear()
            try:
                collector.receive(packets.get(timeout=.25))
            except queue.Empty:
                pass
            collector.tick()
    finally:
        sniffer.close()
        thread.join(timeout=2)
        collector.tracker.write(False, 'collector_stopped', session=collector.tracker.active,
                                **collector.tracker.diagnostics())


if __name__ == '__main__':
    run()
