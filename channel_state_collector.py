"""Passive ClassicWorld channel/map state collector.

Uses WinDivert in SNIFF | RECV_ONLY mode. It never forwards, drops, injects,
or records game packets. The only output is a local JSON state file containing
validated channel/map observations.
"""
import argparse
import ctypes
import json
import os
import queue
import re
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

LIMIT = 1024 * 1024
MAP_ENTRY_CODES = (0x1A6, 0x1A9)
CHANNEL_MIN, CHANNEL_MAX = 1, 40
FILTER = 'inbound and ip and tcp and !loopback and tcp.SrcPort >= 8500 and tcp.SrcPort <= 8599'
SNIFF_RECV_ONLY = 1 | 4
ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ClassworldFMBot' / 'channel_state.json'


def _shuffle_table():
    source = (ROOT / 'maple_crypto_constants.cs').read_text(encoding='utf-8-sig')
    values = re.findall(r'0x([0-9A-Fa-f]{2})', source.split('bShuffle =', 1)[1].split('};', 1)[0])
    if len(values) != 256:
        raise RuntimeError('Invalid bundled Maple IV shuffle table')
    return bytes(int(value, 16) for value in values)


SHUFFLE = _shuffle_table()


def advance(iv):
    """Advance the legacy IV. Adapted from MapleLib; see notices in repo."""
    state = bytearray.fromhex('f25350c6')
    for byte in iv:
        prior = state[1]
        state[0] = (state[0] + SHUFFLE[prior] - byte) & 255
        state[1] = (prior - (state[2] ^ SHUFFLE[byte])) & 255
        prior = (state[3] - state[0] + SHUFFLE[byte]) & 255
        state[2] = ((SHUFFLE[state[3]] + byte) ^ state[2]) & 255
        state[3] = prior
        number = int.from_bytes(state, 'little')
        state[:] = (((number << 3) | (number >> 29)) & 0xffffffff).to_bytes(4, 'little')
    return bytes(state)


def frame_header(length, iv):
    if len(iv) != 4 or not 2 <= length <= 65535:
        raise ValueError('Invalid frame header')
    first = int.from_bytes(iv[2:4], 'little') ^ 65534
    return struct.pack('<HH', first, first ^ length)


def inspect_map(plain):
    """Return the verified Free Market location, or None for an unknown layout."""
    if len(plain) < 2 or int.from_bytes(plain[:2], 'little') not in MAP_ENTRY_CODES:
        return None
    channel = int.from_bytes(plain[10:14], 'little') + 1 if len(plain) >= 14 else None
    if channel is None or not CHANNEL_MIN <= channel <= CHANNEL_MAX:
        return None
    # Confirmed layouts from the old collector: full channel entry and short
    # portal transition. Never infer a map id from an arbitrary byte sequence.
    if len(plain) >= 251 and plain[14:24] == bytes.fromhex('00000000000100000000'):
        offset, parser = 247, 'channel_entry_v1'
    elif len(plain) == 103 and plain[14:19] == bytes(5) and plain[32:36] == bytes(4):
        offset, parser = 36, 'portal_entry_v1'
    else:
        return None
    map_id = int.from_bytes(plain[offset:offset + 4], 'little')
    # The packet layout above establishes that this is a server map entry.
    # Do not discard a structurally valid map merely because this installed
    # client uses a different Free Market map-id range than older captures.
    if not 1 <= map_id <= 999999999:
        return None
    location = dict(channel=channel, map_id=map_id, source='server_map_entry', parser=parser,
                    validation='map_entry_layout_and_channel_range')
    if 80002000 <= map_id <= 80002022:
        location['room'] = map_id - 80002000
        location['map_family'] = 'legacy_free_market'
    else:
        location['room'] = None
        location['map_family'] = 'unclassified'
    return location


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.01)


def packet(raw):
    if len(raw) < 40 or raw[0] >> 4 != 4 or raw[9] != 6:
        return None
    ip_length = (raw[0] & 15) * 4
    total = int.from_bytes(raw[2:4], 'big')
    if ip_length < 20 or total > len(raw) or total < ip_length + 20 or int.from_bytes(raw[6:8], 'big') & 0x3fff:
        return None
    tcp = raw[ip_length:total]
    tcp_length = (tcp[12] >> 4) * 4
    if tcp_length < 20 or tcp_length > len(tcp):
        return None
    source_port, destination_port, sequence = struct.unpack_from('!HHI', tcp)
    if not 8500 <= source_port <= 8599:
        return None
    source = '.'.join(map(str, raw[12:16]))
    destination = '.'.join(map(str, raw[16:20]))
    return (source, source_port, destination, destination_port), sequence, tcp[13], tcp[tcp_length:]


class Stream:
    """Bounded TCP reassembly for a single receive-only server flow."""
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
        self.pending.sort(key=lambda part: part[0])
        while self.pending and self.pending[0][0] <= len(result):
            offset, data = self.pending.pop(0)
            result.extend(data[max(0, len(result) - offset):])
        self.next = (self.next + len(result)) & 0xffffffff
        self.pending = [(position - len(result), data) for position, data in self.pending]
        self.gap_since = self.gap_since if self.pending and self.gap_since is not None else (now if self.pending else None)
        return bytes(result)


