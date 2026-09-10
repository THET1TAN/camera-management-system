import tkinter as tk
from tkinter import ttk
import sys

VERSION = "0.2.6"
REVISION = 2
KEY_POLL_INTERVAL_MS = 30
key_is_down = None
requested_motion_var = None
ptz_command_var = None

# Paramètres de vitesse et mapping des presets
speed = 0.5
min_speed = 0.1
max_speed = 1.0
pan_tilt_speed = 1.0
zoom_speed = 1.0

preset_tokens = {
    '1': 'PresetToken1',
    '2': 'PresetToken2',
    '3': 'PresetToken3',
    '4': 'PresetToken4',
    '5': 'PresetToken5',
    '6': 'PresetToken6',
    '7': 'PresetToken7',
    '8': 'PresetToken8',
    '9': 'PresetToken9'
}

# Variables d'état PTZ
current_pan = 0
current_tilt = 0
current_zoom = 0
current_focus = 0


def axis_needs_stop(previous, requested):
    """Arrêter un axe relâché/inversé ou dont l'état est inconnu."""
    return previous is None or (previous != 0 and previous * requested <= 0)


def show_ptz_command(message):
    if ptz_command_var is not None:
        ptz_command_var.set(message)


def start_move(pan, tilt, zoom):
    global current_pan, current_tilt, current_zoom
    if pan == current_pan and tilt == current_tilt and zoom == current_zoom:
        return
    # Certains appareils conservent une composante précédente malgré un zéro.
    # ONVIF arrête Pan/Tilt ensemble : arrêter ce groupe, puis reprendre les
    # directions encore demandées. Ne pas interrompre le zoom sans nécessité.
    stop_pan_tilt = (axis_needs_stop(current_pan, pan)
                     or axis_needs_stop(current_tilt, tilt))
    stop_zoom = axis_needs_stop(current_zoom, zoom)
    if stop_pan_tilt or stop_zoom:
        if not stop_move(pan_tilt=stop_pan_tilt, zoom=stop_zoom):
            return  # Retenter l'arrêt avant toute reprise si la caméra échoue.
    request = ptz_service.create_type('ContinuousMove')
    request.ProfileToken = media_profile.token
    request.Velocity = PTZSpeed()
    request.Velocity.PanTilt = Vector2D(x=pan, y=tilt)
    request.Velocity.Zoom = Vector1D(x=zoom)
    try:
        ptz_service.ContinuousMove(request)
        current_pan, current_tilt, current_zoom = pan, tilt, zoom
        show_ptz_command(f"PTZ request accepted: pan {pan:+.1f}, tilt {tilt:+.1f}, zoom {zoom:+.1f}")
    except Exception as e:
        # La caméra a pu recevoir la commande malgré une réponse perdue.
        # Un état inconnu permet de retenter le mouvement ou son arrêt.
        current_pan = current_tilt = current_zoom = None
        show_ptz_command("PTZ request failed (see console)")
        print(f"ContinuousMove error: {e}")

def stop_move(force=False, pan_tilt=True, zoom=True):
    global current_pan, current_tilt, current_zoom
    if not pan_tilt and not zoom:
        return True
    if not force and (not pan_tilt or (current_pan == 0 and current_tilt == 0)) and (not zoom or current_zoom == 0):
        return True
    try:
        ptz_service.Stop({'ProfileToken': media_profile.token, 'PanTilt': pan_tilt, 'Zoom': zoom})
        if pan_tilt:
            current_pan, current_tilt = 0, 0
        if zoom:
            current_zoom = 0
        groups = 'pan/tilt + zoom' if pan_tilt and zoom else ('pan/tilt' if pan_tilt else 'zoom')
        show_ptz_command(f"PTZ stop accepted: {groups}")
        return True
    except Exception as e:
        if pan_tilt:
            current_pan = current_tilt = None
        if zoom:
            current_zoom = None
        show_ptz_command("PTZ stop failed; retry pending (see console)")
        print(f"Stop error: {e}")
        return False

def start_focus(focus_speed):
    global current_focus
    if focus_speed == current_focus:
        return
    request = imaging_service.create_type('Move')
    request.VideoSourceToken = video_source_token
    request.Focus = {'Continuous': {'Speed': focus_speed}}
    try:
        imaging_service.Move(request)
        current_focus = focus_speed
    except Exception as e:
        current_focus = None
        print(f"Focus move error: {e}")

def stop_focus(force=False):
    global current_focus
    if not force and current_focus == 0:
        return
    request = imaging_service.create_type('Stop')
    request.VideoSourceToken = video_source_token
    try:
        imaging_service.Stop(request)
        current_focus = 0
    except Exception as e:
        current_focus = None
        print(f"Focus stop error: {e}")

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

# Remplacer la variable keys_pressed par une instance de KeyboardManager
keyboard = KeyboardManager()

