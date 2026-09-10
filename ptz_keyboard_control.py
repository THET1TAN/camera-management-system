import os
import tkinter as tk
from tkinter import ttk
from onvif import ONVIFCamera
import sys
import time

# Get command line arguments
if len(sys.argv) != 5:
    print("Usage: script.py camera_id ip username password")
    sys.exit(1)

camera_id = sys.argv[1]
camera_ip = sys.argv[2]
username = sys.argv[3]
password = sys.argv[4]

# Connexion à la caméra ONVIF
try:
    camera = ONVIFCamera(camera_ip, 80, username, password)
    media_service = camera.create_media_service()
    ptz_service = camera.create_ptz_service()
    imaging_service = camera.create_imaging_service()
except Exception as e:
    print(f"Error connecting to camera: {e}")
    sys.exit(1)

media_profile = media_service.GetProfiles()[0]
video_source_token = media_profile.VideoSourceConfiguration.SourceToken

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

# Chargement des types complexes ONVIF
PTZSpeed = ptz_service.zeep_client.wsdl.types.get_type('{http://www.onvif.org/ver10/schema}PTZSpeed')
Vector2D = ptz_service.zeep_client.wsdl.types.get_type('{http://www.onvif.org/ver10/schema}Vector2D')
Vector1D = ptz_service.zeep_client.wsdl.types.get_type('{http://www.onvif.org/ver10/schema}Vector1D')

def start_move(pan, tilt, zoom):
    global current_pan, current_tilt, current_zoom
    if pan == current_pan and tilt == current_tilt and zoom == current_zoom:
        return
    current_pan, current_tilt, current_zoom = pan, tilt, zoom
    request = ptz_service.create_type('ContinuousMove')
    request.ProfileToken = media_profile.token
    request.Velocity = PTZSpeed()
    request.Velocity.PanTilt = Vector2D(x=pan, y=tilt)
    request.Velocity.Zoom = Vector1D(x=zoom)
    try:
        ptz_service.ContinuousMove(request)
    except Exception as e:
        print(f"ContinuousMove error: {e}")

def stop_move():
    global current_pan, current_tilt, current_zoom
    if current_pan == 0 and current_tilt == 0 and current_zoom == 0:
        return
    current_pan, current_tilt, current_zoom = 0, 0, 0
    try:
        ptz_service.Stop({'ProfileToken': media_profile.token})
    except Exception as e:
        print(f"Stop error: {e}")

def start_focus(focus_speed):
    global current_focus
    if focus_speed == current_focus:
        return
    current_focus = focus_speed
    request = imaging_service.create_type('Move')
    request.VideoSourceToken = video_source_token
    request.Focus = {'Continuous': {'Speed': focus_speed}}
    try:
        imaging_service.Move(request)
    except Exception as e:
        print(f"Focus move error: {e}")

def stop_focus():
    global current_focus
    if current_focus == 0:
        return
    current_focus = 0
    request = imaging_service.create_type('Stop')
    request.VideoSourceToken = video_source_token
    try:
        imaging_service.Stop(request)
    except Exception as e:
        print(f"Focus stop error: {e}")

