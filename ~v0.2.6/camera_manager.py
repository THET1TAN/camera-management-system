import os
import sqlite3
from cryptography.fernet import Fernet
import tkinter as tk
from tkinter import simpledialog, messagebox
import threading
import subprocess
import sys
import shutil

# Encryption key (should be kept secret)
ENCRYPTION_KEY = b'g4ZltE3Vv2Xzq5y6Lq3l4f8Ozt2Ck2Tk6v5b0rN2ghE=' # implémenter un system de clé d'utilisateur pour chaque administrateurs et un gui (dans un scripte séparé) pour la gestion des clés
 

# Database file
DB_FILE = os.path.join(os.path.dirname(__file__), 'camera_credentials.db')

# Initialize encryption
cipher = Fernet(ENCRYPTION_KEY)

# Create or connect to the database
def init_db():
    """Initialize database if it doesn't exist"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS cameras
                      (id INTEGER PRIMARY KEY, 
                       ip TEXT, 
                       username TEXT, 
                       password TEXT)''')
    # Add the ptz column if it doesn't exist
    try:
        cursor.execute('ALTER TABLE cameras ADD COLUMN ptz INTEGER DEFAULT 0')
    except:
        pass
    conn.commit()
    conn.close()

def encrypt_data(data):
    return cipher.encrypt(str(data).encode())

def is_encrypted(data):
    """Vérifie si les données sont déjà cryptées"""
    try:
        cipher.decrypt(data)
        return True
    except Exception:
        return False

def decrypt_data(encrypted_data):
    """Décrypte les données si elles sont cryptées"""
    try:
        if is_encrypted(encrypted_data):
            return cipher.decrypt(encrypted_data).decode()
        return encrypted_data
    except Exception:
        return encrypted_data

# Add camera credentials to the database
def add_camera(ip, username, password, ptz=0):
    encrypted_ip = encrypt_data(ip)
    encrypted_username = encrypt_data(username)
    encrypted_password = encrypt_data(password)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO cameras (ip, username, password, ptz) VALUES (?, ?, ?, ?)', 
                  (encrypted_ip, encrypted_username, encrypted_password, ptz))
    conn.commit()
    conn.close()

# Get all cameras from the database
def get_cameras():
    """Get all cameras from database with better error handling"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT id, ip, username, password, ptz FROM cameras')
    cameras = cursor.fetchall()
    conn.close()
    
    decrypted_cameras = []
    for camera in cameras:
        try:
            # Convertir les données en bytes si elles sont stockées en texte
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

def get_current_python():
    """Get the current Python executable path"""
    return sys.executable

def get_python39():
    """Get Python 3.9 executable path (required for ONVIF/camera interaction)"""
    python_exe = shutil.which("python3.9")
    if not python_exe:
        try:
            python_exe = subprocess.check_output(
                ["py", "-3.9", "-c", "import sys; print(sys.executable)"]
            ).decode().strip()
        except Exception:
            python_exe = None
    return python_exe

# Play camera stream
def play_camera(ip, username, password):
    python_exe = get_python39()
    script_path = os.path.join(os.path.dirname(__file__), 'player_vilkin_hikvision.py')
    subprocess.Popen([python_exe, script_path, ip, username, password])

# Delete camera from the database
def delete_camera(camera_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM cameras WHERE id = ?', (camera_id,))
    conn.commit()
    conn.close()

# Update camera credentials in the database
def update_camera(camera_id, ip, username, password, ptz=0):
    encrypted_ip = encrypt_data(ip)
    encrypted_username = encrypt_data(username)
    encrypted_password = encrypt_data(password)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE cameras SET ip = ?, username = ?, password = ?, ptz = ? WHERE id = ?', 
                  (encrypted_ip, encrypted_username, encrypted_password, ptz, camera_id))
    conn.commit()
    conn.close()

