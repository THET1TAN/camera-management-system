import os
import sqlite3
from cryptography.fernet import Fernet
import tkinter as tk
from tkinter import messagebox
import sys
from camera_health import CameraTarget, load_overrides
from camera_health_monitor import HealthMonitor
from child_processes import ChildProcesses, parent_lifetime
from camera_key import load_encryption_key

# Keep the installation key outside the published source files.
ENCRYPTION_KEY = load_encryption_key(os.path.dirname(__file__))

# Database file
DB_FILE = os.path.join(os.path.dirname(__file__), 'camera_credentials.db')

# Initialize encryption
cipher = Fernet(ENCRYPTION_KEY)

def get_current_python():
    """Get the current Python executable path"""
    return sys.executable

def is_encrypted(data):
    try:
        cipher.decrypt(data)
        return True
    except Exception:
        return False

def decrypt_data(encrypted_data):
    try:
        if is_encrypted(encrypted_data):
            return cipher.decrypt(encrypted_data).decode()
        return encrypted_data
    except Exception:
        return encrypted_data

def get_cameras():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT id, ip, username, password, ptz FROM cameras')
    cameras = cursor.fetchall()
    conn.close()
    
    decrypted_cameras = []
    for camera in cameras:
        try:
            ip_data = camera[1].encode() if isinstance(camera[1], str) else camera[1]
            username_data = camera[2].encode() if isinstance(camera[2], str) else camera[2]
            password_data = camera[3].encode() if isinstance(camera[3], str) else camera[3]
            
            decrypted_camera = (
                camera[0],
                decrypt_data(ip_data),
                decrypt_data(username_data),
                password_data,
                camera[4]
            )
            decrypted_cameras.append(decrypted_camera)
        except Exception as e:
            print(f"Error processing camera {camera[0]}: {e}")
            continue
    return decrypted_cameras