class Decoder:
    def __init__(self, observe):
        self.buffer = bytearray()
        self.iv = None
        self.observe = observe

    def feed(self, data):
        self.buffer.extend(data)
        if len(self.buffer) > LIMIT:
            raise ValueError('Frame buffer limit')
        if self.iv is None:
            if len(self.buffer) < 21:
                return
            if self.buffer[:11].hex() != '1300010001000000010030':
                raise ValueError('Unsupported channel greeting')
            self.iv = bytes(self.buffer[15:19])
            del self.buffer[:21]
        while len(self.buffer) >= 4:
            header = bytes(self.buffer[:4])
            left, right = struct.unpack('<HH', header)
            length = left ^ right
            if not 2 <= length <= 65535 or header != frame_header(length, self.iv):
                raise ValueError('Frame/IV mismatch; reconnect required')
            if len(self.buffer) < length + 4:
                return
            wire = bytes(self.buffer[4:length + 4])
            del self.buffer[:length + 4]
            plain = bytes((value - self.iv[0]) & 255 for value in wire)
            self.iv = advance(self.iv)
            self.observe(plain)


def game_owner(connection):
    import psutil
    remote, remote_port, local, local_port = connection
    for item in psutil.net_connections(kind='tcp4'):
        if item.pid and item.raddr and tuple(item.laddr) == (local, local_port) and tuple(item.raddr) == (remote, remote_port):
            try:
                return psutil.Process(item.pid).name().lower() == 'maplestory.exe'
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False
    return False


class Sniffer:
    def __init__(self, capture_filter=FILTER):
        if sys.platform != 'win32':
            raise RuntimeError('Windows is required')
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise PermissionError('Run the scanner as administrator so passive packet capture can start')
        root = Path(getattr(sys, '_MEIPASS', ROOT))
        candidates = (
            root / 'windivert' / 'WinDivert.dll',
            root / 'vendor' / 'windivert' / 'WinDivert-2.2.2-A' / 'x64' / 'WinDivert.dll',
            ROOT / 'vendor' / 'windivert' / 'WinDivert-2.2.2-A' / 'x64' / 'WinDivert.dll',
        )
        dll = next((path for path in candidates if path.exists()), None)
        if dll is None:
            raise RuntimeError('WinDivert is missing; run fetch_windivert.ps1 before building')
        self.dll = ctypes.WinDLL(str(dll), use_last_error=True)
        self.dll.WinDivertOpen.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_short, ctypes.c_uint64]
        self.dll.WinDivertOpen.restype = ctypes.c_void_p
        self.dll.WinDivertRecv.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        self.dll.WinDivertRecv.restype = ctypes.c_bool
        self.dll.WinDivertShutdown.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.dll.WinDivertClose.argtypes = [ctypes.c_void_p]
        self.handle = self.dll.WinDivertOpen(capture_filter.encode(), 0, 0, SNIFF_RECV_ONLY)
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def recv(self):
        buffer = ctypes.create_string_buffer(65535)
        address = ctypes.create_string_buffer(80)
        received = ctypes.c_uint()
        if not self.dll.WinDivertRecv(self.handle, buffer, len(buffer), ctypes.byref(received), address):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[:received.value]

    def close(self):
        if self.handle is not None:
            self.dll.WinDivertShutdown(self.handle, 1)
            self.dll.WinDivertClose(self.handle)
            self.handle = None


