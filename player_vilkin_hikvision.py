# V0.2.4

import os
from queue import Queue, Empty
import threading
import time
import vlc
import tkinter as tk
from tkinter import Frame
import tkinter.ttk as ttk
from PIL import Image, ImageTk
from onvif import ONVIFCamera
import argparse
from collections import deque

class VideoStream:
    def __init__(self, stream_uri, instance_params=None):
        if instance_params is None:
            instance_params = [
                '--no-video-deco',
                '--no-embedded-video',
                '--rtsp-ttcp',
                '--network-caching=50',
                '--rtsp-frame-buffer-size=500000',
                '--no-skip-frames',
                '--avcodec-threads=2',
                '--file-logging',
                '--logfile=vlc-log.txt',
                '--log-verbose=2'
            ]
        self.stream_uri = stream_uri
        self.instance = vlc.Instance(*instance_params)
        self.player = self.instance.media_player_new()
        self.media = self.instance.media_new(stream_uri)
        self.player.set_media(self.media)
        self.running = False
        self.status_queue = Queue()
        self._last_state = None
        self._frame_count = 0
        self._bitrate_samples = deque(maxlen=5)  # Utilisation de deque pour une moyenne mobile
        self._last_bytes = 0
        self._last_time = time.time()
        self.is_muted = False
        self._no_update_count = 0
        self._last_bitrate_value = 0
        self._restart_attempt = 0  # Compteur de tentatives de redémarrage
        self._max_restart_delay = 30  # Délai maximum entre les tentatives (en secondes)
        
        # Event handler pour les frames
        self.player.event_manager().event_attach(vlc.EventType.MediaPlayerTimeChanged, 
                                               lambda _: self._increment_frame())
    
    def _increment_frame(self):
        self._frame_count += 1

    def start(self):
        self.running = True
        self.player.play()
        self.player.audio_set_mute(self.is_muted)
        threading.Thread(target=self._monitor_stream, daemon=True).start()

    def stop(self):
        self.running = False
        self.player.stop()

    def _monitor_stream(self):
        last_bitrate_ts = time.time()
        zero_bitrate_count = 0  # Compteur pour les bitrates nuls consécutifs
        
        while self.running:
            state = self.player.get_state()
            
            # Vérifie les états critiques (ajout de Ended)
            if state in (vlc.State.Error, vlc.State.Stopped, vlc.State.Ended):
                self.status_queue.put(f"restart-state-{state}")
                self._restart_player()
                zero_bitrate_count = 0  # Reset du compteur après redémarrage
                time.sleep(10)  # Attendre 10 secondes avant de recommencer à vérifier
                continue  # Passe au prochain cycle pour vérifier le nouveau état
            
            bitrate = self.get_bitrate()
            if bitrate == "--" or float(bitrate) == 0.0:
                zero_bitrate_count += 1
                
                if zero_bitrate_count >= 3:  # 3 lectures nulles consécutives
                    self.status_queue.put("restart-zero-bitrate")
                    self._restart_player()
                    zero_bitrate_count = 0
                    time.sleep(10)  # Attendre 10 secondes avant de recommencer à vérifier
            else:
                zero_bitrate_count = 0  # Reset du compteur si on reçoit un bitrate valide
                last_bitrate_ts = time.time()
                self._restart_attempt = 0  # Réinitialise le compteur si tout va bien
            
            self._last_state = state
            time.sleep(2)

    def _restart_player(self):
        self._restart_attempt += 1
        delay = min(5 * self._restart_attempt, self._max_restart_delay)  # Délai progressif
        
        time.sleep(delay)  # Attente progressive
        
        self.player.stop()
        time.sleep(1)  # Petit délai pour laisser le temps à VLC de se réinitialiser
        self.player.set_media(self.media)
        self.player.play()
        time.sleep(2)  # Délai pour laisser le temps au flux de redémarrer

    def get_bitrate(self):
        stats = vlc.MediaStats()
        current_time = time.time()

        # Pas de stats ?
        if not self.player.get_media().get_stats(stats):
            self._no_update_count += 1
            if self._no_update_count <= 3:
                return self._last_bitrate_value
            return "--"

        current_bytes = stats.demux_read_bytes or 0
        time_diff = current_time - self._last_time

        # Calcul d'un nouveau bitrate
        if self._last_bytes > 0 and time_diff >= 1.0:
            bytes_diff = current_bytes - self._last_bytes
            bitrate = (bytes_diff * 8) / (time_diff * 1_000_000)
            self._bitrate_samples.append(bitrate)

            self._last_bitrate_value = max(
                0,
                min(sum(self._bitrate_samples) / len(self._bitrate_samples), 100)
            )

            self._last_bytes = current_bytes
            self._last_time = current_time
            self._no_update_count = 0

            return self._last_bitrate_value

        self._last_bytes = current_bytes
        self._last_time = current_time
        self._no_update_count = 0

        return self._last_bitrate_value

