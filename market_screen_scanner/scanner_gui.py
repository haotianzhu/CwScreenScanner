"""GUI launcher for scanning shops visible on the current game screen."""
import ctypes
import os
import queue
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path


def enable_per_monitor_dpi():
    """Keep Tk dimensions in physical pixels without resizing the window."""
    if sys.platform != 'win32':
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except AttributeError:
            pass


enable_per_monitor_dpi()

import tkinter as tk
from tkinter import ttk

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel_state_collector import DEFAULT_STATE, run as run_channel_capture
from position_state_collector import POSITION_STATE, run as run_position_capture
from map_load_probe import run as run_map_load_probe
from market_flow_test import run as run_flow_test
from full_market_scan import run as run_full_market_scan
from control_state import clear_pause, is_paused, request_pause
from market_keyboard import send_key


APP_NAME = '自由市场摊位扫描器'
RUNS = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ClassworldFMBot' / 'screen_scanner'


class ScannerWindow:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        # The user may resize this window freely.  Geometry is deliberately
        # never reset when scanning starts, focus changes, or scanning ends.
        self.root.minsize(620, 410)
        self.root.resizable(True, True)
        self.root.attributes('-topmost', True)
        self.messages = queue.Queue()
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.worker = None
        self.run_folder = None
        self.active_hwnd = None
        self.windows = {}
        self.window_choice = tk.StringVar()
        self.mode = tk.StringVar(value='scan')
        self.status = tk.StringVar(value='准备就绪。选择游戏窗口后开始扫描，或启用地图进出洞测试。')
        self.channel_value = tk.StringVar(value='读取中')
        self.visited_value = tk.StringVar(value='0')
        self.point_value = tk.StringVar(value='--')
        self._configure_style()
        self._build()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Configure>', self._on_resize)
        self.root.after(100, self._poll)

    def _configure_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('Header.TLabel', font=('Microsoft YaHei UI', 17, 'bold'), foreground='#17365d')
        style.configure('Subtle.TLabel', foreground='#52667d')
        style.configure('MetricName.TLabel', font=('Microsoft YaHei UI', 9), foreground='#52667d')
        style.configure('MetricValue.TLabel', font=('Microsoft YaHei UI', 13, 'bold'), foreground='#17365d')
        style.configure('Status.TLabel', font=('Microsoft YaHei UI', 10), foreground='#1f3349')
        style.configure('Hint.TLabel', foreground='#52667d')
        style.configure('Accent.TButton', font=('Microsoft YaHei UI', 10, 'bold'))

    def _build(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky='ew')
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style='Header.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(header, text='普通扫描会从 FM 入口完整扫描 1–20 号洞，并最终回到 1 号洞 CH.1。', style='Subtle.TLabel').grid(row=1, column=0, sticky='w', pady=(3, 0))

        source = ttk.LabelFrame(outer, text=' 游戏窗口 ', padding=(12, 10))
        source.grid(row=1, column=0, sticky='ew', pady=(16, 10))
        source.columnconfigure(0, weight=1)
        self.window_box = ttk.Combobox(source, textvariable=self.window_choice, state='readonly')
        self.window_box.grid(row=0, column=0, sticky='ew')
        self.refresh_button = ttk.Button(source, text='刷新列表', command=self.refresh_windows)
        self.refresh_button.grid(row=0, column=1, padx=(10, 0))
        modes = ttk.Frame(source)
        modes.grid(row=1, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Radiobutton(modes, text='普通扫描', variable=self.mode, value='scan', command=self._refresh_start_label).pack(side='left')
        ttk.Radiobutton(modes, text='地图信息测试', variable=self.mode, value='probe', command=self._refresh_start_label).pack(side='left', padx=(14, 0))
        ttk.Radiobutton(modes, text='流程测试（FM → 1号洞）', variable=self.mode, value='flow', command=self._refresh_start_label).pack(side='left', padx=(14, 0))

        metrics = ttk.LabelFrame(outer, text=' 扫描进度 ', padding=(12, 10))
        metrics.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        for column in range(3):
            metrics.columnconfigure(column, weight=1)
        self._metric(metrics, 0, '当前频道', self.channel_value)
        self._metric(metrics, 1, '已点击店铺', self.visited_value)
        self._metric(metrics, 2, '当前点击点', self.point_value)

        status_box = ttk.LabelFrame(outer, text=' 当前状态 ', padding=(12, 10))
        status_box.grid(row=3, column=0, sticky='nsew', pady=(0, 12))
        status_box.columnconfigure(0, weight=1)
        status_box.rowconfigure(0, weight=1)
        self.status_label = ttk.Label(status_box, textvariable=self.status, style='Status.TLabel', justify='left', anchor='nw')
        self.status_label.grid(row=0, column=0, sticky='nsew')

        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, sticky='ew')
        self.start_button = ttk.Button(actions, text='开始扫描', style='Accent.TButton', command=self.start)
        self.start_button.pack(side='left')
        self.pause_button = ttk.Button(actions, text='暂停', command=self.toggle_pause, state='disabled')
        self.pause_button.pack(side='left', padx=(10, 0))
        self.stop_button = ttk.Button(actions, text='停止', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=(10, 0))
        ttk.Label(actions, text='F9 或鼠标移到左上角可紧急停止。', style='Hint.TLabel').pack(side='right')
        self.refresh_windows()

    def _refresh_start_label(self):
        if not self.worker or not self.worker.is_alive():
            self.start_button.configure(text={'scan': '开始扫描', 'probe': '开始地图测试', 'flow': '开始流程测试'}[self.mode.get()])

    @staticmethod
    def _metric(parent, column, name, variable):
        cell = ttk.Frame(parent)
        cell.grid(row=0, column=column, sticky='ew', padx=(0 if column == 0 else 16, 0))
        ttk.Label(cell, text=name, style='MetricName.TLabel').pack(anchor='w')
        ttk.Label(cell, textvariable=variable, style='MetricValue.TLabel').pack(anchor='w', pady=(2, 0))

    def _on_resize(self, event):
        if event.widget is self.root:
            self.status_label.configure(wraplength=max(360, event.width - 80))

    def refresh_windows(self):
        previous = self.window_choice.get()
        self.windows = self._visible_windows()
        choices = list(self.windows)
        self.window_box.configure(values=choices)
        if previous in self.windows:
            self.window_choice.set(previous)
        else:
            preferred = next((label for label in choices if 'maplestory' in label.lower()), '')
            self.window_choice.set(preferred)
        if not choices:
            self.status.set('没有找到可选窗口。请先打开游戏，然后点击“刷新列表”。')

    @staticmethod
    def _visible_windows():
        user = ctypes.windll.user32
        user.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user.IsWindowVisible.restype = ctypes.c_bool
        user.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user.GetWindowTextLengthW.restype = ctypes.c_int
        user.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        user.GetWindowThreadProcessId.restype = ctypes.c_ulong
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_ssize_t)
        user.EnumWindows.argtypes = [callback_type, ctypes.c_ssize_t]
        user.EnumWindows.restype = ctypes.c_bool
        found = {}

        def visit(hwnd, _):
            if not user.IsWindowVisible(hwnd):
                return True
            length = user.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user.GetWindowTextW(hwnd, title, len(title))
            pid = ctypes.c_ulong()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found[f'{title.value} (PID {pid.value})'] = int(hwnd)
            return True

        user.EnumWindows(callback_type(visit), 0)
        return found

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        label = self.window_choice.get()
        hwnd = self.windows.get(label)
        if not hwnd:
            self.status.set('请先选择游戏窗口；列表中没有时请打开游戏后刷新。')
            return
        # A new Start begins a new investigation.  Pause/Resume never reaches
        # this path, so it keeps the active run's evidence intact.
        if RUNS.exists():
            for child in RUNS.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file():
                    try:
                        child.unlink()
                    except OSError:
                        pass
        self.start_event.clear()
        self.stop_event.clear()
        self.active_hwnd = hwnd
        clear_pause()
        self.start_button.configure(state='disabled')
        self.window_box.configure(state='disabled')
        self.refresh_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.pause_button.configure(state='normal', text='暂停')
        mode = self.mode.get()
        messages = {'scan': f'正在准备“{label}”。会识别当前洞并扫描至 20 号洞。', 'probe': '正在准备地图测试。请手动切一次频道，然后进出 1 号洞；程序只读取本次的地图加载和起点/终点坐标。', 'flow': '正在准备流程测试：会自动进入 1 号洞、各扫描一间 CH.40/CH.1 摊位、离洞后进入 2 号洞。'}
        self.status.set(messages[mode])
        self.channel_value.set('读取中')
        self.visited_value.set('0')
        self.point_value.set('--')
        self._focus_game(hwnd)
        for delay in (100, 350, 750):
            self.root.after(delay, lambda hwnd=hwnd: self._focus_game(hwnd))
        self.start_event.set()
        self.worker = threading.Thread(target=self._run, args=(hwnd, mode), name='shop-scanner', daemon=True)
        self.worker.start()

    @staticmethod
    def _release_game_keys():
        for key in ('left', 'right', 'up', 'down', 'space'):
            try:
                send_key(key, False)
            except OSError:
                pass

    def toggle_pause(self):
        if is_paused():
            self._focus_game(self.active_hwnd)
            clear_pause()
            self.pause_button.configure(text='暂停')
            self.status.set('已恢复；从当前洞、频道和店铺进度继续。')
            return
        request_pause()
        self._release_game_keys()
        self.pause_button.configure(text='继续')
        self.status.set('已暂停；当前洞、频道和店铺进度会保留。')

    def stop(self):
        clear_pause()
        self._release_game_keys()
        self.stop_event.set()
        self.pause_button.configure(state='disabled', text='暂停')
        self.status.set('正在停止；将释放按键并保存本次记录。')

    def _run(self, hwnd, mode='scan'):
        old_stdout, old_stderr = sys.stdout, sys.stderr
        capture_thread = None
        position_thread = None
        try:
            RUNS.mkdir(parents=True, exist_ok=True)
            self.run_folder = RUNS / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            self.run_folder.mkdir(parents=True, exist_ok=False)
            log_path = self.run_folder / 'scanner.log'
            with log_path.open('a', encoding='utf-8') as log:
                sys.stdout, sys.stderr = log, log

                def capture():
                    try:
                        run_channel_capture(DEFAULT_STATE, self.stop_event)
                    except Exception as error:
                        print(f'Passive channel capture unavailable: {error!r}', flush=True)

                capture_thread = threading.Thread(target=capture, name='channel-capture', daemon=True)
                capture_thread.start()

                def capture_position():
                    try:
                        run_position_capture(POSITION_STATE, self.stop_event)
                    except Exception as error:
                        print(f'Passive position capture unavailable: {error!r}', flush=True)

                position_thread = threading.Thread(target=capture_position, name='position-capture', daemon=True)
                position_thread.start()
                if mode == 'probe':
                    outcome = run_map_load_probe(self.stop_event, self.messages.put)
                elif mode == 'flow':
                    outcome = run_flow_test(self.run_folder, hwnd, self.start_event, self.stop_event, self.messages.put)
                else:
                    outcome = run_full_market_scan(self.run_folder, hwnd, self.start_event, self.stop_event, self.messages.put)
            if mode == 'probe':
                if outcome.get('reason') == 'room_round_trip_observed':
                    room = outcome.get('room', {})
                    message = ('地图测试完成：洞内起点 '
                               f'{room.get("start_position")}，出口前终点 {room.get("exit_position")}；'
                               f'地图 epoch {room.get("map_epoch")}。请查看本次 scanner.log。')
                else:
                    message = '地图测试已取消；没有保存路线档案。'
            elif mode == 'flow':
                message = '流程测试完成：已完成 1 号洞的 CH.40/CH.1 各一间店铺，离洞后已进入 2 号洞。'
            elif outcome and outcome.get('reason') == 'cancelled':
                message = '扫描已取消。'
            elif outcome and outcome.get('reason') == 'room_scan_incomplete':
                failed = outcome.get('room', '?')
                message = f'{failed} 号洞未完成频道扫描，流程已停止；请查看 full_scan.jsonl 和 channels.jsonl。'
            else:
                visited = outcome.get('visited', 0) if outcome else 0
                cycles = outcome.get('completed_cycles', 0) if outcome else 0
                message = f'完整扫描已停止：完成 {cycles} 轮，访问 {visited} 家摊位。'
            self.messages.put(('done', message))
        except Exception as error:
            if self.run_folder:
                (self.run_folder / 'error.txt').write_text(f'{type(error).__name__}: {error}\n\n{traceback.format_exc()}', encoding='utf-8')
            self.messages.put(('done', f'扫描已停止：{error}'))
        finally:
            self.stop_event.set()
            if capture_thread:
                capture_thread.join(timeout=2)
            if position_thread:
                position_thread.join(timeout=2)
            sys.stdout, sys.stderr = old_stdout, old_stderr

    @staticmethod
    def _focus_game(hwnd):
        user = ctypes.windll.user32
        user.IsIconic.argtypes = [ctypes.c_void_p]
        user.IsIconic.restype = ctypes.c_bool
        user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.ShowWindow.restype = ctypes.c_bool
        user.BringWindowToTop.argtypes = [ctypes.c_void_p]
        user.BringWindowToTop.restype = ctypes.c_bool
        user.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user.SetForegroundWindow.restype = ctypes.c_bool
        if hwnd:
            if user.IsIconic(hwnd):
                user.ShowWindow(hwnd, 9)
            user.BringWindowToTop(hwnd)
            user.SetForegroundWindow(hwnd)

    def _poll(self):
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            if isinstance(message, tuple) and message[0] == 'progress':
                progress = message[1]
                point = progress.get('point')
                self.channel_value.set(f'CH.{progress["channel"]}')
                self.visited_value.set(str(progress['visited']))
                self.point_value.set('--' if point is None else f'X={point[0]}, Y={point[1]}')
            elif isinstance(message, tuple) and message[0] == 'map_probe_update':
                segments = message[1]
                latest = segments[-1] if segments else {}
                self.visited_value.set(f'{len(segments)}/3')
                point = latest.get('start_position') or latest.get('last_position')
                if point:
                    self.point_value.set(f'X={point.get("x")}, Y={point.get("y")}')
                self.status.set(f'已读取 {len(segments)}/3 次地图加载；当前 epoch {latest.get("map_epoch", "--")}。')
            elif isinstance(message, tuple) and message[0] == 'done':
                self.root.deiconify()
                self.root.lift()
                self.status.set(message[1])
                self.start_button.configure(state='normal')
                self._refresh_start_label()
                self.stop_button.configure(state='disabled')
                self.pause_button.configure(state='disabled', text='暂停')
                self.window_box.configure(state='readonly')
                self.refresh_button.configure(state='normal')
            else:
                labels = {
                    'waiting_for_game_foreground': '等待游戏窗口进入前台；如果未自动切换，请点击游戏画面。',
                    'started': '正在识别并扫描当前频道的可见摊位。',
                    'loading_keyboard_and_screenshot_backend': '正在准备截图与输入控制。',
                    'map_probe_waiting_for_channel_change': '请手动切换到任意频道一次，等待地图加载完成；然后从 FM 入口进入 1 号洞、在洞内走几步，再离开。测试完成后会自动停止。',
                    'flow_preparing_channel_40': '流程测试正在确认 CH.40 并建立本次位置连接。',
                    'route_calibration_connecting': '正在尝试切换到下一个频道以建立本次位置连接。',
                    'flow_entering_room_1': '流程测试正在从 FM 入口进入 1 号洞。',
                    'flow_scanning_channel_1': '流程测试正在扫描 1 号洞 CH.1 的一间摊位。',
                    'flow_scanning_channel_40': '流程测试正在扫描 1 号洞 CH.40 的一间摊位。',
                    'flow_switching_to_channel_1': '流程测试正在切换至 CH.1。',
                    'flow_switching_to_channel_40': '流程测试正在切换至 CH.40。',
                    'flow_resetting_to_exit_level': '流程测试正在下落到 1 号洞出口层。',
                    'flow_exiting_room_1': '流程测试正在走到 out00 并离开 1 号洞。',
                    'flow_entering_room_2': '流程测试正在从 FM 入口进入 2 号洞。',
                    **{f'scanning_channel_{channel}': f'正在扫描 1号洞 CH.{channel}。' for channel in range(1, 41)},
                    **{f'switching_to_channel_{channel}': f'正在切换到 CH.{channel}。' for channel in range(1, 41)},
                }
                if isinstance(message, str) and message.startswith('full_scanning_room_'):
                    _, _, _, room, _, channel = message.split('_')
                    self.status.set(f'正在扫描 {room} 号洞 CH.{channel}。')
                elif isinstance(message, str) and message.startswith('full_switching_room_'):
                    _, _, _, room, _, channel = message.split('_')
                    self.status.set(f'正在切换 {room} 号洞到 CH.{channel}。')
                elif isinstance(message, str) and message.startswith('full_returning_room_'):
                    parts = message.split('_')
                    self.status.set(f'正在为离开 {parts[3]} 号洞尝试 CH.{parts[-1]}。')
                elif isinstance(message, str) and message.startswith('full_entering_room_'):
                    self.status.set(f'正在从 FM 入口进入 {message.rsplit("_", 1)[1]} 号洞。')
                elif isinstance(message, str) and message.startswith('full_exiting_room_'):
                    self.status.set(f'正在离开 {message.rsplit("_", 1)[1]} 号洞。')
                elif isinstance(message, str) and message.startswith('full_resetting_room_'):
                    self.status.set(f'正在复位到 {message.split("_")[3]} 号洞出口层。')
                elif isinstance(message, str) and message.startswith('full_retrying_scan_room_'):
                    parts = message.split('_')
                    self.status.set(f'{parts[4]} 号洞 CH.{parts[6]} 扫描失败，正在第 {parts[-1]} 次重试。')
                elif isinstance(message, str) and message.startswith('full_retrying_channel_room_'):
                    parts = message.split('_')
                    self.status.set(f'{parts[4]} 号洞切换 CH.{parts[6]} 失败，正在第 {parts[-1]} 次重试。')
                elif isinstance(message, str) and message.startswith('full_retrying_return_room_'):
                    parts = message.split('_')
                    self.status.set(f'{parts[4]} 号洞返回 CH.{parts[6]} 失败，正在第 {parts[-1]} 次重试。')
                elif isinstance(message, str) and message.startswith('full_retrying_route_'):
                    parts = message.split('_')
                    self.status.set(f'路线 {"_".join(parts[3:-2])} 失败，正在第 {parts[-1]} 次重试。')
                elif message == 'full_preparing_capture':
                    self.status.set('正在建立本次地图与坐标连接。')
                elif isinstance(message, str) and message.startswith('route_calibration_trying_channel_'):
                    self.status.set(f'正在尝试切换到 CH.{message.rsplit("_", 1)[1]}。')
                elif isinstance(message, str) and message.startswith('route_calibration_retrying_read_channel_'):
                    self.status.set(f'未读到当前频道，正在第 {message.rsplit("_", 1)[1]} 次重试。')
                elif isinstance(message, str) and message.startswith('route_calibration_connection_ready_'):
                    self.status.set(f'已确认 CH.{message.rsplit("_", 1)[1]}；开始完整扫描。')
                elif message == 'full_starting_entrance':
                    self.status.set('已识别 FM 入口；正在进入 1 号洞。')
                elif isinstance(message, str) and message.startswith('full_starting_room_'):
                    self.status.set(f'已识别当前在 {message.rsplit("_", 1)[1]} 号洞；将从这里继续。')
                elif isinstance(message, str) and message.startswith('full_cycle_started_'):
                    self.status.set(f'第 {message.rsplit("_", 1)[1]} 轮扫描开始。')
                elif isinstance(message, str) and message.startswith('full_cycle_complete_'):
                    parts = message.split('_')
                    self.status.set(f'第 {parts[3]} 轮完成；已回到 1 号洞 CH.{parts[-1]}，继续下一轮。')
                else:
                    self.status.set(labels.get(message, f'扫描状态：{message}'))
        self.root.after(100, self._poll)

    def close(self):
        self.stop_event.set()
        self._close_when_stopped()

    def _close_when_stopped(self):
        if self.worker and self.worker.is_alive():
            self.root.after(100, self._close_when_stopped)
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    ScannerWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