class KeyboardManager:
    def __init__(self):
        self.keys = {
            'horizontal': {'keys': {'left': ['a', 'left'], 'right': ['d', 'right']}, 'state': {'left': False, 'right': False}, 'last_direction': None},
            'vertical': {'keys': {'up': ['w', 'up'], 'down': ['s', 'down']}, 'state': {'up': False, 'down': False}, 'last_direction': None},
            'zoom': {'keys': {'in': ['shift'], 'out': ['ctrl']}, 'state': {'in': False, 'out': False}, 'last_direction': None},
            'focus': {'keys': {'in': ['q'], 'out': ['e']}, 'state': {'in': False, 'out': False}, 'last_direction': None}  # Ajout du focus
        }
        self.last_command = {'pan': 0, 'tilt': 0, 'zoom': 0, 'focus': 0}
        self.last_update = time.time()

    def _get_direction(self, key, axis):
        for direction, key_list in self.keys[axis]['keys'].items():
            if key in key_list:
                return direction
        return None

    def press_key(self, key):
        for axis in self.keys:
            direction = self._get_direction(key, axis)
            if direction:
                self.keys[axis]['state'][direction] = True
                self.keys[axis]['last_direction'] = direction
                return True
        return False

    def release_key(self, key):
        for axis in self.keys:
            direction = self._get_direction(key, axis)
            if direction:
                self.keys[axis]['state'][direction] = False
                # Si c'était la dernière direction active, réinitialiser last_direction
                if not any(self.keys[axis]['state'].values()):
                    self.keys[axis]['last_direction'] = None
                elif self.keys[axis]['last_direction'] == direction:
                    # Trouver la nouvelle direction active s'il y en a une
                    active_directions = [d for d, state in self.keys[axis]['state'].items() if state]
                    self.keys[axis]['last_direction'] = active_directions[0] if active_directions else None
                # Ajouter une mise à jour de la timestamp pour forcer la réévaluation
                self.last_update = time.time()
                return True
        return False

    def get_movement(self):
        pan = tilt = zoom = 0
        
        # Gestion horizontale (pan)
        if self.keys['horizontal']['last_direction'] == 'left':
            pan = -1
        elif self.keys['horizontal']['last_direction'] == 'right':
            pan = 1

        # Gestion verticale (tilt)
        if self.keys['vertical']['last_direction'] == 'up':
            tilt = 1
        elif self.keys['vertical']['last_direction'] == 'down':
            tilt = -1

        # Gestion zoom
        if self.keys['zoom']['last_direction'] == 'in':
            zoom = 1
        elif self.keys['zoom']['last_direction'] == 'out':
            zoom = -1

        # Mettre à jour la dernière commande si elle est différente
        current_command = {'pan': pan, 'tilt': tilt, 'zoom': zoom}
        if current_command != self.last_command:
            self.last_command = current_command
            self.last_update = time.time()

        return pan, tilt, zoom

    def get_focus(self):
        focus = 0
        if self.keys['focus']['state']['in']:
            focus = 1
        elif self.keys['focus']['state']['out']:
            focus = -1
        return focus

# Remplacer la variable keys_pressed par une instance de KeyboardManager
keyboard = KeyboardManager()

def update_move():
    pan, tilt, zoom = keyboard.get_movement()
    
    # Applique la vitesse aux mouvements avec une transition plus douce
    pan = pan * speed if pan != 0 else 0
    tilt = tilt * speed if tilt != 0 else 0
    zoom = zoom * speed if zoom != 0 else 0

    # N'envoie la commande que si il y a un changement réel
    if pan != current_pan or tilt != current_tilt or zoom != current_zoom:
        start_move(pan, tilt, zoom)
    elif pan == 0 and tilt == 0 and zoom == 0 and (current_pan != 0 or current_tilt != 0 or current_zoom != 0):
        stop_move()

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

    if key in ('control_l', 'control_r'):
        key = 'ctrl'
    if key in ('shift_l', 'shift_r'):
        key = 'shift'

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
    elif key == 'escape':
        stop_move()
        stop_focus()
        root.quit()
    else:
        keyboard.press_key(key)
        update_move()
        update_focus()

def on_key_release(event):
    key = event.keysym.lower()

    if key in ('control_l', 'control_r'):
        key = 'ctrl'
    elif key in ('shift_l', 'shift_r'):
        key = 'shift'

    keyboard.release_key(key)
    update_move()
    update_focus()
    root.update_idletasks()

# Création de la fenêtre Tkinter
root = tk.Tk()

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
        self.root.title(f"PTZ Control - Camera {self.camera_id} - {status}")
    
    def on_focus_in(self, event):
        self.update_title_status("In Use")
    
    def on_focus_out(self, event):
        self.update_title_status("Idle")

controller = PTZController(root, camera_id, camera_ip)

info_label = tk.Label(root, text=(
    "PTZ Camera Control\n"
    "Click on this window to give it focus.\n"
    "Commands only work when this window is active.\n\n"
    "M/N: Increase/Decrease speed\n"
    "Q/E: Focus in/out\nW/S/A/D or Arrows: Pan/Tilt\nCtrl/Shift: Zoom in/out\n"
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
root.mainloop()
