import os
import sqlite3
from cryptography.fernet import Fernet
import tkinter as tk
import subprocess
import shutil
import sys

# Encryption key (should be kept secret)
ENCRYPTION_KEY = b'g4ZltE3Vv2Xzq5y6Lq3l4f8Ozt2Ck2Tk6v5b0rN2ghE='

# Database file
DB_FILE = os.path.join(os.path.dirname(__file__), 'camera_credentials.db')

# Initialize encryption
cipher = Fernet(ENCRYPTION_KEY)

def get_python39():
    python_exe = shutil.which("python3.9")
    if not python_exe:
        try:
            python_exe = subprocess.check_output(
                ["py", "-3.9", "-c", "import sys; print(sys.executable)"]
            ).decode().strip()
        except Exception:
            python_exe = sys.executable
    return python_exe

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
        self.processes = []
        
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

        self.load_cameras()
        
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
        self.camera_list.config(state=tk.NORMAL)
        self.camera_list.delete("1.0", tk.END)
        cameras = get_cameras()
        for camera in cameras:
            self.camera_list.insert(tk.END, f"Camera : {camera[0]} ")
            
            button_frame = tk.Frame(self.camera_list, highlightthickness=0, bd=0, bg=self.camera_list.cget('bg'))
            button_frame.configure(pady=5, padx=5)
            
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
        python_exe = get_python39()  # Use Python 3.9 for ONVIF/camera interaction
        if not python_exe:
            messagebox.showerror("Error", "Python 3.9 is required to view cameras")
            return
            
        process = subprocess.Popen([python_exe, 
                                  os.path.join(os.path.dirname(__file__), 'player_vilkin_hikvision.py'),
                                  str(camera[0]),
                                  camera[1],
                                  camera[2],
                                  decrypted_password])
        self.processes.append(process)

    def play_ptz_thread(self, camera):
        python_exe = get_python39()
        if not python_exe:
            messagebox.showerror("Error", "Python 3.9 is required for PTZ")
            return
        decrypted_password = cipher.decrypt(camera[3]).decode()
        subprocess.Popen([python_exe,
                          os.path.join(os.path.dirname(__file__), 'ptz_keyboard_control.py'),
                          str(camera[0]), camera[1], camera[2], decrypted_password])

    def open_camera_manager(self):
        manager_path = os.path.join(os.path.dirname(__file__), 'camera_manager.py')
        subprocess.Popen([get_current_python(), manager_path])  # Use same Python version as current

    def on_closing(self):
        for process in self.processes:
            try:
                process.terminate()
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
            except Exception as e:
                print(f"Error closing process: {e}")
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    root.title("Camera Viewer")
    root.geometry("300x250")
    app = CameraViewer(root)
    root.mainloop()