def update_move():
    pan, tilt, zoom = keyboard.get_movement()
    
    # Applique la vitesse aux mouvements avec une transition plus douce
    pan = pan * speed if pan != 0 else 0
    tilt = tilt * speed if tilt != 0 else 0
    zoom = zoom * speed if zoom != 0 else 0

    if requested_motion_var is not None:
        requested_motion_var.set(f"Keyboard request: pan {pan:+.1f}, tilt {tilt:+.1f}, zoom {zoom:+.1f}")

    # Un arrêt explicite quand toutes les touches PTZ sont relâchées.
    if pan == 0 and tilt == 0 and zoom == 0:
        stop_move()
    else:
        # Le vecteur complet met à zéro les seuls axes relâchés.
        start_move(pan, tilt, zoom)

def update_focus():
    # Obtenir l'état du focus depuis le gestionnaire de clavier
    focus_direction = keyboard.get_focus()
    focus_speed = focus_direction * speed

    if focus_speed != 0:
        start_focus(focus_speed)
    else:
        stop_focus()

def update_speed_label():
    # Cette fonction met à jour l'affichage de la vitesse dans l'interface.
    speed_value_label.config(text=f"{speed:.1f}")
    speed_progress['value'] = (speed - min_speed) / (max_speed - min_speed) * 100

def handle_preset(preset_number):
    print(f"Preset {preset_number} activé")
    # Logique pour activer le preset correspondant

def increase_speed():
    global speed
    if speed < max_speed:
        speed += 0.1
        speed = round(speed, 1)
        speed_value_label.config(text=f"{speed:.1f}")

def decrease_speed():
    global speed
    if speed > min_speed:
        speed -= 0.1
        speed = round(speed, 1)
        speed_value_label.config(text=f"{speed:.1f}")

def on_key_press(event):
    global speed
    key = event.keysym.lower()

    if key == 'escape':
        close_controller()
        return

    keycode = event_keycode(event)
    keyboard.synchronize(key_is_down)
    if key_is_down is not None and not key_is_down(keycode):
        # Ne pas rejouer un ancien appui resté en attente pendant un appel réseau.
        update_move()
        update_focus()
        return

    if key == 'm':
        increase_speed()
        update_speed_label()  # Mettre à jour l'affichage de la vitesse
        print(f"Vitesse augmentée à {speed:.1f}")
        # Appeler update_move() et update_focus() pour prendre en compte la nouvelle vitesse immédiatement
        update_move()
        update_focus()
    elif key == 'n':
        decrease_speed()
        update_speed_label()  # Mettre à jour l'affichage de la vitesse
        print(f"Vitesse diminuée à {speed:.1f}")
        # Appeler update_move() et update_focus() pour prendre en compte la nouvelle vitesse immédiatement
        update_move()
        update_focus()
    elif key in preset_tokens:
        preset_token = preset_tokens[key]
        try:
            ptz_service.GotoPreset({
                'ProfileToken': media_profile.token,
                'PresetToken': preset_token,
                'Speed': {
                    'PanTilt': {'x': pan_tilt_speed, 'y': pan_tilt_speed},
                    'Zoom': {'x': zoom_speed}
                }
            })
            print(f"Aller au preset {key}")
        except Exception as e:
            print(f"Erreur preset {key}: {e}")
    else:
        keyboard.press_key(key, keycode)
        update_move()
        update_focus()

def on_key_release(event):
    keyboard.release_key(event.keysym, event_keycode(event))
    keyboard.synchronize(key_is_down)
    update_move()
    update_focus()


def poll_keyboard():
    keyboard.synchronize(key_is_down)
    update_move()
    update_focus()
    root.after(KEY_POLL_INTERVAL_MS, poll_keyboard)


def release_all_controls(force=False):
    keyboard.clear()
    stop_move(force=force)
    stop_focus(force=force)


def close_controller():
    release_all_controls(force=True)
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
        self.update_title_status("In Use")
    
    def on_focus_out(self, event):
        self.root.after_idle(self.check_focus)

    def check_focus(self):
        # Un transfert du focus entre widgets de cette fenêtre reste actif.
        if self.root.focus_get() is None:
            release_all_controls(force=True)
            self.update_title_status("Idle")

def main(argv=None):
    global root, ptz_service, imaging_service, media_profile, video_source_token
    global PTZSpeed, Vector2D, Vector1D, speed_value_label, speed_progress, key_is_down
    global requested_motion_var, ptz_command_var
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
        get_type = ptz_service.zeep_client.wsdl.types.get_type
        PTZSpeed = get_type('{http://www.onvif.org/ver10/schema}PTZSpeed')
        Vector2D = get_type('{http://www.onvif.org/ver10/schema}Vector2D')
        Vector1D = get_type('{http://www.onvif.org/ver10/schema}Vector1D')
    except Exception as e:
        print(f"Error connecting to camera: {e}")
        return 1

    key_is_down = create_key_state_reader()
    keyboard.clear()
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

    # Distinguer le relâchement détecté au clavier de la réponse ONVIF.
    requested_motion_var = tk.StringVar(root, value="Keyboard request: idle")
    ptz_command_var = tk.StringVar(root, value="PTZ request: idle")
    tk.Label(root, textvariable=requested_motion_var).pack(padx=20)
    tk.Label(root, textvariable=ptz_command_var).pack(padx=20)

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
    root.after(KEY_POLL_INTERVAL_MS, poll_keyboard)
    try:
        root.mainloop()
    finally:
        release_all_controls(force=True)
        root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