class ChannelStateCollector:
    def __init__(self, state_path, owner=game_owner):
        self.state_path = Path(state_path)
        self.owner = owner
        self.flows = {}
        self.active_session = None
        self.last_status = 0
        self.packets = 0
        self.handshakes = 0
        self.decoded_frames = 0
        self.map_entry_frames = 0
        self.valid_maps = 0
        self.last_error = None
        self.last_map_layout = None
        self.recent_server_frames = []
        self.write_state(False, 'waiting_for_game_connection')

    def write_state(self, valid, reason, location=None, **extra):
        state = dict(version=1, valid=valid, reason=reason, observed_at=time.time(), **extra)
        if location:
            state.update(location)
            # Retain only derived map observations. Packet bytes are never
            # written. This identifies the installed client's real entrance
            # and room IDs for offline map-resource lookup.
            journal = self.state_path.with_name('channel_map_observations.jsonl')
            with journal.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(observed_at=state['observed_at'], **location), ensure_ascii=False) + '\n')
        atomic_json(self.state_path, state)

    def new_session(self, connection):
        session = uuid.uuid4().hex[:12]
        self.active_session = session
        self.handshakes += 1
        self.write_state(False, 'waiting_for_map_entry', session=session)
        return session

    def observe(self, session, plain):
        self.decoded_frames += 1
        if len(plain) < 2:
            return
        opcode = int.from_bytes(plain[:2], 'little')
        # Bounded structural telemetry only: opcode, frame size and values that
        # look like Maple map IDs. Packet bytes and text are never written.
        candidates = []
        for offset in range(min(320, max(0, len(plain) - 3))):
            value = int.from_bytes(plain[offset:offset + 4], 'little')
            if 100000000 <= value <= 999999999 and value not in candidates:
                candidates.append(value)
                if len(candidates) == 8:
                    break
        self.recent_server_frames.append(dict(opcode=opcode, length=len(plain), map_id_candidates=candidates))
        self.recent_server_frames = self.recent_server_frames[-160:]
        if opcode not in MAP_ENTRY_CODES:
            return
        self.map_entry_frames += 1
        location = inspect_map(plain)
        if location is None:
            self.last_map_layout = dict(length=len(plain), prefix=plain[:24].hex())
            return
        self.valid_maps += 1
        if session != self.active_session:
            return
        self.write_state(True, None, location, session=session)

    def fail(self, flow, reason):
        self.last_error = reason
        flow['failed'] = True
        flow['buffer'].clear()
        flow['stream'].pending.clear()
        if flow['decoder']:
            flow['decoder'].buffer.clear()
        if flow['session'] == self.active_session:
            self.write_state(False, 'capture_error', session=flow['session'], error=reason)

    def check_owner(self, connection, flow, now):
        if flow['decoder'] is None and now >= flow['check']:
            flow['check'] = now + .2
            if self.owner(connection):
                flow['session'] = self.new_session(connection)
                flow['decoder'] = Decoder(lambda plain: self.observe(flow['session'], plain))
                flow['decoder'].feed(flow['buffer'])
                flow['buffer'].clear()

    def receive(self, raw, now=None):
        now = time.monotonic() if now is None else now
        parsed = packet(raw)
        if not parsed:
            return
        self.packets += 1
        connection, sequence, flags, data = parsed
        flow = self.flows.get(connection)
        if flags & 0x12 == 0x12:
            if flow and flow['syn'] == sequence:
                return
            if len(self.flows) >= 64:
                self.flows.pop(next(iter(self.flows)))
            flow = dict(syn=sequence, stream=Stream(sequence + 1), decoder=None, buffer=bytearray(),
                        created=now, last=now, check=0, failed=False, session=None)
            self.flows[connection] = flow
        if not flow:
            return
        flow['last'] = now
        if flags & 4:
            self.flows.pop(connection, None)
            return
        if flow['failed']:
            return
        try:
            ordered = flow['stream'].feed((sequence + bool(flags & 2)) & 0xffffffff, data, now)
            if flow['decoder'] is None:
                flow['buffer'].extend(ordered)
                if len(flow['buffer']) > LIMIT:
                    raise ValueError('Pending owner buffer limit')
                self.check_owner(connection, flow, now)
            else:
                flow['decoder'].feed(ordered)
        except ValueError as error:
            self.fail(flow, str(error))

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        for connection, flow in list(self.flows.items()):
            if not flow['failed']:
                try:
                    self.check_owner(connection, flow, now)
                except Exception as error:
                    self.fail(flow, str(error))
            if flow['decoder'] is None and now - flow['created'] > 3:
                self.flows.pop(connection, None)
            elif now - flow['last'] > 600:
                self.flows.pop(connection, None)
            elif not flow['failed'] and flow['stream'].gap_since is not None and now - flow['stream'].gap_since > 10:
                self.fail(flow, 'TCP gap timeout')
        if now - self.last_status > 2:
            self.last_status = now
            status = self.state_path.with_name('channel_capture_status.json')
            atomic_json(status, dict(version=1, packets=self.packets, connections=len(self.flows),
                                     active_session=self.active_session, observed_at=time.time(),
                                     mode='windivert_sniff_recv_only', handshakes=self.handshakes,
                                     decoded_frames=self.decoded_frames, map_entry_frames=self.map_entry_frames,
                                     valid_maps=self.valid_maps, last_error=self.last_error,
                                     last_map_layout=self.last_map_layout,
                                     recent_server_frames=self.recent_server_frames))


def run(state_path, stop_event=None):
    """Capture passively until ``stop_event`` is set, when one is supplied."""
    collector = ChannelStateCollector(state_path)
    sniffer = Sniffer()
    packets = queue.Queue(maxsize=8192)
    overflow = threading.Event()

    def receive_loop():
        while sniffer.handle is not None:
            try:
                raw = sniffer.recv()
                packets.put_nowait(raw)
            except queue.Full:
                overflow.set()
            except OSError:
                return

    thread = threading.Thread(target=receive_loop, name='channel-capture', daemon=True)
    thread.start()
    try:
        while stop_event is None or not stop_event.is_set():
            if overflow.is_set():
                for flow in collector.flows.values():
                    collector.fail(flow, 'Local capture queue overflow')
                overflow.clear()
            try:
                collector.receive(packets.get(timeout=.25))
            except queue.Empty:
                pass
            collector.tick()
    finally:
        sniffer.close()
        thread.join(timeout=2)
        collector.write_state(False, 'collector_stopped', session=collector.active_session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    run(args.state)


if __name__ == '__main__':
    main()