def migrate_existing_data():
    """
    Fonction à exécuter une seule fois pour migrer les données existantes
    vers le format crypté
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT id, ip, username, password FROM cameras')
    cameras = cursor.fetchall()
    
    for camera in cameras:
        try:
            # Ne crypter que si les données ne sont pas déjà cryptées
            ip_data = camera[1] if isinstance(camera[1], bytes) else encrypt_data(camera[1])
            username_data = camera[2] if isinstance(camera[2], bytes) else encrypt_data(camera[2])
            password_data = camera[3] if is_encrypted(camera[3]) else encrypt_data(camera[3])
            
            cursor.execute('UPDATE cameras SET ip = ?, username = ?, password = ? WHERE id = ?',
                         (ip_data, username_data, password_data, camera[0]))
        except Exception as e:
            print(f"Migration error for camera {camera[0]}: {e}")
            continue
    
    conn.commit()
    conn.close()

# GUI
class CameraApp:
    def __init__(self, root):
        self.root = root
        self.processes = []  # Liste pour stocker les sous-processus
        
        # Configuration du conteneur principal
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        
        # Créer un frame principal
        main_frame = tk.Frame(root)
        main_frame.grid(row=0, column=0, sticky="nsew")
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=1)

        # Créer un frame pour la liste avec scrollbar
        list_frame = tk.Frame(main_frame)
        list_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        # Créer la scrollbar
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.grid(row=0, column=1, sticky="ns")

        # Configuration de la zone de texte avec scrollbar
        self.camera_list = tk.Text(list_frame, height=10, state=tk.DISABLED,
                                 yscrollcommand=scrollbar.set, 
                                 cursor="arrow")  # Curseur par défaut en flèche
        self.camera_list.grid(row=0, column=0, sticky="nsew")
        scrollbar.config(command=self.camera_list.yview)

        # Bouton Add Camera en bas, centré avec sa taille naturelle
        button_frame = tk.Frame(main_frame)  # Frame conteneur pour le bouton
        button_frame.grid(row=1, column=0, pady=5)
        button_frame.grid_columnconfigure(0, weight=1)  # Centre le bouton horizontalement
        
        self.add_button = tk.Button(button_frame, text="Add Camera", 
                                  command=self.open_add_camera_window)
        self.add_button.grid(row=0, column=0)  # Le bouton garde sa taille naturelle

        # Initialiser la liste des caméras
        self.load_cameras()

        # Configuration des touches de navigation
        self.root.bind('<Up>', self.navigate_up)
        self.root.bind('<Down>', self.navigate_down)
        self.root.bind('<Return>', self.activate_button)
        root.bind("<Up>", self.focus_previous)
        root.bind("<Down>", self.focus_next)

        # Gestionnaire de fermeture
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def load_cameras(self):
        self.camera_list.config(state=tk.NORMAL)
        self.camera_list.delete("1.0", tk.END)
        cameras = get_cameras()
        for camera in cameras:
            self.camera_list.insert(tk.END, f"ID: {camera[0]}, IP: {camera[1]} ")
            
            # Créer un frame invisible pour les boutons de chaque caméra
            button_frame = tk.Frame(self.camera_list, highlightthickness=0, bd=0, bg=self.camera_list.cget('bg'))
            button_frame.configure(pady=5, padx=5)
            
            # Configuration des boutons avec curseur pointer
            play_button = tk.Button(button_frame, text="Play", 
                                  command=lambda c=camera: self.play_camera_thread(c),
                                  cursor="hand2")  # Curseur main au survol
            play_button.pack(side=tk.LEFT, padx=2)
            
            edit_button = tk.Button(button_frame, text="Edit", 
                                  command=lambda c=camera: self.open_edit_camera_window(c),
                                  cursor="hand2")  # Curseur main au survol
            edit_button.pack(side=tk.LEFT, padx=2)
            
            delete_button = tk.Button(button_frame, text="Delete", 
                                    command=lambda c=camera: self.delete_camera_confirm(c),
                                    cursor="hand2")  # Curseur main au survol
            delete_button.pack(side=tk.LEFT, padx=2)
            
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

    def navigate_left(self, event):
        self.camera_list.xview_scroll(-1, "units")

    def navigate_right(self, event):
        self.camera_list.xview_scroll(1, "units")

    def activate_button(self, event):
        focused_widget = self.root.focus_get()
        if isinstance(focused_widget, tk.Button):
            focused_widget.invoke()

    def open_add_camera_window(self):
        add_camera_window = tk.Toplevel(self.root)
        add_camera_window.title("Add Camera")

        # Rendre la fenêtre modale
        add_camera_window.grab_set()  
        add_camera_window.transient(self.root)

        tk.Label(add_camera_window, text="IP Address:").grid(row=0, column=0)
        ip_entry = tk.Entry(add_camera_window)
        ip_entry.grid(row=0, column=1)
        ip_entry.focus_set()

        tk.Label(add_camera_window, text="Username:").grid(row=1, column=0)
        username_entry = tk.Entry(add_camera_window)
        username_entry.grid(row=1, column=1)

        tk.Label(add_camera_window, text="Password:").grid(row=2, column=0)
        password_entry = tk.Entry(add_camera_window, show='*')
        password_entry.grid(row=2, column=1)

        ptz_var = tk.IntVar()
        ptz_check = tk.Checkbutton(add_camera_window, text="Has PTZ", variable=ptz_var)
        ptz_check.grid(row=3, columnspan=2, pady=5)

        def confirm_add():
            ip = ip_entry.get().strip()
            username = username_entry.get().strip()
            password = password_entry.get().strip()

            if not ip:
                messagebox.showerror("Error", "IP address is required")
                return
            if not username:
                messagebox.showerror("Error", "Username is required")
                return
            if not password:
                messagebox.showerror("Error", "Password is required")
                return

            add_camera(ip, username, password, ptz_var.get())
            self.load_cameras()
            add_camera_window.destroy()

        confirm_button = tk.Button(add_camera_window, text="Add", command=confirm_add)
        confirm_button.grid(row=4, columnspan=2, padx=5, pady=5)

        # Bind la touche Entrée pour valider
        add_camera_window.bind('<Return>', lambda e: confirm_add())

    def open_edit_camera_window(self, camera):
        edit_camera_window = tk.Toplevel(self.root)
        edit_camera_window.title("Edit Camera")

        # Rendre la fenêtre modale
        edit_camera_window.grab_set()
        edit_camera_window.transient(self.root)

        tk.Label(edit_camera_window, text="IP Address:").grid(row=0, column=0)
        ip_entry = tk.Entry(edit_camera_window)
        ip_entry.grid(row=0, column=1)
        ip_entry.insert(0, camera[1])

        tk.Label(edit_camera_window, text="Username:").grid(row=1, column=0)
        username_entry = tk.Entry(edit_camera_window)
        username_entry.grid(row=1, column=1)
        username_entry.insert(0, camera[2])

        tk.Label(edit_camera_window, text="Password:").grid(row=2, column=0)
        password_entry = tk.Entry(edit_camera_window, show='*')
        password_entry.grid(row=2, column=1)
        decrypted_password = cipher.decrypt(camera[3]).decode()
        password_entry.insert(0, decrypted_password)

        ptz_var = tk.IntVar(value=1 if camera[4] else 0)  # camera[4] is the ptz column
        ptz_check = tk.Checkbutton(edit_camera_window, text="Has PTZ", variable=ptz_var)
        ptz_check.grid(row=3, columnspan=2, pady=5)

        def save_changes():
            ip = ip_entry.get().strip()
            username = username_entry.get().strip()
            password = password_entry.get().strip()

            if not ip:
                messagebox.showerror("Error", "IP address is required")
                return
            if not username:
                messagebox.showerror("Error", "Username is required")
                return
            if not password:
                messagebox.showerror("Error", "Password is required")
                return

            update_camera(camera[0], ip, username, password, ptz_var.get())
            self.load_cameras()
            edit_camera_window.destroy()

        save_button = tk.Button(edit_camera_window, text="Save", command=save_changes)
        save_button.grid(row=4, columnspan=2, padx=5, pady=5)

        # Bind la touche Entrée pour valider
        edit_camera_window.bind('<Return>', lambda e: save_changes())

    def delete_camera_confirm(self, camera):
        if messagebox.askyesno("Confirm Delete", f"Are you sure you want to delete camera {camera[0]}?"):
            delete_camera(camera[0])
            self.load_cameras()

    def play_camera_thread(self, camera):
        python_exe = get_python39()  # Use Python 3.9 for ONVIF/camera interaction
        if not python_exe:
            messagebox.showerror("Error", "Python 3.9 is required to view cameras")
            return
        
        decrypted_password = cipher.decrypt(camera[3]).decode()
        process = subprocess.Popen([python_exe, 
                                  os.path.join(os.path.dirname(__file__), 'player_vilkin_hikvision.py'),
                                  str(camera[0]),  # ID de la caméra en premier argument
                                  camera[1],       # Adresse IP
                                  camera[2],       # Username
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

    def on_closing(self):
        # Terminer tous les sous-processus
        for process in self.processes:
            try:
                process.terminate()  # Envoyer un signal de terminaison
                process.wait(timeout=1)  # Attendre la fin du processus
            except subprocess.TimeoutExpired:
                process.kill()  # Forcer la fermeture si le processus ne répond pas
            except Exception as e:
                print(f"Erreur lors de la fermeture du processus : {e}")

        # Fermer la fenêtre principale
        self.root.destroy()

    def focus_previous(self, event):
        event.widget.tk_focusPrev().focus_set()

    def focus_next(self, event):
        event.widget.tk_focusNext().focus_set()

if __name__ == "__main__":
    init_db()
    try:
        # Tentative de migration des données existantes
        migrate_existing_data()
    except Exception as e:
        print(f"Global migration error: {e}")
    
    root = tk.Tk()
    root.title("Camera Manager")
    root.geometry("390x250")  # Modification de la largeur initiale à 390px
    app = CameraApp(root)
    root.mainloop()