class CameraViewer:
    def __init__(self, root):
        self.root = root
        self.children = ChildProcesses(root)
        self.health = None
        self.health_widgets = {}
        self.health_updates = {}
        self.health_after = None
        self.managers = []
        parent_lifetime.bind(root, self.on_closing)
        
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        
        main_frame = tk.Frame(root)
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=1)

        list_frame = tk.Frame(main_frame)
        list_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.camera_list = tk.Text(list_frame, height=10, state=tk.DISABLED,
                                 yscrollcommand=scrollbar.set, 
                                 cursor="arrow")
        self.camera_list.grid(row=0, column=0, sticky="nsew")
        scrollbar.config(command=self.camera_list.yview)

        self.status_detail = tk.Label(main_frame, text='', height=1, anchor='w')
        self.status_detail.grid(row=2, column=0, sticky='ew', padx=8, pady=(0, 5))
        self.load_cameras()
        self.health_after = self.root.after(100, self._poll_health)
        
        # Ajout d'un frame pour le bouton en bas
        button_frame = tk.Frame(main_frame)
        button_frame.grid(row=1, column=0, pady=5)
        button_frame.grid_columnconfigure(0, weight=1)
        
        self.manage_button = tk.Button(button_frame, text="Manage Cameras", 
                                     command=self.open_camera_manager,
                                     cursor="hand2")
        self.manage_button.grid(row=0, column=0)
        
        self.root.bind('<Up>', self.navigate_up)
        self.root.bind('<Down>', self.navigate_down)
        self.root.bind('<Return>', self.activate_button)
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def load_cameras(self):
        if self.health is not None:
            self.health.stdin.close()
            self.health = None
        self.health_widgets.clear()
        self.health_updates.clear()
        for widget in self.camera_list.winfo_children():
            widget.destroy()
        self.camera_list.config(state=tk.NORMAL)
        self.camera_list.delete("1.0", tk.END)
        cameras = get_cameras()
        for camera in cameras:
            self.camera_list.insert(tk.END, f"Camera : {camera[0]} ")
            
            button_frame = tk.Frame(self.camera_list, highlightthickness=0, bd=0, bg=self.camera_list.cget('bg'))
            button_frame.configure(pady=5, padx=5)

            indicator = tk.Canvas(button_frame, width=16, height=16, highlightthickness=0,
                                  bg=self.camera_list.cget('bg'))
            dot = indicator.create_oval(3, 3, 13, 13, fill='#737373', outline='')
            indicator.pack(side=tk.LEFT)
            status = tk.Label(button_frame, text='Checking', width=10, anchor='w',
                              bg=self.camera_list.cget('bg'), takefocus=True)
            status.pack(side=tk.LEFT, padx=(0, 4))
            self.health_widgets[camera[0]] = (indicator, dot, status)
            
            play_button = tk.Button(button_frame, text="Play", 
                                  command=lambda c=camera: self.play_camera_thread(c),
                                  cursor="hand2")
            play_button.pack(side=tk.LEFT, padx=2)
            
            if camera[4] == 1:  # camera[4] = ptz
                ptz_button = tk.Button(button_frame, text="PTZ",
                                       command=lambda c=camera: self.play_ptz_thread(c),
                                       cursor="hand2")
                ptz_button.pack(side=tk.LEFT, padx=2)
            
            self.camera_list.window_create("end", window=button_frame)
            self.camera_list.insert(tk.END, "\n")
            
        self.camera_list.config(state=tk.DISABLED)
        self._refresh_health_summary()
        if cameras:
            try:
                overrides = load_overrides(os.path.dirname(__file__))
                targets = [CameraTarget(c[0], c[1], c[2], cipher.decrypt(c[3]).decode(),
                                        **overrides.get(str(c[0]), {})) for c in cameras]
                self.health = HealthMonitor(targets)
                self.children.processes.append(self.health)
            except Exception:
                # Keep controls usable and never include sensitive exception text.
                self.status_detail.config(text='Availability checks unavailable.')
                for _, _, status in self.health_widgets.values():
                    status.config(text='Unknown')

    def _refresh_health_summary(self):
        counts = {}
        for _, _, label in self.health_widgets.values():
            state = label.cget('text').lower()
            counts[state] = counts.get(state, 0) + 1
        parts = [f'{counts[state]} {state}' for state in
                 ('online', 'degraded', 'unreachable', 'checking', 'unknown') if counts.get(state)]
        self.status_detail.config(text=' · '.join(parts) if parts else 'No cameras configured.')

    def _poll_health(self):
        self.health_after = None
        if self.children.closing:
            return
        # Reap a replaced monitor even when the user has not launched a player.
        self.children._reap()
        if self.health is not None:
            updates = self.health.updates()
            for update in updates:
                if update.camera_id not in self.health_widgets:
                    continue
                canvas, dot, label = self.health_widgets[update.camera_id]
                color, title = {'online': ('#238636', 'Online'),
                                'degraded': ('#b77900', 'Degraded'),
                                'offline': ('#c62828', 'Unreachable'),
                                'unknown': ('#737373', 'Unknown')}[update.state]
                canvas.itemconfigure(dot, fill=color)
                label.config(text=title)
                self.health_updates[update.camera_id] = update
            if updates:
                self._refresh_health_summary()
            if self.health.poll() is not None:
                for canvas, dot, label in self.health_widgets.values():
                    canvas.itemconfigure(dot, fill='#737373')
                    label.config(text='Unknown')
                self.health_updates.clear()
                self.status_detail.config(text='Availability checks stopped. Reopen Viewer.')
        closed_managers = [p for p in self.managers if p.poll() is not None]
        if closed_managers:
            self.managers = [p for p in self.managers if p not in closed_managers]
            self.load_cameras()
        self.health_after = self.root.after(100, self._poll_health)

    def navigate_up(self, event):
        self.camera_list.yview_scroll(-1, "units")

    def navigate_down(self, event):
        self.camera_list.yview_scroll(1, "units")

    def activate_button(self, event):
        focused_widget = self.root.focus_get()
        if isinstance(focused_widget, tk.Button):
            focused_widget.invoke()

    def play_camera_thread(self, camera):
        decrypted_password = cipher.decrypt(camera[3]).decode()
        python_exe = get_current_python()  # Keep the Viewer's dependency environment.
        if not python_exe:
            messagebox.showerror("Error", "Python interpreter unavailable")
            return
            
        self.children.spawn([python_exe,
                                  os.path.join(os.path.dirname(__file__), 'player_vilkin_hikvision.py'),
                                  str(camera[0]),
                                  camera[1],
                                  camera[2],
                                  decrypted_password])

    def play_ptz_thread(self, camera):
        python_exe = get_current_python()
        if not python_exe:
            messagebox.showerror("Error", "Python interpreter unavailable")
            return
        decrypted_password = cipher.decrypt(camera[3]).decode()
        self.children.spawn([python_exe,
                          os.path.join(os.path.dirname(__file__), 'ptz_keyboard_control.py'),
                          str(camera[0]), camera[1], camera[2], decrypted_password])

    def open_camera_manager(self):
        manager_path = os.path.join(os.path.dirname(__file__), 'camera_manager.py')
        process = self.children.spawn([get_current_python(), manager_path])
        if process is not None:
            self.managers.append(process)

    def on_closing(self):
        if self.health_after is not None:
            self.root.after_cancel(self.health_after)
            self.health_after = None
        self.children.close()

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    root = tk.Tk()
    root.title("Camera Viewer - v0.2.8 preview")
    root.geometry("470x290")
    app = CameraViewer(root)
    root.mainloop()
