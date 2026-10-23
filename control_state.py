"""Process-local pause gate shared by GUI-driven game actions."""
import ctypes
import threading
import time

_pause_requested = threading.Event()


def request_pause():
    _pause_requested.set()


def clear_pause():
    _pause_requested.clear()


def is_paused():
    return _pause_requested.is_set()


def wait_if_paused(stop_event=None):
    """Block before the next action while still allowing Stop/F9 handling."""
    while _pause_requested.is_set():
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError('Stopped from scanner window')
        if hasattr(ctypes, 'windll') and ctypes.windll.user32.GetAsyncKeyState(0x78) & 0x8000:
            raise RuntimeError('Stopped with F9')
        time.sleep(.05)