class ButtonStyle:
    def __init__(self, 
                 normal_bg='#C0C0C0',
                 normal_active_bg='#D0D0D0',
                 pressed_bg='#E0E0E0',
                 pressed_active_bg='#F0F0F0'):
        self.normal_bg = normal_bg
        self.normal_active_bg = normal_active_bg
        self.pressed_bg = pressed_bg
        self.pressed_active_bg = pressed_active_bg
        
    def get_normal_style(self):
        return {
            'relief': 'flat',
            'bg': self.normal_bg,
            'activebackground': self.normal_active_bg,
            'bd': 0,
            'highlightthickness': 0
        }
        
    def get_pressed_style(self):
        return {
            'relief': 'sunken',
            'bg': self.pressed_bg,
            'activebackground': self.pressed_active_bg,
            'bd': 0,
            'highlightthickness': 0
        }

class VideoPlayer:
    CONTROL_BAR_HEIGHT = 40

    def __init__(self, camera_id, camera_ip, username, password):
        self.camera_id = camera_id
        self.camera_ip = camera_ip
        self.stream_uri = self._get_stream_uri(camera_ip, username, password)
        self.video_ratio = 0
        self.root = None
        self.frame = None
        self.control_bar = None
        self.bitrate_label = None
        self.mute_button = None
        self.volume_up_icon = None
        self.volume_mute_icon = None
        self.button_style = ButtonStyle()  # Ajout du style de bouton
        self.base_title = f"Camera {camera_id}"  # Changed from "Caméra" to "Camera"
        
        vlc_params = [
            '--no-video-deco', '--no-embedded-video', '--rtsp-tcp',
            '--network-caching=50', '--file-caching=50', '--live-caching=50',
            '--no-skip-frames', '--drop-late-frames',
            '--avcodec-threads=2', '--sout-mux-caching=0'
        ]
        
        self.video_stream = VideoStream(self.stream_uri, vlc_params)
        self.setup_gui()

    def _get_stream_uri(self, camera_ip, username, password):
        camera = ONVIFCamera(camera_ip, 80, username, password)
        media_service = camera.create_media_service()
        media_profile = media_service.GetProfiles()[0]
        
        stream_uri = media_service.GetStreamUri({
            'StreamSetup': {'Stream': 'RTP-Unicast', 'Transport': 'RTSP'},
            'ProfileToken': media_profile.token
        }).Uri

        if "@" not in stream_uri:
            stream_uri = f"rtsp://{username}:{password}@{camera_ip}:554/Streaming/Channels/101"
        
        return stream_uri

    def setup_gui(self):
        self.root = tk.Tk()
        self.root.title(self.base_title)  # Utilisation du titre de base
        self.root.geometry("800x600")
        
        # Frame principal pour la vidéo
        self.frame = Frame(self.root, width=800, height=600)
        self.frame.pack(fill=tk.BOTH, expand=True)
        
        # Barre de contrôle
        self.setup_control_bar()
        
        # Configuration des événements
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.after(500, self.initial_resize)
        self.root.after(1000, self.check_stream_status)
        self.root.after(1000, self.update_bitrate)

    def setup_control_bar(self):
        self.control_bar = Frame(self.root, height=self.CONTROL_BAR_HEIGHT, bg='#2b2b2b')
        self.control_bar.pack(fill=tk.X, side=tk.BOTTOM)
        
        # Conteneurs gauche et droite
        left_container = Frame(self.control_bar, bg='#2b2b2b')
        left_container.pack(side=tk.LEFT, fill=tk.Y)
        
        right_container = Frame(self.control_bar, bg='#2b2b2b')
        right_container.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Configuration du style et des icônes
        self.setup_icons()
        self.setup_controls(left_container, right_container)

    def setup_icons(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.volume_up_icon = ImageTk.PhotoImage(
            Image.open(os.path.join(script_dir, "assets", "icons", "volume-up.png")).resize((16, 16))
        )
        self.volume_mute_icon = ImageTk.PhotoImage(
            Image.open(os.path.join(script_dir, "assets", "icons", "volume-mute.png")).resize((16, 16))
        )

    def setup_controls(self, left_container, right_container):
        self.mute_button = tk.Button(
            left_container,
            image=self.volume_up_icon,
            command=self.toggle_mute,
            width=30,
            height=30,
            **self.button_style.get_normal_style()  # Utilisation du style
        )
        self.mute_button.pack(side=tk.LEFT, padx=5, pady=5)
        
        self.bitrate_label = tk.Label(
            right_container,
            text="-- Mbps",
            fg="white",
            bg='#2b2b2b',
            font=('Arial', 9)
        )
        self.bitrate_label.pack(side=tk.RIGHT, padx=10)

    def toggle_mute(self):
        self.video_stream.is_muted = not self.video_stream.is_muted
        self.video_stream.player.audio_set_mute(self.video_stream.is_muted)
        
        # Changed from "(Muet)" to "(Muted)"
        self.root.title(f"{self.base_title} (Muted)" if self.video_stream.is_muted else self.base_title)
        
        style = self.button_style.get_pressed_style() if self.video_stream.is_muted else self.button_style.get_normal_style()
        self.mute_button.config(
            image=self.volume_mute_icon if self.video_stream.is_muted else self.volume_up_icon,
            **style
        )

    def initial_resize(self):
        width, height = self.video_stream.player.video_get_size()
        if width > 0 and height > 0:
            self.video_ratio = width / height
            window_width = 800
            window_height = int(window_width / self.video_ratio)
            total_height = window_height + self.CONTROL_BAR_HEIGHT
            self.root.geometry(f"{window_width}x{total_height}")
            self.frame.config(width=window_width, height=window_height)
            self.video_stream.player.set_hwnd(self.frame.winfo_id())
            self.root.bind("<Configure>", self.resize)
        else:
            self.root.after(500, self.initial_resize)

    def resize(self, event):
        if event.widget != self.root:
            return
        
        window_width = event.width
        window_height = event.height - self.CONTROL_BAR_HEIGHT
        
        if self.video_ratio == 0:
            return
            
        if window_width / window_height > self.video_ratio:
            new_height = window_height
            new_width = int(window_height * self.video_ratio)
        else:
            new_width = window_width
            new_height = int(window_width / self.video_ratio)
            
        self.frame.config(width=new_width, height=new_height)
        self.video_stream.player.set_hwnd(self.frame.winfo_id())

    def check_stream_status(self):
        try:
            msg = self.video_stream.status_queue.get_nowait()
            if msg == "restart":
                print("Stream frozen, restarting...")  # Changed from "Flux figé, redémarrage..."
        except Empty:
            pass
        finally:
            self.root.after(2000, self.check_stream_status)

    def update_bitrate(self):
        try:
            bitrate = self.video_stream.get_bitrate()
            self.bitrate_label.config(
                text=f"{bitrate:.2f} Mbps" if isinstance(bitrate, (int, float)) and bitrate > 0 else "-- Mbps"
            )
        except Exception:
            self.bitrate_label.config(text="-- Mbps")
        finally:
            self.root.after(1000, self.update_bitrate)

    def on_closing(self):
        self.video_stream.stop()
        self.root.destroy()

    def run(self):
        self.video_stream.player.set_hwnd(self.frame.winfo_id())
        self.video_stream.start()
        self.root.mainloop()

class PlayerVilkinHikvision:
    def __init__(self):
        self.player = vlc.MediaPlayer()

    def get_current_bitrate(self, current_time):
        stats = vlc.MediaStats()
        if not self.player.get_media().get_stats(stats):
            return "--"

def main():
    parser = argparse.ArgumentParser(description='Launch ONVIF camera video stream.')  # Changed from 'Lancer le flux vidéo de la caméra ONVIF.'
    parser.add_argument('camera_id', type=str, help='Camera ID')  # Changed from 'ID de la caméra'
    parser.add_argument('camera_ip', type=str, help='Camera IP address')  # Changed from 'Adresse IP de la caméra'
    parser.add_argument('username', type=str, nargs='?', default=os.getenv('CAMERA_USERNAME'))
    parser.add_argument('password', type=str, nargs='?', default=os.getenv('CAMERA_PASSWORD'))
    args = parser.parse_args()

    player = VideoPlayer(args.camera_id, args.camera_ip, args.username, args.password)
    player.run()

if __name__ == "__main__":
    main()
