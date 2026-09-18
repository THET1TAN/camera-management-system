"""Responsive Tk window; libVLC and ONVIF live only in a supervised process."""
import argparse
import ctypes
import os
import tkinter as tk

from child_processes import parent_lifetime
from player_supervisor import PlayerSupervisor, PlayerSettings
from player_diagnostics import StackCapture


class VideoPlayer:
    CONTROL_BAR_HEIGHT = 40

    def __init__(self, camera_id, camera_ip='', username='', password='', *, uri=None,
                 settings=None, supervisor_factory=PlayerSupervisor):
        settings = settings or PlayerSettings.from_environment()
        self.root = tk.Tk()
        self.base_title = f'Camera {camera_id}'
        self.root.title(self.base_title)
        self.root.geometry('800x600')
        self.closing = False
        self.is_muted = False
        self.volume = 100
        self._sized = False
        self._timer = None
        self.frame = tk.Frame(self.root, bg='black')
        self.frame.pack(fill=tk.BOTH, expand=True)
        # Keep a sized, mapped HWND throughout its native session. A full-size
        # sibling covers stale video while unavailable without invalidating vout.
        self.video_surface = tk.Frame(self.frame, bg='black')
        self.video_surface.place(x=0, y=0, relwidth=1, relheight=1)
        self.status_label = tk.Label(self.frame, text='Connecting…', bg='black', fg='white', font=('Arial', 14))
        self.status_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.status_label.lift()
        self.control_bar = tk.Frame(self.root, bg='#2b2b2b', height=self.CONTROL_BAR_HEIGHT)
        self.control_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.mute_button = tk.Button(self.control_bar, text='Mute', command=self.toggle_mute)
        self.mute_button.pack(side=tk.LEFT, padx=5, pady=5)
        self.volume_control = tk.Scale(self.control_bar, from_=0, to=100, orient=tk.HORIZONTAL,
            showvalue=False, label='', length=100, command=self.set_volume)
        self.volume_control.set(self.volume)
        self.volume_control.pack(side=tk.LEFT)
        self.bitrate_label = tk.Label(self.control_bar, text='-- Mbps', fg='white', bg='#2b2b2b')
        self.bitrate_label.pack(side=tk.RIGHT, padx=10)
        self.root.update_idletasks()
        # This is the only HWND lookup. It runs on the Tk thread, before the
        # worker exists; resizing never calls back into libVLC.
        hwnd = self.video_surface.winfo_id()
        if os.name == 'nt':
            # libVLC's HWND contract requires WS_CLIPCHILDREN.
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            style = user32.GetWindowLongW(hwnd, -16)
            user32.SetWindowLongW(hwnd, -16, style | 0x02000000)
        self.supervisor = supervisor_factory(camera_id,
            {'host': camera_ip, 'username': username or '', 'password': password or '', 'uri': uri,
             'tk_version': str(self.root.tk.call('info', 'patchlevel'))}, hwnd, settings)
        self.root.protocol('WM_DELETE_WINDOW', self.on_closing)
        self._dumps = StackCapture(camera_id, 'tk')
        self._dumps.arm()
        self._timer = self.root.after(100, self.check_stream_status)

    def toggle_mute(self):
        if self.closing:
            return
        self.is_muted = not self.is_muted
        self.supervisor.set_audio(self.is_muted, self.volume)
        self.mute_button.config(text='Unmute' if self.is_muted else 'Mute')
        self.root.title(self.base_title + (' (Muted)' if self.is_muted else ''))

    def set_volume(self, value):
        self.volume = int(float(value))
        if hasattr(self, 'supervisor') and not self.closing:
            self.supervisor.set_audio(self.is_muted, self.volume)

    def check_stream_status(self):
        self._timer = None
        if self.closing:
            return
        self.supervisor.heartbeat()
        self._dumps.arm()
        snapshot = self.supervisor.snapshot
        live = snapshot.state == 'PLAYING'
        if live:
            if not self._sized and snapshot.width and snapshot.height:
                self._sized = True
                self.root.geometry(f'800x{int(800*snapshot.height/snapshot.width)+self.CONTROL_BAR_HEIGHT}')
            self.status_label.place_forget()
        else:
            text = 'Connecting…' if snapshot.generation <= 1 and snapshot.attempt == 0 else 'Stream unavailable · reconnecting…'
            if snapshot.state == 'FAILED':
                text = 'Player stopped · close and reopen this window'
            self.status_label.config(text=text)
            self.status_label.place(x=0, y=0, relwidth=1, relheight=1)
            self.status_label.lift()
        self.bitrate_label.config(text=f'{snapshot.bitrate:.2f} Mbps' if live else '-- Mbps')
        self._timer = self.root.after(100, self.check_stream_status)

    def on_closing(self):
        if self.closing:
            return
        self.closing = True
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer = None
        self.root.withdraw()
        self.supervisor.close()
        self._finish_close()

    def _finish_close(self):
        # Keep the HWND alive until its native session is reaped. No join on Tk.
        if self.supervisor.closed.is_set():
            self._dumps.close()
            self.root.destroy()
        else:
            self.root.after(50, self._finish_close)

    def run(self):
        self.root.mainloop()


class PlayerArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes unrecognized values, potentially a password.
        super().error('Invalid player arguments (values omitted); use --help for usage.')


def main():
    parser = PlayerArgumentParser(description='Launch an ONVIF camera video stream.')
    parser.add_argument('camera_id')
    parser.add_argument('camera_ip', nargs='?', default='')
    parser.add_argument('username', nargs='?', default=os.getenv('CAMERA_USERNAME', ''))
    parser.add_argument('password', nargs='?', default=os.getenv('CAMERA_PASSWORD', ''))
    parser.add_argument('--test-uri', help='Local synthetic RTSP source (loopback only)')
    args = parser.parse_args()
    if args.test_uri:
        from urllib.parse import urlsplit
        parts = urlsplit(args.test_uri)
        if parts.hostname not in ('127.0.0.1', '::1', 'localhost') or parts.username is not None:
            parser.error('--test-uri requires an unauthenticated loopback source')
    player = VideoPlayer(args.camera_id, args.camera_ip, args.username, args.password, uri=args.test_uri)
    parent_lifetime.bind(player.root, player.on_closing)
    player.run()


if __name__ == '__main__':
    main()
