import tkinter as tk
from tkinter import ttk
import sys

from ptz_command_worker import ControlState, PTZCommandWorker, select_move_timeout

VERSION = "0.2.6"
REVISION = 3
KEY_POLL_INTERVAL_MS = 30
key_is_down = None
command_worker = None
closing = False
window_active = False
preset_request = None
preset_sequence = 0

speed = 0.5
min_speed = 0.1
max_speed = 1.0
preset_tokens = {str(number): f'PresetToken{number}' for number in range(1, 10)}


class KeyboardManager:
    """Recalcule chaque axe à partir des touches physiques encore maintenues."""

    AXES = (
        {'a': -1, 'left': -1, 'd': 1, 'right': 1},
        {'w': 1, 'up': 1, 's': -1, 'down': -1},
        {'shift': 1, 'shift_l': 1, 'shift_r': 1,
         'ctrl': -1, 'control_l': -1, 'control_r': -1},
        {'q': 1, 'e': -1},
    )

    def __init__(self):
        # L'ordre d'insertion conserve la priorité du dernier appui réel.
        self.pressed_keys = {}

    def press_key(self, key, keycode=None):
        key = key.lower()
        if not any(key in axis for axis in self.AXES):
            return False
        identity = keycode if keycode is not None else key
        if identity in self.pressed_keys:
            return False  # Ignorer la répétition automatique du clavier.
        self.pressed_keys[identity] = key
        return True

    def release_key(self, key, keycode=None):
        # Le code physique reste stable même si Shift/Ctrl change le keysym.
        identity = keycode if keycode is not None else key.lower()
        return self.pressed_keys.pop(identity, None) is not None

    def synchronize(self, is_down):
        """Récupère les relâchements manqués sans activer de nouvelles touches."""
        if is_down is None:
            return
        for keycode in list(self.pressed_keys):
            if isinstance(keycode, int) and not is_down(keycode):
                del self.pressed_keys[keycode]

    def clear(self):
        self.pressed_keys.clear()

    def _axis_value(self, axis):
        for key in reversed(list(self.pressed_keys.values())):
            if key in axis:
                return axis[key]
        return 0

    def get_movement(self):
        return tuple(self._axis_value(axis) for axis in self.AXES[:3])

    def get_focus(self):
        return self._axis_value(self.AXES[3])


def create_key_state_reader():
    """Sous Windows, Tk utilise les codes de touches virtuelles Win32."""
    if sys.platform != 'win32':
        return None
    import ctypes
    get_state = ctypes.WinDLL('user32').GetAsyncKeyState
    get_state.argtypes = [ctypes.c_int]
    get_state.restype = ctypes.c_short
    # Seul le bit de poids fort indique une touche actuellement enfoncée.
    return lambda keycode: bool(get_state(keycode) & 0x8000)


def event_keycode(event):
    if sys.platform == 'win32':
        # Tk peut utiliser le code générique pour les deux côtés.
        modifiers = {'shift_l': 0xA0, 'shift_r': 0xA1,
                     'control_l': 0xA2, 'control_r': 0xA3}
        return modifiers.get(event.keysym.lower(), event.keycode)
    return event.keycode

keyboard = KeyboardManager()


def update_controls():
    """Publish one complete snapshot without doing network I/O in Tk."""
    keyboard.synchronize(key_is_down)
    pan, tilt, zoom = (direction * speed for direction in keyboard.get_movement())
    focus = keyboard.get_focus() * speed
    command_worker.submit(ControlState(pan, tilt, zoom, focus, preset_request))


def update_speed_label():
    speed_value_label.config(text=f"{speed:.1f}")
    speed_progress['value'] = (speed - min_speed) / (max_speed - min_speed) * 100


def on_key_press(event):
    global speed, preset_request, preset_sequence
    if closing or not window_active:
        return
    key = event.keysym.lower()
    if key == 'escape':
        close_controller()
        return
    keycode = event_keycode(event)
    keyboard.synchronize(key_is_down)
    if key_is_down is not None and not key_is_down(keycode):
        update_controls()
        return
    if key in ('m', 'n'):
        speed = round(max(min_speed, min(max_speed, speed + (0.1 if key == 'm' else -0.1))), 1)
        update_speed_label()
    elif key in preset_tokens:
        keyboard.clear()
        preset_sequence += 1
        preset_request = (preset_sequence, preset_tokens[key])
    elif keyboard.press_key(key, keycode):
        preset_request = None
    update_controls()


def on_key_release(event):
    if closing:
        return
    keyboard.release_key(event.keysym, event_keycode(event))
    update_controls()


def poll_keyboard():
    if closing:
        return
    update_controls()
    root.after(KEY_POLL_INTERVAL_MS, poll_keyboard)


