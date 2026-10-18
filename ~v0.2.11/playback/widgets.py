"""Shared native controls, local Bootstrap PNGs, one non-modal in-window tooltip."""
from pathlib import Path
import json
import tkinter as tk
from tkinter import ttk

from .presentation import ACTIONS, COLORS

ASSETS = Path(__file__).resolve().parent.parent/'assets'/'bootstrap-icons'


class Icons:
    def __init__(self, root):
        self.root = root
        self.scale = max(1., root.winfo_fpixels('1i')/96)
        self.size = min((20, 25, 30, 40), key=lambda n: abs(n-20*self.scale))
        self.images = {}
        self.missing = set()
        self.sprite = None
        self.coordinates = {}
        try:
            self.sprite = tk.PhotoImage(master=root, file=str(ASSETS/'sprite.png'))
            self.coordinates = json.loads((ASSETS/'sprite.json').read_text(encoding='utf-8'))
        except (tk.TclError, OSError, ValueError):
            pass

    def get(self, action, state='normal'):
        name = ACTIONS[action][0]
        key = (name, state, self.size)
        if key not in self.images:
            try:
                x, y, size = self.coordinates[f'{name}/{state}/{self.size}']
                target = tk.PhotoImage(master=self.root, width=size, height=size)
                self.root.tk.call(str(target), 'copy', str(self.sprite), '-from', x, y, x+size, y+size, '-to', 0, 0)
                self.images[key] = target
            except (tk.TclError, KeyError, TypeError):
                self.images[key] = None
                self.missing.add(name)
        return self.images[key]


class Help:
    def __init__(self, root, footer):
        self.root, self.footer = root, footer
        self.pending = None
        self.label = tk.Label(root, bg='#e3ebf6', fg='#111925', bd=1, relief='solid',
                              padx=8, pady=5, font=('Segoe UI', 9), justify='left', wraplength=300)

    def bind(self, widget, text):
        widget.bind('<Enter>', lambda _e: self.schedule(widget, text), add='+')
        widget.bind('<FocusIn>', lambda _e: self.schedule(widget, text), add='+')
        for event in ('<Leave>', '<FocusOut>', '<ButtonPress>', '<Destroy>'):
            widget.bind(event, lambda _e: self.hide(), add='+')

    def schedule(self, widget, text):
        self.hide()
        message = text() if callable(text) else text
        if self.footer is not None:
            self.footer.config(text=message)
        self.pending = self.root.after(400, lambda: self.show(widget, message))

    def show(self, widget, message):
        self.pending = None
        if not widget.winfo_exists() or not widget.winfo_ismapped():
            return
        self.label.config(text=message)
        width = min(310, max(150, self.root.winfo_width()-20))
        x = max(6, min(widget.winfo_rootx()-self.root.winfo_rootx(), self.root.winfo_width()-width-6))
        y = widget.winfo_rooty()-self.root.winfo_rooty()+widget.winfo_height()+4
        if y+65 > self.root.winfo_height():
            y = max(6, y-widget.winfo_height()-75)
        self.label.place(x=x, y=y, width=width)
        self.label.lift()

    def hide(self):
        if self.pending is not None:
            self.root.after_cancel(self.pending)
            self.pending = None
        if self.label.winfo_exists():
            self.label.place_forget()


class IconButton(tk.Button):
    def __init__(self, parent, action, command, icons, help_manager=None, *, label=False, tip=None):
        self.icons, self.action, self.with_label = icons, action, label
        self.reason = ''
        self.tip = tip
        image = icons.get(action)
        super().__init__(parent, command=command, image=image,
            text=ACTIONS[action][1] if label or image is None else '', compound='left',
            font=('Segoe UI', 10), bg=COLORS['raised'], fg=COLORS['text'],
            activebackground=COLORS['border'], activeforeground=COLORS['text'],
            disabledforeground=COLORS['secondary'], bd=0, relief='flat',
            padx=round(8*icons.scale), pady=round(6*icons.scale), takefocus=True,
            highlightthickness=1, highlightbackground=COLORS['raised'], highlightcolor=COLORS['accent'], cursor='hand2')
        def activate(_event):
            self.invoke()
            return 'break'  # Viewer also binds Return at the toplevel.
        self.bind('<Return>', activate)
        self.bind('<Enter>', lambda _e: self.config(bg=COLORS['border']) if self.cget('state') != 'disabled' else None, add='+')
        self.bind('<Leave>', lambda _e: self.config(bg=COLORS['raised']), add='+')
        if help_manager:
            help_manager.bind(self, lambda: self.reason or self.tip or ACTIONS[self.action][2])

    def set_action(self, action):
        if action != self.action:
            self.action = action
            self.set_enabled(self.cget('state') != 'disabled', self.reason)

    def set_enabled(self, enabled, reason=''):
        self.reason = '' if enabled else reason
        image = self.icons.get(self.action, 'normal' if enabled else 'disabled')
        self.config(state='normal' if enabled else 'disabled', image=image,
            text=ACTIONS[self.action][1] if self.with_label or image is None else '')


def styles(root):
    style = ttk.Style(root)
    # Dedicated names; never change the process-wide theme used by Viewer/PTZ.
    style.configure('Playback.TFrame', background=COLORS['panel'])
    style.configure('Playback.TLabel', background=COLORS['panel'], foreground=COLORS['text'], font=('Segoe UI', 10))
    style.configure('Playback.TNotebook', background=COLORS['panel'], borderwidth=0)
    style.configure('Playback.TNotebook.Tab', padding=(10, 6), font=('Segoe UI', 10))
    style.configure('Playback.TCombobox', padding=4, font=('Segoe UI', 10))
