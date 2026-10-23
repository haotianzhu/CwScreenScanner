"""Visible-screen shop browsing MVP. No packets are sent by this program.

Preview: --image screenshot.png; live run: --run (F8 starts, F9 stops).
Optional recorded routes replay movement and climbing between marked shop rows.
"""
import argparse
import ctypes
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'tools/market_bot_vendor'))
import cv2
import numpy as np
from PIL import Image, ImageDraw

from control_state import wait_if_paused

ASSETS = ROOT/'tools/market_bot_templates'
CLIENT_SIZE = (1920, 1080)


def matches(frame, template, threshold):
    if frame.shape[0] < template.shape[0] or frame.shape[1] < template.shape[1]:
        return []
    scores = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
    out = []
    for _ in range(80):
        _, score, _, pos = cv2.minMaxLoc(scores)
        if score < threshold:
            break
        x, y = pos
        h, w = template.shape[:2]
        out.append((x, y, w, h, float(score)))
        scores[max(0,y-h):y+h+1, max(0,x-w):x+w+1] = -1
    return out


class Vision:
    def __init__(self):
        self.templates = {
            'leave': np.asarray(Image.open(ASSETS / 'leave_store_1920.png').convert('RGB')),
            'owner': np.asarray(Image.open(ASSETS / 'owner.png').convert('RGB')),
        }
        # Native MapleStory controls at the supported 1920x1080 client size.
        # Each template is anchored at its lower-left coin control; box offsets
        # describe the full visible stall relative to that anchor.
        self.stall_templates = (
            ('dialogue', 'coin_dialogue_1920.png', .90, (-3, -53, 154, 28)),
            ('house', 'coin_house_pink_1920.png', .88, (-17, -89, 184, 133)),
            ('house', 'coin_house_yellow_1920.png', .90, (-9, -89, 206, 133)),
            ('house', 'coin_house_beige_1920.png', .90, (-9, -89, 188, 133)),
            ('house', 'coin_house_beige_lower_1920.png', .88, (-12, -88, 210, 140)),
        )
        self.stall_templates = tuple(
            (kind, np.asarray(Image.open(ASSETS / filename).convert('RGB')), threshold, offsets)
            for kind, filename, threshold, offsets in self.stall_templates
        )

    def window_markers(self, image):
        """Return only shop-window markers without scanning market stalls."""
        frame = np.asarray(image.convert('RGB'))
        leave = matches(frame, self.templates['leave'], .90)
        owner = matches(frame, self.templates['owner'], .94)
        return dict(owner=owner, leave=leave)

    def inspect(self, image):
        # Market target recognition is deliberately separate from the fast
        # window-state check used while a shop is closing.
        frame = np.asarray(image.convert('RGB'))
        windows = self.window_markers(image)
        leave = windows['leave']
        owner = windows['owner']
        candidates = []
        if not leave and not owner:
            for kind, template, threshold, offsets in self.stall_templates:
                for x,y,w,h,score in matches(frame, template, threshold):
                    box = (x + offsets[0], y + offsets[1],
                           x + offsets[2], y + offsets[3])
                    left, top, right, bottom = box
                    box = (max(0, left), max(0, top),
                           min(image.width - 1, right), min(image.height - 1, bottom))
                    # Horizontal and bottom clipping makes a target
                    # unreachable.  A top-row sign is different: its title
                    # can be behind the map HUD while the coin control itself
                    # remains fully visible and clickable.  For that case,
                    # target the template's visible coin-strip centre instead
                    # of the unavailable full-box centre.
                    top_clipped = box[1] <= 1
                    if box[0] <= 1 or box[2] >= image.width - 2 or box[3] >= image.height - 2:
                        continue
                    if top_clipped:
                        point = (x + w // 2, y + h // 2)
                    else:
                        point = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
                    if not any(abs(point[0]-c['x'])<25 and abs(point[1]-c['y'])<25 for c in candidates):
                        candidates.append(dict(
                            x=point[0], y=point[1], kind=kind,
                            score=round(score, 4),
                            box=box,
                        ))
        return dict(owner=owner, leave=leave, targets=sorted(candidates, key=lambda c:(c['y']//60,c['x'])))


class StaleShopTarget(RuntimeError):
    """A shop target disappeared or moved before its click was sent."""


def save_preview(image, result, folder):
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    for i, p in enumerate(result['targets'], 1):
        x,y=p['x'],p['y']
        draw.rectangle((x-10,y-8,x+10,y+8),outline='red',width=2)
        draw.text((x+12,y-10), f"{i}:{p.get('kind', 'shop')}", fill='red')
    marked.save(folder/'preview.png')
    (folder/'targets.json').write_text(json.dumps(result,indent=2),encoding='utf-8')


MOVEMENT_KEYS={'left':0x25,'up':0x26,'right':0x27,'down':0x28,'alt':0x12,'space':0x20,'ctrl':0x11}


def validate_route(route):
    if route.get('version')!=1 or not isinstance(route.get('steps'),list):
        raise ValueError('Invalid route format')
    steps=route['steps']
    if not 1<=len(steps)<=1000:raise ValueError('Route must contain 1..1000 steps')
    total=0
    if not any(s.get('action')=='scan' for s in steps):raise ValueError('Route needs a scan checkpoint')
    for step in steps:
        if step.get('action')=='scan':
            if not isinstance(step.get('y'),int) or not 35<=step['y']<=2000:raise ValueError('Invalid scan row')
        elif step.get('action')=='move':
            keys=step.get('keys');duration=step.get('seconds')
            if not isinstance(keys,list) or not keys or any(k not in MOVEMENT_KEYS for k in keys) or len(set(keys))!=len(keys):raise ValueError('Invalid movement keys')
            if ('left' in keys and 'right' in keys) or ('up' in keys and 'down' in keys):raise ValueError('Opposing movement keys')
            if not isinstance(duration,(float,int)) or not 0<duration<=10:raise ValueError('Invalid movement duration')
            total+=duration
        elif step.get('action')=='wait':
            duration=step.get('seconds')
            if not isinstance(duration,(float,int)) or not 0<duration<=10:raise ValueError('Invalid wait duration')
            total+=duration
        else:raise ValueError('Unknown route action')
    if total>600:raise ValueError('Route movement exceeds ten minutes')
    size=route.get('client_size')
    if not isinstance(size,list) or len(size)!=2 or any(not isinstance(v,int) or v<1 for v in size):raise ValueError('Missing client size')
    return route


def run_live(vision, folder, limit, route=None, record_path=None, navigation=None, calibrate_resource=None, calibration_output=None, navigation_test=False, position_path=None, capture_folder=None, start_event=None, stop_event=None, status_callback=None, target_hwnd=None, visit_callback=None, max_open_attempts=None):
    def window_id(handle):
        """Normalize ctypes HWND values before comparing them."""
        if isinstance(handle, int):
            return handle
        return int(getattr(handle, 'value', 0) or 0)

    def startup(stage):
        (folder/'startup.json').write_text(json.dumps(dict(stage=stage,time=time.time())),encoding='utf-8')
        if status_callback:status_callback(stage)
    startup('loading_keyboard_and_screenshot_backend')
    import pyautogui as gui
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.FindWindowW.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR]
    user.FindWindowW.restype=wintypes.HWND
    user.GetForegroundWindow.restype=wintypes.HWND
    user.GetClientRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user.ClientToScreen.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.POINT)]
    user.IsWindow.argtypes=[wintypes.HWND]
    user.IsWindow.restype=wintypes.BOOL
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    hwnd=window_id(target_hwnd) if target_hwnd else window_id(user.FindWindowW(None,'MapleStory'))
    if not hwnd or not user.IsWindow(hwnd):
        raise RuntimeError('Selected game window is no longer available; refresh the window list and try again')

    def game_is_foreground():
        return window_id(user.GetForegroundWindow()) == hwnd
    gui.FAILSAFE=True
    gui.PAUSE=.08
    if calibrate_resource or navigation_test:
        startup('waiting_for_game_foreground')
        print('Switch to MapleStory. Starting after 3 seconds in foreground. F9 cancels.',flush=True)
        focused_since=None
        deadline=time.monotonic()+120
        while True:
            if user.GetAsyncKeyState(0x78)&0x8000:return
            now=time.monotonic()
            if now>deadline:raise RuntimeError('Game was not focused within 120 seconds; restart calibration')
            if game_is_foreground():
                if focused_since is None:
                    focused_since=now;startup('foreground_countdown')
                    print('Game focused: capturing in 3 seconds...',flush=True)
                if now-focused_since>=3:break
            else:focused_since=None
            time.sleep(.05)
    elif start_event is None:
        startup('waiting_for_f8')
        print('Focus MapleStory, then press F8 to start. F9 or mouse top-left stops.',flush=True)
        while not user.GetAsyncKeyState(0x77)&0x8000:
            if user.GetAsyncKeyState(0x78)&0x8000:
                return
            time.sleep(.05)
    else:
        startup('waiting_for_gui_start')
        if stop_event and stop_event.is_set():return dict(visited=0,targets_seen=False,reason='cancelled_before_start')
        startup('waiting_for_game_foreground')
        focused_since=None
        deadline=time.monotonic()+120
        while True:
            if stop_event and stop_event.is_set():
                return dict(visited=0,targets_seen=False,reason='cancelled_before_start')
            if user.GetAsyncKeyState(0x78)&0x8000:
                return dict(visited=0,targets_seen=False,reason='cancelled_before_start')
            now=time.monotonic()
            if now>deadline:raise RuntimeError('Game did not become foreground within 120 seconds')
            if game_is_foreground():
                if focused_since is None:focused_since=now
                if now-focused_since>=.4:break
            else:focused_since=None
            time.sleep(.05)
    startup('started')

    # Fast but variable timing: the scanner targets a roughly two-minute
    # room cycle, so it avoids long idle and movement-only intervals.
    _, tempo = random.choice((('quick', .48), ('ordinary', .56), ('steady', .66)))

    def human_delay(low, high, mode=None, long_chance=0, long_max=0):
        mode = mode if mode is not None else low + (high - low) * .38
        value = random.triangular(low, high, mode) * tempo
        if long_chance and random.random() < long_chance:
            value += random.triangular(.28, long_max or max(.6, high * 4), .65)
        return value

    def check():
        wait_if_paused(stop_event)
        if stop_event and stop_event.is_set():
            raise RuntimeError('Stopped from scanner window')
        if user.GetAsyncKeyState(0x78)&0x8000:
            raise RuntimeError('Stopped with F9')
        if not game_is_foreground():
            raise RuntimeError('Game lost foreground; stopped')
        if gui.position().x==0 and gui.position().y==0:
            raise RuntimeError('Mouse failsafe')

    def pause(seconds):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            check();time.sleep(.03)

    def capture():
        check()
        rect=wintypes.RECT();origin=wintypes.POINT(0,0)
        if not user.GetClientRect(hwnd,ctypes.byref(rect)) or not user.ClientToScreen(hwnd,ctypes.byref(origin)):
            raise RuntimeError('Cannot locate game client')
        if rect.right<600 or rect.bottom<450:
            raise RuntimeError('Game client is minimized or too small')
        if (rect.right, rect.bottom) != CLIENT_SIZE:
            raise RuntimeError(
                f'Game client must be {CLIENT_SIZE[0]}x{CLIENT_SIZE[1]}; '
                f'current size is {rect.right}x{rect.bottom}'
            )
        return gui.screenshot(region=(origin.x,origin.y,rect.right,rect.bottom)), (origin.x,origin.y,rect.right,rect.bottom)

    def human_mouse_move(x, y):
        """Take a varied path with an occasional approach correction."""
        start = gui.position()
        dx, dy = x - start.x, y - start.y
        distance = (dx * dx + dy * dy) ** .5
        tweens = (gui.easeInQuad, gui.easeOutQuad, gui.easeInOutQuad)
        if distance > 220 and random.random() < .22:
            fraction = random.uniform(.35, .65)
            bend = random.uniform(-.06, .06)
            middle_x = round(start.x + dx * fraction - dy * bend)
            middle_y = round(start.y + dy * fraction + dx * bend)
            gui.moveTo(middle_x, middle_y, duration=human_delay(.035, .13, .06), tween=random.choice(tweens))
        gui.moveTo(x, y, duration=human_delay(.055, .25, .10), tween=random.choice(tweens))

    def click(x,y,bounds,action):
        check()
        fresh,current=capture()
        if current!=bounds:
            raise RuntimeError('Window moved or resized; stopped')
        state=vision.inspect(fresh)
        if state['owner']:
            raise RuntimeError('Own shop appeared; stopped')
        if action=='open':
            valid=not state['leave'] and any(abs(x-p['x'])<4 and abs(y-p['y'])<4 for p in state['targets'])
        else:
            valid=len(state['leave'])==1 and abs(x-(state['leave'][0][0]+state['leave'][0][2]//2))<4 and abs(y-(state['leave'][0][1]+state['leave'][0][3]//2))<4
        if not valid:
            raise StaleShopTarget('Target changed before click')
        pause(human_delay(.015, .075, .03))
        human_mouse_move(bounds[0]+x+random.randint(-3,3), bounds[1]+y+random.randint(-2,2))
        pause(human_delay(.012, .065, .025))
        check()
        if action=='open':
            # Preserve a normal double-click while varying the inter-click
            # pause and occasionally making a tiny hand correction.
            gui.click()
            pause(human_delay(.045, .13, .07))
            gui.click()
            pause(human_delay(.045, .16, .075))
        else:
            gui.click()
            pause(human_delay(.04, .14, .065))

    def log(**event):
        with (folder/'visits.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(dict(time=time.time(),**event))+'\n')

    def release(keys):
        # Key-up must still execute after a mouse-corner failsafe or focus loss.
        previous=gui.FAILSAFE
        try:
            gui.FAILSAFE=False
            from market_keyboard import send_key
            for key in keys:send_key(key,False)
        finally:gui.FAILSAFE=previous

    def between_shop_actions():
        """Keep a sparse, short variation between shops without wandering."""
        if random.random() >= .16:
            return []
        from market_keyboard import send_key
        side = random.choice(('left', 'right'))
        duration = human_delay(.025, .10, .045)
        check(); send_key(side, True)
        try:
            pause(duration)
        finally:
            send_key(side, False)
        pause(human_delay(.015, .06, .025))
        return [dict(kind='movement', key=side, jumps=0, seconds=round(duration, 3))]

    def pulse(keys,seconds):
        previous_pause=gui.PAUSE
        try:
            gui.PAUSE=0
            from market_keyboard import send_key
            jump=next((key for key in keys if key in ('space','alt','ctrl')),None)
            if 'up' in keys and jump:
                check();send_key('up',True)
                pause(.08)
                check();send_key(jump,True)
                pause(.12)
                send_key(jump,False)
                pause(max(seconds,.35))
            else:
                for key in keys:check();send_key(key,True)
                pause(seconds)
        finally:
            release(list(reversed(keys)))
            gui.PAUSE=previous_pause

    if calibrate_resource:
        from market_navigation_live import calibrate
        startup('capturing_calibration')
        shot,_=capture()
        state=vision.inspect(shot)
        if state['owner'] or state['leave']:raise RuntimeError('Close the shop before calibration')
        startup('automatic_calibration')
        calibrate(shot,json.loads(calibrate_resource.read_text(encoding='utf-8')),calibration_output)
        startup('calibration_saved')
        print(f'Calibration saved: {calibration_output}',flush=True)
        return

    if record_path:
        start,bounds=capture();start.save(record_path.with_suffix('.start.png'))
        steps=[];active=[];since=time.monotonic();was_mark=False
        print('Recording: move/climb manually. Release movement keys, hover a shop title and press F7 to mark a row. F9 saves. Only arrows and Alt are recorded.',flush=True)
        def append_motion(keys,seconds):
            while seconds>.02:
                chunk=min(seconds,10)
                steps.append(dict(action='move',keys=keys,seconds=round(chunk,3)) if keys else dict(action='wait',seconds=round(chunk,3)))
                seconds-=chunk
        try:
            while True:
                now=time.monotonic()
                if user.GetAsyncKeyState(0x78)&0x8000:
                    append_motion(active,now-since);break
                check()
                keys=[k for k,v in MOVEMENT_KEYS.items() if user.GetAsyncKeyState(v)&0x8000]
                if keys!=active:
                    append_motion(active,now-since);active=keys;since=now
                mark=bool(user.GetAsyncKeyState(0x76)&0x8000)
                if mark and not was_mark:
                    if keys:raise RuntimeError('Release movement keys before F7 checkpoint')
                    shot,current=capture()
                    if current!=bounds:raise RuntimeError('Window changed while recording')
                    state=vision.inspect(shot)
                    if state['owner'] or state['leave']:raise RuntimeError('Close shop before recording a checkpoint')
                    y=int(gui.position().y-bounds[1])
                    if not any(abs(p['y']-y)<=40 for p in state['targets']):raise RuntimeError('Hover a visible shop title before F7')
                    append_motion([],now-since);since=now
                    steps.append(dict(action='scan',y=y));print(f'Checkpoint {sum(x["action"]=="scan" for x in steps)}',flush=True)
                was_mark=mark
                if now-since>10:append_motion(active,now-since);since=now
                if len(steps)>990:raise RuntimeError('Route recording is too long')
                time.sleep(.02)
            recorded=validate_route(dict(version=1,client_size=list(bounds[2:]),steps=steps))
            record_path.write_text(json.dumps(recorded,indent=2),encoding='utf-8')
            print(f'Saved route: {record_path}',flush=True)
        except Exception:
            (folder/'route_incomplete.json').write_text(json.dumps(steps,indent=2),encoding='utf-8');raise
        return

    visited=0
    targets_seen=False
    preview_saved=False
    reason='complete'
    def scan_row(row_y=None,eligible=None,on_visit=None):
      nonlocal visited,targets_seen,preview_saved,reason
      seen=[]
      attempts = max_open_attempts if max_open_attempts is not None else limit - visited
      for index in range(attempts):
        if visited >= limit:
            break
        shot,bounds=capture();state=vision.inspect(shot)
        if not preview_saved:
            save_preview(shot,state,folder)
            preview_saved=True
        targets_seen=targets_seen or bool(state['targets'])
        if state['owner'] or state['leave']:
            reason='window_already_open'
            log(result='scan_skipped_window_already_open',row_y=row_y,
                window='owner' if state['owner'] else 'visitor')
            print('A shop window is already open; leaving it untouched.',flush=True)
            return
        targets=[p for p in state['targets'] if (row_y is None or abs(p['y']-row_y)<=40) and (eligible is None or eligible(p)) and not any(abs(p['x']-x)<25 and abs(p['y']-y)<25 for x,y in seen)]
        if not targets:
            if not targets_seen:reason='no_targets'
            log(result='no_shop_targets_detected' if reason=='no_targets' else 'row_complete',
                row_y=row_y,targets_detected=bool(state['targets']))
            return
        target=targets[0];seen.append((target['x'],target['y']))
        actions = between_shop_actions()
        log(result='between_shop_actions', target=target, actions=actions)
        receipt=None
        if capture_folder:
            from market_scan_receipt import CaptureReceipt
            receipt=CaptureReceipt(capture_folder)
        try:
            click(target['x'],target['y'],bounds,'open')
        except StaleShopTarget as error:
            log(result='shop_skipped_target_changed',target=target,reason=str(error))
            print(f'Skipped shop at ({target["x"]}, {target["y"]}): target changed.',flush=True)
            continue
        deadline=time.monotonic()+1.6
        opened_ok=False
        while True:
            pause(.08);opened,_=capture();state=vision.inspect(opened)
            if state['owner']:
                reason='own_shop_detected'
                log(result='scan_skipped_owner_window_detected',target=target)
                print('Own shop window detected; leaving it untouched.',flush=True)
                return
            if len(state['leave'])>1:
                opened.save(folder/f'ambiguous_open_{visited+1}_{index+1}.png')
                log(result='shop_exit_button_ambiguous',target=target,
                    leave_buttons=len(state['leave']))
                raise RuntimeError('Shop opened, but its exit button is ambiguous; stopped safely')
            if len(state['leave'])==1:
                opened_ok=True
                break
            if time.monotonic()>deadline:
                opened.save(folder/f'skipped_open_{visited+1}_{index+1}.png')
                log(result='shop_skipped_open_unconfirmed',target=target,
                    leave_buttons=len(state['leave']))
                print(f'Skipped shop at ({target["x"]}, {target["y"]}): opening was not confirmed.',flush=True)
                break
        if not opened_ok:continue
        collected=None
        if receipt:
            deadline=time.monotonic()+5
            while collected is None:
                collected=receipt.poll()
                if collected is not None:break
                if time.monotonic()>deadline:
                    raise RuntimeError('Shop UI opened but no complete price capture arrived')
                pause(.1)
            log(result='prices_captured',target=target,receipt=collected)
        pause(human_delay(.18, .62, .32))

        def wait_for_close(seconds):
            deadline=time.monotonic()+seconds
            clear_frames=0
            while True:
                pause(.08);closed,_=capture();current_state=vision.window_markers(closed)
                if not current_state['leave'] and not current_state['owner']:
                    clear_frames += 1
                    if clear_frames >= 2:
                        return True
                else:
                    clear_frames = 0
                if time.monotonic()>deadline:
                    return False

        # Every confirmed visitor shop is closed by its visible LEAVE STORE
        # button.  ESC is reserved for the game/channel menu after closure.
        x,y,w,h,_=state['leave'][0]
        log(result='shop_close_leave_store_clicked',target=target,leave_button=(x,y,w,h))
        try:
            click(x+w//2,y+h//2,bounds,'close')
        except StaleShopTarget as error:
            log(result='shop_close_target_changed',target=target,reason=str(error))
            raise RuntimeError('LEAVE STORE changed before it could be clicked; stopped') from error
        if not wait_for_close(3.0):
            fresh,current=capture();state=vision.window_markers(fresh)
            # A zero-button frame after the timeout means the close succeeded
            # between the last poll and this confirmation frame.
            if current != bounds or state['owner']:
                fresh.save(folder/'stopped.png')
                raise RuntimeError('LEAVE STORE click did not close the shop; stopped')
            if not state['leave']:
                log(result='shop_close_confirmed_after_timeout',target=target)
            elif len(state['leave']) != 1:
                fresh.save(folder/'stopped.png')
                raise RuntimeError('LEAVE STORE button is ambiguous after close attempt')
            else:
                x,y,w,h,_=state['leave'][0]
                log(result='shop_close_leave_store_retry_clicked',target=target,leave_button=(x,y,w,h))
                click(x+w//2,y+h//2,bounds,'close')
                if not wait_for_close(3.0):
                    fresh,_=capture();state=vision.window_markers(fresh)
                    if state['leave'] or state['owner']:
                        fresh.save(folder/'stopped.png')
                        raise RuntimeError('LEAVE STORE retry did not close the shop; stopped')
        with (folder/'visits.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(dict(time=time.time(),target=target,result='opened_and_closed',receipt=collected))+'\n')
        visited+=1
        if on_visit:on_visit(target)
        pause(human_delay(.03, .16, .065))

    if navigation:
        from market_navigation_live import traverse
        try:
            traverse(navigation,capture,vision.inspect,pulse,pause,scan_row,log,lambda:visited>=limit,test_only=navigation_test,position_path=position_path)
        finally:release(MOVEMENT_KEYS)
    elif route:
        initial,bounds=capture()
        if list(bounds[2:])!=route['client_size']:raise RuntimeError('Client size differs from recorded route')
        initial.save(folder/'route_start.png')
        print('Route playback: starting position must match the saved start screenshot.',flush=True)
        try:
            for number,step in enumerate(route['steps']):
                if visited>=limit:break
                check();shot,current=capture();state=vision.inspect(shot)
                if current!=bounds:raise RuntimeError('Window moved or resized during route')
                if state['owner'] or state['leave']:raise RuntimeError('Shop window present before route movement')
                log(result='route_step',step=number,action=step['action'])
                if step['action']=='scan':scan_row(step['y'])
                elif step['action']=='wait':pause(step['seconds'])
                else:
                    try:
                        for key in step['keys']:check();gui.keyDown(key)
                        pause(step['seconds'])
                    finally:
                        release(step['keys'])
                    pause(.15)
        finally:
            release(MOVEMENT_KEYS)
    else:scan_row(on_visit=visit_callback)
    return dict(visited=visited,targets_seen=targets_seen,reason=reason)



def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--image',type=Path,help='Offline screenshot preview; no input actions')
    group.add_argument('--run',action='store_true')
    parser.add_argument('--limit',type=int,default=300)
    parser.add_argument('--navigation-test',action='store_true',help='Walk, climb out/back, then scan; foreground countdown')
    routes=parser.add_mutually_exclusive_group()
    routes.add_argument('--record-route',type=Path)
    routes.add_argument('--route',type=Path)
    routes.add_argument('--navigate-map',type=Path,help='Resource map plus minimap/name calibration')
    parser.add_argument('--position-file',type=Path,help='Use live client movement coordinates for navigation')
    parser.add_argument('--capture-folder',type=Path,help='Require a new completed shop capture before closing each shop')
    routes.add_argument('--calibrate-map',type=Path,help='Extracted WZ map JSON')
    parser.add_argument('--calibration-output',type=Path,default=ROOT/'market_data/maps/room7-navigation.json')
    args=parser.parse_args()
    if not 1<=args.limit<=300:parser.error('limit must be 1..300')
    if args.navigation_test and not args.navigate_map:parser.error('--navigation-test requires --navigate-map')
    if (args.record_route or args.route or args.navigate_map or args.calibrate_map) and not args.run:parser.error('Routes require --run')
    if args.capture_folder and (not args.run or not args.capture_folder.is_dir()):parser.error('--capture-folder requires --run and an existing collector folder')
    route=validate_route(json.loads(args.route.read_text(encoding='utf-8-sig'))) if args.route else None
    if args.record_route:
        args.record_route.parent.mkdir(parents=True,exist_ok=True)
        if args.record_route.exists():parser.error('Route exists; choose a new name to preserve it')
    folder=ROOT/'bot_runs'/time.strftime('%Y%m%d_%H%M%S');folder.mkdir(parents=True,exist_ok=True)
    vision=Vision()
    try:
        if args.image:
            image=Image.open(args.image).convert('RGB');result=vision.inspect(image)
            save_preview(image,result,folder);print(json.dumps(result));print(folder)
        else:run_live(vision,folder,args.limit,route,args.record_route,args.navigate_map,args.calibrate_map,args.calibration_output,args.navigation_test,args.position_file,args.capture_folder)
    except (RuntimeError,KeyboardInterrupt) as error:
        print(f'STOPPED: {error}',flush=True)
        (folder/'stop.txt').write_text(str(error),encoding='utf-8')
        raise SystemExit(1)


if __name__=='__main__':main()