def release_all_controls():
    global preset_request
    keyboard.clear()
    preset_request = None
    command_worker.halt()


def close_controller():
    global closing
    if closing:
        return
    closing = True
    keyboard.clear()
    command_worker.close()
    finish_close()


def finish_close():
    if command_worker.is_alive():
        root.after(KEY_POLL_INTERVAL_MS, finish_close)
    else:
        root.quit()


class PTZController:
    def __init__(self, root, camera_id, camera_ip):
        self.root = root
        self.camera_id = camera_id
        self.camera_ip = camera_ip
        self.update_title_status()

        # Bind focus events
        self.root.bind("<FocusIn>", self.on_focus_in)
        self.root.bind("<FocusOut>", self.on_focus_out)

    def update_title_status(self, status=None):
        if status is None:
            status = "In Use" if self.root.focus_get() else "Idle"
        self.root.title(f"PTZ Control v{VERSION} r{REVISION} - Camera {self.camera_id} - {status}")

    def on_focus_in(self, event):
        global window_active
        window_active = True
        self.update_title_status("In Use")

    def on_focus_out(self, event):
        self.root.after_idle(self.check_focus)

    def check_focus(self):
        global window_active
        # Un transfert du focus entre widgets de cette fenêtre reste actif.
        if self.root.focus_get() is None:
            window_active = False
            release_all_controls()
            self.update_title_status("Idle")

def main(argv=None):
    global root, command_worker, speed_value_label, speed_progress, key_is_down
    global closing, window_active, preset_request
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 4:
        print("Usage: ptz_keyboard_control.py camera_id ip username password")
        return 1
    camera_id, camera_ip, username, password = argv
    from onvif import ONVIFCamera
    try:
        camera = ONVIFCamera(camera_ip, 80, username, password)
        media_service = camera.create_media_service()
        ptz_service = camera.create_ptz_service()
        imaging_service = camera.create_imaging_service()
        media_profile = media_service.GetProfiles()[0]
        video_source_token = media_profile.VideoSourceConfiguration.SourceToken
        for service in (ptz_service, imaging_service):
            service.zeep_client.transport.operation_timeout = 1.0
        move_timeout = select_move_timeout(ptz_service, media_profile)
    except Exception as e:
        print(f"Error connecting to camera: {e}")
        return 1

    key_is_down = create_key_state_reader()
    keyboard.clear()
    closing = False
    window_active = False
    preset_request = None
    command_worker = PTZCommandWorker(ptz_service, imaging_service, media_profile.token,
                                      video_source_token, move_timeout=move_timeout)
    root = tk.Tk()
    controller = PTZController(root, camera_id, camera_ip)


    info_label = tk.Label(root, text=(
        "PTZ Camera Control\n"
        "Click on this window to give it focus.\n"
        "Commands only work when this window is active.\n\n"
        "M/N: Increase/Decrease speed\n"
        "Q/E: Focus in/out\nW/S/A/D or Arrows: Pan/Tilt\nShift/Ctrl: Zoom in/out\n"
        "Numbers 1-9: Presets\nEsc: Exit"
    ))
    info_label.pack(padx=20, pady=20)

    # Création du frame pour la vitesse
    speed_frame = tk.Frame(root, bd=2, relief=tk.GROOVE)
    speed_frame.pack(padx=20, pady=10)

    #speed_label = tk.Label(speed_frame, text=f"Vitesse actuelle: {speed:.1f}", font=('Helvetica', 12, 'bold'))
    #speed_label.pack(padx=10, pady=10)

    speed_text_label = tk.Label(speed_frame, text="Current speed:", font=('Helvetica', 12))
    speed_text_label.pack(side=tk.LEFT, padx=10, pady=10)

    speed_value_label = tk.Label(speed_frame, text=f"{speed:.1f}", font=('Helvetica', 12, 'bold'))
    speed_value_label.pack(side=tk.LEFT, padx=10, pady=10)

    # Création de la barre de progression pour la vitesse
    speed_progress = ttk.Progressbar(speed_frame, orient="horizontal", length=200, mode="determinate")
    speed_progress.pack(side=tk.LEFT, padx=10, pady=10)
    speed_progress['value'] = (speed - min_speed) / (max_speed - min_speed) * 100

    root.bind("<KeyPress>", on_key_press)
    root.bind("<KeyRelease>", on_key_release)

    print("Window ready. Click on the window to select it, then use the indicated keys.")
    root.protocol("WM_DELETE_WINDOW", close_controller)
    command_worker.start()
    root.after(KEY_POLL_INTERVAL_MS, poll_keyboard)
    try:
        root.mainloop()
    finally:
        command_worker.close()
        command_worker.join(timeout=8)
        if command_worker.is_alive() or 'not confirmed' in command_worker.status:
            print('Camera stop not confirmed during shutdown')
        root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
