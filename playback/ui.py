"""Single responsive archive window. The video HWND is never recreated on resize."""
import calendar
import ctypes
from dataclasses import replace
from datetime import date, datetime, timedelta
import math
import time
import tkinter as tk
from tkinter import ttk, filedialog
from zoneinfo import ZoneInfo

from .config import ROOT, save_settings
from .controller import Controller
from .model import Controls, Viewport, day_bounds, local_candidates, layout_mode
from .presentation import ACTIONS, COLORS as C, CAMERA_COLORS, STATES, error_text
from .widgets import Icons, IconButton, Help, styles

MONTHS = ('janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre')
DAY_STATES = {'unknown':'non interrogé', 'empty':'absence confirmée', 'partial':'recherche partielle',
              'error':'erreur de recherche', 'present':'présence indexée', 'cache':'cache local'}


def frame(parent, color=None, **kwargs):
    return tk.Frame(parent, bg=color or C['panel'], **kwargs)


def label(parent, text='', *, secondary=False, size=10, **kwargs):
    return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=C['secondary' if secondary else 'text'],
                    font=('Segoe UI', size), **kwargs)


class PlaybackWindow:
    def __init__(self, parent, camera_id=None, on_closed=None, fixture_directory=None):
        self.window = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.window.title('Enregistrements — v0.2.11-dev')
        self.window.configure(bg=C['background'])
        self.window.geometry('1160x800')
        self.window.minsize(520, 460)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.on_closed = on_closed
        self.fixture_directory = fixture_directory
        self.closing = False
        self.initial_camera = camera_id
        self.camera_id = camera_id
        self.controller = None
        self.initialized = False
        self.timer = self.layout_timer = self.seek_timer = None
        self.mode = 'wide'
        self.panel_open = False
        self.dragging = False
        self.selected = date.today()
        self.year, self.month = self.selected.year, self.selected.month
        self.zone = 'America/Toronto'
        self.position = time.time()
        self.view = Viewport(self.position-900)
        self.mark_a = self.mark_b = None
        self.calendar_snapshot = None
        self.controls = Controls()
        self.icons = Icons(self.window)
        styles(self.window)
        self.window.grid_columnconfigure(0, weight=1)
        self.window.grid_rowconfigure(1, weight=1)
        self.footer = label(self.window, 'Survolez une commande ou utilisez Tab pour consulter son aide.',
                            secondary=True, anchor='w', height=2, justify='left')
        self.footer.grid(row=3, column=0, sticky='ew', padx=16, pady=(0,8))
        self.help = Help(self.window, self.footer)
        self._header()
        self.area = frame(self.window, C['background'])
        self.area.grid(row=1, column=0, sticky='nsew', padx=12, pady=(0,8))
        self.area.grid_columnconfigure(1, weight=1)
        self.area.grid_rowconfigure(0, weight=1)
        self._navigation()
        self._video()
        self._commands()
        self._timeline()
        self.notice = label(self.window, 'Version de développement · validation sur votre poste à effectuer',
                            secondary=True, anchor='w')
        self.notice.grid(row=2, column=0, sticky='ew', padx=16, pady=(0,5))
        self.window.bind('<Configure>', self._configure, add='+')
        self.start_timer = self.window.after_idle(self._start)

    def button(self, parent, action, command, *, text=False, tip=None):
        return IconButton(parent, action, command, self.icons, self.help, label=text, tip=tip)

    def _header(self):
        header = frame(self.window, C['background'])
        header.grid(row=0, column=0, sticky='ew', padx=16, pady=(14,12))
        header.grid_columnconfigure(1, weight=1)
        title = label(header, 'Enregistrements', size=17, anchor='w')
        title.grid(row=0, column=0, columnspan=2, sticky='w')
        self.identity = label(header, 'ARCHIVES · initialisation', secondary=True, anchor='w')
        self.identity.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(3,0))
        actions = frame(header, C['background'])
        actions.grid(row=0, column=2, rowspan=2, sticky='e')
        self.button(actions, 'navigation', self.toggle_panel).pack(side='left', padx=2)
        self.button(actions, 'settings', lambda: self.show_tab(2)).pack(side='left', padx=2)
        self.button(actions, 'details', lambda: self.show_tab(1)).pack(side='left', padx=2)

    def _navigation(self):
        self.side = frame(self.area, width=320)
        self.side.grid(row=0, column=0, rowspan=2, sticky='nsew', padx=(0,10))
        self.side.grid_propagate(False)
        self.side.grid_rowconfigure(0, weight=1)
        self.side.grid_columnconfigure(0, weight=1)
        self.tabs = ttk.Notebook(self.side, style='Playback.TNotebook')
        self.tabs.grid(row=0, column=0, sticky='nsew')
        self.nav = frame(self.tabs)
        self.details = frame(self.tabs)
        self.settings_frame = frame(self.tabs)
        for panel, title in ((self.nav,'Calendrier'), (self.details,'Détails / export'), (self.settings_frame,'Réglages')):
            self.tabs.add(panel, text=title)
        self.nav.grid_columnconfigure(0, weight=1)
        self.nav.grid_rowconfigure(5, weight=1)
        label(self.nav, 'CAMÉRA ACTIVE', secondary=True, size=9, anchor='w').grid(row=0,column=0,sticky='ew',padx=10,pady=(12,3))
        self.camera_choice = ttk.Combobox(self.nav, state='readonly', style='Playback.TCombobox')
        self.camera_choice.grid(row=1,column=0,sticky='ew',padx=10)
        self.camera_choice.bind('<<ComboboxSelected>>', self._camera_changed)
        filter_frame = frame(self.nav)
        filter_frame.grid(row=2,column=0,sticky='ew',padx=10,pady=8)
        label(filter_frame, 'Caméras du calendrier', secondary=True).pack(anchor='w')
        self.filter_list = tk.Listbox(filter_frame, selectmode='multiple', exportselection=False, height=3,
            bg=C['background'], fg=C['text'], selectbackground=C['raised'], selectforeground=C['accent'],
            bd=0, highlightthickness=1, highlightcolor=C['accent'], highlightbackground=C['border'], font=('Segoe UI',10))
        self.filter_list.pack(side='left', fill='x', expand=True)
        scroll = ttk.Scrollbar(filter_frame, command=self.filter_list.yview)
        scroll.pack(side='right',fill='y')
        self.filter_list.config(yscrollcommand=scroll.set)
        self.filter_list.bind('<<ListboxSelect>>', lambda _e: self._request_month())
        monthbar = frame(self.nav)
        monthbar.grid(row=3,column=0,sticky='ew',padx=8,pady=4)
        self.button(monthbar,'previous',lambda:self.change_month(-1)).pack(side='left')
        self.month_label = label(monthbar, '', size=11)
        self.month_label.pack(side='left',fill='x',expand=True)
        self.button(monthbar,'next',lambda:self.change_month(1)).pack(side='right')
        quick = frame(self.nav)
        quick.grid(row=4,column=0,sticky='ew',padx=8,pady=4)
        self.button(quick,'today',self.today,text=True).pack(side='left')
        self.button(quick,'refresh',lambda:self._request_month(True)).pack(side='right')
        # The calendar scrolls vertically at low height; no controls are clipped.
        calwrap = frame(self.nav)
        calwrap.grid(row=5,column=0,sticky='nsew',padx=6,pady=6)
        calwrap.grid_columnconfigure(0,weight=1)
        calwrap.grid_rowconfigure(0,weight=1)
        self.calcanvas = tk.Canvas(calwrap,bg=C['panel'],highlightthickness=0)
        self.calcanvas.grid(row=0,column=0,sticky='nsew')
        sb = ttk.Scrollbar(calwrap,command=self.calcanvas.yview)
        sb.grid(row=0,column=1,sticky='ns')
        self.calcanvas.config(yscrollcommand=sb.set)
        self.calgrid = frame(self.calcanvas)
        self.calwindow = self.calcanvas.create_window(0,0,window=self.calgrid,anchor='nw')
        self.calcanvas.bind('<Configure>', lambda e:self.calcanvas.itemconfigure(self.calwindow,width=e.width))
        self.calgrid.bind('<Configure>', lambda _e:self.calcanvas.configure(scrollregion=self.calcanvas.bbox('all')))
        label(self.nav,'C# : vidéo indexée · * : cache local\n? non interrogé · ! erreur · … partiel · — vide',
              secondary=True,size=9,justify='left').grid(row=6,column=0,sticky='w',padx=10,pady=(0,8))
        self._build_calendar()
        self._details_panel()
        self._settings_panel()

    def _video(self):
        self.video = frame(self.area, '#080d14')
        self.video.grid(row=0,column=1,sticky='nsew')
        self.surface = frame(self.video,'#080d14')
        self.surface.place(x=0,y=0,relwidth=1,relheight=1)
        self.overlay = tk.Label(self.video,text='ENREGISTREMENTS\n\nChoisissez une caméra et une date.',
            bg='#080d14',fg=C['secondary'],font=('Segoe UI',13),justify='center',wraplength=420)
        self.overlay.place(x=0,y=0,relwidth=1,relheight=1)
        self.overlay.lift()

    def _commands(self):
        self.commands = frame(self.area)
        self.commands.grid(row=1,column=1,sticky='ew',pady=(6,0))
        self.groups = [frame(self.commands) for _ in range(3)]
        g = self.groups[0]
        self.button(g,'back',lambda:self.jump(-10),text=True).pack(side='left',padx=2,pady=3)
        self.play_button = self.button(g,'play',self.play_pause)
        self.play_button.pack(side='left',padx=2,pady=3)
        self.button(g,'stop',self.stop).pack(side='left',padx=2,pady=3)
        self.button(g,'forward',lambda:self.jump(10),text=True).pack(side='left',padx=2,pady=3)
        self.button(g,'next_record',self.next_record).pack(side='left',padx=2,pady=3)
        g = self.groups[1]
        label(g,'Vitesse').pack(side='left',padx=(6,4))
        self.rate = ttk.Combobox(g,state='readonly',width=5,values=('0,5×','1×','2×','4×'),style='Playback.TCombobox')
        self.rate.set('1×')
        self.rate.pack(side='left',padx=2)
        self.rate.bind('<<ComboboxSelected>>',self._rate_changed)
        self.mute_button = self.button(g,'mute',self.toggle_mute)
        self.mute_button.pack(side='left',padx=5)
        self.volume = tk.Scale(g,from_=0,to=100,orient='horizontal',showvalue=False,length=85,
            bg=C['panel'],fg=C['text'],highlightthickness=0,troughcolor=C['raised'],bd=0,
            command=self._volume_changed)
        self.volume.set(70)
        self.volume.pack(side='left',padx=2)
        self.help.bind(self.volume,'Régler le volume de 0 à 100 ; les flèches fonctionnent au clavier')
        g = self.groups[2]
        self.button(g,'previous',lambda:self.select_date(self.selected-timedelta(days=1)),tip='Afficher le jour précédent').pack(side='left',padx=2)
        self.clock_entry = ttk.Entry(g,width=10,font=('Segoe UI',10))
        self.clock_entry.insert(0,'12:00:00')
        self.clock_entry.pack(side='left',padx=3)
        self.clock_entry.bind('<Return>',lambda _e:self.goto())
        self.button(g,'goto',self.goto).pack(side='left',padx=2)
        self.button(g,'next',lambda:self.select_date(self.selected+timedelta(days=1)),tip='Afficher le jour suivant').pack(side='left',padx=2)

    def _timeline(self):
        self.timeframe = frame(self.area)
        self.timeframe.grid(row=2,column=0,columnspan=2,sticky='ew',pady=(10,0))
        self.timeframe.grid_columnconfigure(0,weight=1)
        bar = frame(self.timeframe)
        bar.grid(row=0,column=0,columnspan=2,sticky='ew',padx=8,pady=5)
        self.time_label = label(bar,'—',size=11)
        self.time_label.pack(side='left',fill='x',expand=True)
        self.button(bar,'zoom_out',lambda:self.zoom(2)).pack(side='right',padx=2)
        self.button(bar,'zoom_in',lambda:self.zoom(.5)).pack(side='right',padx=2)
        self.timeline = tk.Canvas(self.timeframe,height=110,bg=C['background'],highlightthickness=0,takefocus=True)
        self.timeline.grid(row=1,column=0,sticky='ew',padx=(8,0))
        self.timeline_scroll = ttk.Scrollbar(self.timeframe,command=self.timeline.yview)
        self.timeline_scroll.grid(row=1,column=1,sticky='ns')
        self.timeline.config(yscrollcommand=self.timeline_scroll.set)
        self.timeline.bind('<Configure>',lambda _e:self._draw_timeline())
        self.timeline.bind('<ButtonPress-1>',self._drag_start)
        self.timeline.bind('<B1-Motion>',self._drag)
        self.timeline.bind('<ButtonRelease-1>',self._drag_end)
        self.timeline.bind('<Left>',lambda _e:self.jump(-10))
        self.timeline.bind('<Right>',lambda _e:self.jump(10))
        self.timeline.bind('<plus>',lambda _e:self.zoom(.5))
        self.timeline.bind('<minus>',lambda _e:self.zoom(2))
        self.horizontal = ttk.Scrollbar(self.timeframe,orient='horizontal',command=self._scroll_time)
        self.horizontal.grid(row=2,column=0,columnspan=2,sticky='ew',padx=8,pady=3)
        label(self.timeframe,'Index caméra ▬   Reçu ━   Lisible ═   Durée inconnue │   Fond : consulter l’état du jour',
              secondary=True,size=9,anchor='w').grid(row=3,column=0,columnspan=2,sticky='ew',padx=8,pady=(0,6))

    def _details_panel(self):
        self.details.grid_rowconfigure(0,weight=1)
        self.details.grid_columnconfigure(0,weight=1)
        self.detail_text = tk.Text(self.details,wrap='word',width=30,height=8,bg=C['panel'],fg=C['text'],
            font=('Segoe UI',10),bd=0,padx=10,pady=10,state='disabled')
        self.detail_text.grid(row=0,column=0,sticky='nsew')
        scroll = ttk.Scrollbar(self.details,command=self.detail_text.yview)
        scroll.grid(row=0,column=1,sticky='ns')
        self.detail_text.config(yscrollcommand=scroll.set)
        box = frame(self.details)
        box.grid(row=1,column=0,columnspan=2,sticky='ew',padx=8,pady=8)
        self.original_button = self.button(box,'original',lambda:self.export(False),text=True)
        self.original_button.pack(fill='x',pady=3)
        marks = frame(box)
        marks.pack(fill='x',pady=3)
        for text,which in (('A — début','a'),('B — fin','b')):
            button = tk.Button(marks,text=text,command=lambda w=which:self.mark(w),bg=C['raised'],fg=C['text'],
                activebackground=C['border'],activeforeground=C['text'],bd=0,padx=8,pady=6,takefocus=True)
            button.pack(side='left',fill='x',expand=True,padx=2)
            self.help.bind(button,'Marquer la position actuelle comme '+('début' if which=='a' else 'fin')+' de la plage à exporter')
        self.range_label = label(box,'Plage A–B : non sélectionnée',secondary=True,anchor='w',wraplength=270)
        self.range_label.pack(fill='x',pady=4)
        self.export_button = self.button(box,'export',lambda:self.export(True),text=True)
        self.export_button.pack(fill='x',pady=3)
        self.cancel_button = self.button(box,'cancel',lambda:self.controller.cancel_export() if self.controller else None,text=True)
        self.cancel_button.pack(fill='x',pady=3)
        self.export_label = label(box,'',secondary=True,anchor='w',wraplength=270)
        self.export_label.pack(fill='x')

    def _settings_panel(self):
        self.settings_frame.grid_columnconfigure(0,weight=1)
        canvas = tk.Canvas(self.settings_frame,bg=C['panel'],highlightthickness=0)
        canvas.pack(side='left',fill='both',expand=True)
        scroll = ttk.Scrollbar(self.settings_frame,command=canvas.yview)
        scroll.pack(side='right',fill='y')
        canvas.config(yscrollcommand=scroll.set)
        inner = frame(canvas)
        window = canvas.create_window(0,0,window=inner,anchor='nw')
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        inner.bind('<Configure>',lambda _e:canvas.config(scrollregion=canvas.bbox('all')))
        self.fields = {}
        for name,title,values in (
            ('backend','API de la caméra active',('auto','isapi','videolink')),
            ('zone','Fuseau caméra confirmé (CGI)',('','America/Toronto','UTC','Europe/Paris')),
            ('track','Piste / flux (vide = automatique)',None),
            ('revision','Révision locale de l’appareil',None),
            ('time_shift','Correction locale explicite (secondes)',None),
            ('display_zone','Fuseau d’affichage',('America/Toronto','UTC','Europe/Paris')),
            ('cache_gib','Quota du cache (Gio)',None)):
            label(inner,title,secondary=True,anchor='w').pack(fill='x',padx=10,pady=(12,3))
            widget = ttk.Combobox(inner,values=values,style='Playback.TCombobox') if values else ttk.Entry(inner,font=('Segoe UI',10))
            widget.pack(fill='x',padx=10)
            self.fields[name] = widget
        label(inner,'La correction d’heure agit uniquement ici.\nLa caméra et son horloge ne sont pas modifiées.\n\n'
              'Changez la révision après remplacement d’une caméra sans numéro de série détectable.\n'
              'Les mots de passe restent dans la base existante.',secondary=True,justify='left',wraplength=270).pack(fill='x',padx=10,pady=12)
        self.button(inner,'apply',self.apply_settings,text=True).pack(fill='x',padx=10,pady=8)

    def _start(self):
        self.start_timer = None
        if self.closing:
            return
        hwnd = self.surface.winfo_id()
        if __import__('os').name == 'nt':
            user32 = ctypes.WinDLL('user32',use_last_error=True)
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p,ctypes.c_int]
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p,ctypes.c_int,ctypes.c_long]
            user32.SetWindowLongW(hwnd,-16,user32.GetWindowLongW(hwnd,-16)|0x02000000)
        self.controller = Controller(hwnd,fixture_directory=self.fixture_directory)
        self._poll()
        self._layout()

    def focus_camera(self,camera_id=None):
        if camera_id is not None:
            self.initial_camera = camera_id
            if self.initialized:
                self.camera_choice.set(f'C{camera_id}')
                self._camera_changed()
        self.window.deiconify()
        self.window.lift()

    def _initialize(self):
        settings = self.controller.settings
        if settings is None or self.controller.closed.is_set():
            return
        self.zone = settings.display_zone
        self.selected = datetime.now(ZoneInfo(self.zone)).date()
        if self.controller.fixture_data:
            self.selected = datetime.fromtimestamp(self.controller.fixture_data['recordings'][0]['start'],ZoneInfo(self.zone)).date()
        self.year,self.month = self.selected.year,self.selected.month
        self.ids = [c.camera_id for c in self.controller.cameras]
        values = [f'C{i}' for i in self.ids]
        self.camera_choice.config(values=values)
        for value in values:
            self.filter_list.insert('end',value)
        self.filter_list.selection_set(0,'end')
        self.camera_id = self.initial_camera if self.initial_camera in self.ids else (self.ids[0] if self.ids else None)
        if self.camera_id is not None:
            self.camera_choice.set(f'C{self.camera_id}')
        self.initialized = True
        self.position = day_bounds(self.selected,self.zone)[0]+12*3600
        self.view = Viewport(self.position-900)
        self._build_calendar()
        self._load_fields()
        self._request_month()

    def _poll(self):
        self.timer = None
        if self.closing:
            return
        if self.controller.ready.is_set() and not self.initialized:
            self._initialize()
        status = self.controller.view
        if status.camera_id and status.state not in ('IDLE','STOPPED') and not self.dragging:
            self.position = status.position
            if status.state == 'PLAYING':
                local_day = datetime.fromtimestamp(self.position, ZoneInfo(self.zone)).date()
                if local_day != self.selected:
                    self.selected = local_day
                    if (self.year,self.month) != (local_day.year,local_day.month):
                        self.year,self.month = local_day.year,local_day.month
                        self._build_calendar()
                        self._request_month()
                    else:
                        self._paint_calendar()
            if status.state == 'PLAYING' and not self.view.start <= self.position <= self.view.start+self.view.span:
                self.view = Viewport(self.position-self.view.span*.2,self.view.span)
        text = STATES.get(status.state,status.state)
        if status.state == 'DOWNLOADING':
            text += f' · {status.received/1048576:.1f} Mio'
            if status.expected:
                text += f' / {status.expected/1048576:.1f} Mio'
        elif status.source == 'cache':
            text += ' · cache local'
        if status.reason:
            text += '\n'+error_text(status.reason)
        self.overlay.config(text=text,wraplength=max(200,self.video.winfo_width()-60))
        if status.state in ('PLAYING','PAUSED'):
            self.overlay.place_forget()
        else:
            self.overlay.place(x=0,y=0,relwidth=1,relheight=1)
            self.overlay.lift()
        try:
            instant = datetime.fromtimestamp(self.position,ZoneInfo(self.zone))
            shown = instant.strftime('%Y-%m-%d  %H:%M:%S %Z')
        except Exception:
            shown = '—'
        prefix='DÉMONSTRATION LOCALE' if self.fixture_directory else 'ARCHIVES'
        self.identity.config(text=f'{prefix} · C{self.camera_id or "—"} · {self.controls.rate:g}× · {shown}')
        self.time_label.config(text=f'{shown}  ·  {self.view.span/60:g} min')
        self.notice.config(text=text.split('\n')[0])
        self.play_button.set_action('play' if self.controls.paused or status.state in ('IDLE','STOPPED','GAP','ERROR','ENDED') else 'pause')
        self.original_button.set_enabled(bool(status.key), 'Choisissez et recevez une archive avant de la conserver')
        self.export_button.set_enabled(bool(status.key and self.mark_a is not None and self.mark_b is not None and self.mark_b>self.mark_a),
                                       'Sélectionnez un début A et une fin B dans une archive reçue')
        self.cancel_button.set_enabled(self.controller.export_request is not None,'Aucun export en cours')
        export = self.controller.export_status
        self.export_label.config(text={'pending':'Export en attente','working':'Export en cours',
            'complete':'Export conservé avec ses métadonnées','cancelled':'Export annulé'}.get(export,error_text(export)))
        if self.calendar_snapshot is not self.controller.calendar:
            self.calendar_snapshot = self.controller.calendar
            self._paint_calendar()
            self._show_details()
        self._draw_timeline()
        self.timer = self.window.after(200,self._poll)

    def selected_ids(self):
        return tuple(self.ids[n] for n in self.filter_list.curselection()) if self.initialized else ()

    def _request_month(self,force=False):
        if self.initialized and not self.closing:
            self.controller.month(self.year,self.month,self.selected_ids(),force)

    def _build_calendar(self):
        self.help.hide()
        for child in self.calgrid.winfo_children():
            child.destroy()
        self.cells = {}
        self.month_label.config(text=f'{MONTHS[self.month-1]} {self.year}')
        for col,title in enumerate(('L','M','M','J','V','S','D')):
            self.calgrid.grid_columnconfigure(col,weight=1,uniform='days')
            label(self.calgrid,title,secondary=True,size=9).grid(row=0,column=col,sticky='ew')
        for row,week in enumerate(calendar.monthcalendar(self.year,self.month),1):
            for col,number in enumerate(week):
                cell = frame(self.calgrid,C['background'],height=86)
                cell.grid(row=row,column=col,sticky='nsew',padx=1,pady=1)
                cell.grid_columnconfigure(0,weight=1)
                if number:
                    day = date(self.year,self.month,number)
                    date_button = tk.Button(cell,text=str(number),bg=C['background'],fg=C['text'],bd=0,
                        activebackground=C['border'],activeforeground=C['text'],font=('Segoe UI',10),
                        command=lambda d=day:self.select_date(d),takefocus=True,pady=3)
                    date_button.grid(row=0,column=0,sticky='ew')
                    badges=[]
                    for n in (1,2):
                        badge = tk.Button(cell,text='',font=('Segoe UI',8),bg=C['background'],fg=C['text'],
                                          bd=0,padx=0,pady=2,takefocus=True)
                        badge.grid(row=n,column=0,sticky='ew',padx=1,pady=1)
                        badges.append(badge)
                    self.cells[day]=(date_button,badges)
        self._paint_calendar()

    def _paint_calendar(self):
        states = self.controller.calendar if self.controller else {}
        ids = self.selected_ids()
        for day,(button,badges) in self.cells.items():
            button.config(bg=C['raised'] if day==self.selected else C['background'],
                          fg=C['accent'] if day==self.selected else C['text'])
            present=[cid for cid in ids if states.get((cid,day),{}).get('present')]
            for n,badge in enumerate(badges):
                if n < len(present) and (n==0 or len(present)<=2):
                    cid=present[n]
                    item=states.get((cid,day),{})
                    suffix='*' if item.get('cached') else ''
                    badge.config(text=f'C{cid}{suffix}',state='normal',bg=CAMERA_COLORS[cid%len(CAMERA_COLORS)],
                                 command=lambda c=cid,d=day:self.select_date(d,c))
                elif n==1 and len(present)>2:
                    badge.config(text=f'+{len(present)-1}',state='normal',bg=C['raised'],
                                 command=lambda d=day:self.day_details(d))
                else:
                    kinds=[states.get((cid,day),{}).get('state','unknown') for cid in ids]
                    symbol='!' if 'error' in kinds else '…' if 'partial' in kinds else '—' if kinds and all(k=='empty' for k in kinds) else '?'
                    badge.config(text=symbol if n==0 and not present else '',state='normal',bg=C['background'],
                                 command=lambda d=day:self.day_details(d))

    def day_details(self,day):
        self.selected=day
        self._paint_calendar()
        self.show_tab(1)
        self._show_details()

    def _show_details(self):
        if not self.controller:
            return
        lines=[self.selected.strftime('%Y-%m-%d')+' · '+self.zone, '', 'Disponibilités des caméras filtrées']
        for cid in self.selected_ids():
            info=self.controller.calendar.get((cid,self.selected),{})
            title=DAY_STATES.get(info.get('state','unknown'),'inconnu')
            lines.append(f'C{cid} · {title}'+(' · cache local' if info.get('cached') else ''))
            if info.get('checked'):
                lines.append('  Recherche : '+datetime.fromtimestamp(info['checked'],ZoneInfo(self.zone)).strftime('%d/%m %H:%M'))
            if info.get('reason'):
                lines.append('  '+error_text(info['reason']))
        lines.extend(('', 'Un badge signifie présence indexée, pas une journée complète.',
            'Une durée CGI inconnue apparaît comme un repère ponctuel jusqu’à inspection du média.',
            'Le cache est une copie observée : un fichier caméra peut encore grandir.', '',
            'Audio : même réglage à toutes les vitesses. Son initialement coupé ; utilisez le bouton Son.',
            'Export de plage : remux MKV aux images clés, sans promesse de coupe à l’image.'))
        if self.icons.missing:
            lines.append('Ressources d’icônes manquantes : '+', '.join(sorted(self.icons.missing)))
        self.detail_text.config(state='normal')
        self.detail_text.delete('1.0','end')
        self.detail_text.insert('1.0','\n'.join(lines))
        self.detail_text.config(state='disabled')

    def select_date(self,day,camera_id=None):
        self.stop()
        self.selected=day
        if camera_id is not None:
            self.camera_id=camera_id
            self.camera_choice.set(f'C{camera_id}')
            self._load_fields()
        if (self.year,self.month)!=(day.year,day.month):
            self.year,self.month=day.year,day.month
            self._build_calendar()
            self._request_month()
        self.position=day_bounds(day,self.zone)[0]+12*3600
        self.view=Viewport(self.position-900,self.view.span)
        self._paint_calendar()
        self._show_details()
        self._draw_timeline()

    def change_month(self,delta):
        total=self.year*12+self.month-1+delta
        self.year,self.month=total//12,total%12+1
        self._build_calendar()
        self._request_month()

    def today(self):
        self.select_date(datetime.now(ZoneInfo(self.zone)).date())

    def _camera_changed(self,_event=None):
        try:
            self.camera_id=int(self.camera_choice.get()[1:])
        except ValueError:
            return
        self._load_fields()
        if self.controller and self.controller.request is not None:
            self.seek(self.position)

    def seek(self,stamp):
        if self.controller and self.initialized and self.camera_id is not None:
            self.position=stamp
            self.controller.select(self.camera_id,stamp)

    def goto(self):
        try:
            clock=datetime.strptime(self.clock_entry.get().strip(),'%H:%M:%S').time()
            values=local_candidates(datetime.combine(self.selected,clock),self.zone)
            if len(values)!=1:
                self.footer.config(text=error_text('ambiguous-time' if values else 'nonexistent-time'))
                return
            self.position=values[0].timestamp()
            self.view=Viewport(self.position-self.view.span*.25,self.view.span)
            self.seek(self.position)
        except ValueError:
            self.footer.config(text='Saisissez une heure au format HH:mm:ss.')

    def jump(self,seconds):
        self.seek(self.position+seconds)

    def play_pause(self):
        status=self.controller.view if self.controller else None
        if status is None:
            return
        if status.state in ('IDLE','STOPPED','GAP','ERROR','ENDED','FAILED'):
            self.controls=replace(self.controls,paused=False)
            self.controller.set_controls(self.controls)
            self.seek(self.position)
        else:
            self.controls=replace(self.controls,paused=not self.controls.paused)
            self.controller.set_controls(self.controls)

    def stop(self):
        if self.seek_timer is not None:
            self.window.after_cancel(self.seek_timer)
            self.seek_timer=None
        if self.controller:
            self.controller.stop()

    def next_record(self):
        if not self.controller:
            return
        candidates=[e for e in self.controller.entries if e.recording.camera_id==self.camera_id and e.recording.start>self.position+.5]
        if candidates:
            target=min(e.recording.start for e in candidates)
            self.footer.config(text=f'Saut demandé vers le prochain enregistrement : +{target-self.position:.0f} secondes.')
            self.seek(target)
        else:
            self.footer.config(text='Aucun enregistrement suivant dans le mois chargé. Consultez le mois suivant.')

    def _rate_changed(self,_event=None):
        rate=float(self.rate.get().replace('×','').replace(',','.'))
        self.controls=replace(self.controls,rate=rate)
        if self.controller:
            self.controller.set_controls(self.controls)

    def toggle_mute(self):
        self.controls=replace(self.controls,muted=not self.controls.muted)
        self.mute_button.set_action('mute' if self.controls.muted else 'sound')
        if self.controller:
            self.controller.set_controls(self.controls)

    def _volume_changed(self,value):
        self.controls=replace(self.controls,volume=int(float(value)))
        if self.controller:
            self.controller.set_controls(self.controls)

    def zoom(self,factor):
        self.view=self.view.zoom(factor,self.position)
        self._draw_timeline()

    def _scroll_time(self,*args):
        a,b=day_bounds(self.selected,self.zone)
        if args[0]=='moveto':
            self.view=Viewport(a+float(args[1])*(b-a),self.view.span)
        else:
            self.view=Viewport(self.view.start+int(args[1])*self.view.span*.2,self.view.span)
        self._draw_timeline()

    def _draw_timeline(self):
        if not hasattr(self,'timeline'):
            return
        canvas=self.timeline
        canvas.delete('all')
        width=max(1,canvas.winfo_width()-58)
        ids=self.selected_ids()
        end=self.view.start+self.view.span
        step=next((n for n in (10,30,60,120,300,600,1800,3600,7200,14400,43200) if self.view.span/n<=max(2,width//100)),43200)
        try:
            zone=ZoneInfo(self.zone)
        except Exception:
            return
        for n in range(math.floor(self.view.start/step),math.ceil(end/step)+1):
            stamp=n*step
            x=58+(stamp-self.view.start)/self.view.span*width
            if x<58 or x>width+58:
                continue
            canvas.create_line(x,24,x,30+28*max(1,len(ids)),fill=C['border'])
            canvas.create_text(x,12,text=datetime.fromtimestamp(stamp,zone).strftime('%H:%M:%S' if step<60 else '%H:%M'),
                               fill=C['secondary'],font=('Segoe UI',9))
        entries=self.controller.entries if self.controller else ()
        for row,cid in enumerate(ids):
            y=32+row*28
            canvas.create_text(5,y+6,text=f'C{cid}',anchor='w',fill=C['text'],font=('Segoe UI',10))
            for entry in entries:
                r=entry.recording
                if r.camera_id!=cid or r.start>end or (entry.end is not None and entry.end<self.view.start):
                    continue
                x1=58+max(0,(r.start-self.view.start)/self.view.span)*width
                if entry.end is None:
                    if r.start>=self.view.start:
                        canvas.create_line(x1,y-2,x1,y+15,fill='#9db6da',width=3,dash=(2,2))
                    continue
                x2=58+min(1,(entry.end-self.view.start)/self.view.span)*width
                canvas.create_rectangle(x1,y,x2,y+12,fill=CAMERA_COLORS[cid%len(CAMERA_COLORS)],outline='')
                if entry.state in ('downloaded','preparing','prepared'):
                    canvas.create_line(x1,y+12,x2,y+12,fill='#e9bc73',width=2)
                if entry.state=='prepared':
                    canvas.create_line(x1,y+7,x2,y+7,fill=C['accent'],width=3)
            # Actual in-flight published segments, never a nominal minute estimate.
            playlist=self.controller.active_playlist if self.controller else None
            if playlist and cid==self.camera_id:
                for s in playlist.segments:
                    if s.start<end and s.start+s.duration>self.view.start:
                        x1=58+max(0,(s.start-self.view.start)/self.view.span)*width
                        x2=58+min(1,(s.start+s.duration-self.view.start)/self.view.span)*width
                        canvas.create_line(x1,y+5,x2,y+5,fill=C['accent'],width=4)
        height=38+28*max(1,len(ids))
        for stamp,color,letter in ((self.position,C['text'],''),(self.mark_a,'#e9bc73','A'),(self.mark_b,'#e9bc73','B')):
            if stamp is not None and self.view.start<=stamp<=end:
                x=58+(stamp-self.view.start)/self.view.span*width
                canvas.create_line(x,23,x,height,fill=color,width=2)
                if letter:
                    canvas.create_text(x+7,height-8,text=letter,fill=color)
        canvas.config(scrollregion=(0,0,width+58,height))
        a,b=day_bounds(self.selected,self.zone)
        self.horizontal.set(max(0,min(1,(self.view.start-a)/(b-a))),max(0,min(1,(end-a)/(b-a))))

    def _drag_start(self,event):
        self.timeline.focus_set()
        ids=self.selected_ids()
        row=int((self.timeline.canvasy(event.y)-28)//28)
        if 0<=row<len(ids):
            self.camera_id=ids[row]
            self.camera_choice.set(f'C{self.camera_id}')
        self.dragging=True
        self._drag(event)

    def _drag(self,event):
        self.position=self.view.at(event.x-58,max(1,self.timeline.winfo_width()-58))
        if self.seek_timer is not None:
            self.window.after_cancel(self.seek_timer)
        self.seek_timer=self.window.after(350,self._commit_seek)
        self._draw_timeline()

    def _drag_end(self,event):
        self._drag(event)
        self.dragging=False
        self._commit_seek()

    def _commit_seek(self):
        if self.seek_timer is not None:
            self.window.after_cancel(self.seek_timer)
            self.seek_timer=None
        self.seek(self.position)

    def mark(self,which):
        if which=='a':
            self.mark_a=self.position
        else:
            self.mark_b=self.position
        def show(stamp):
            return datetime.fromtimestamp(stamp,ZoneInfo(self.zone)).strftime('%H:%M:%S') if stamp is not None else '—'
        self.range_label.config(text=f'A {show(self.mark_a)} → B {show(self.mark_b)}')
        self._draw_timeline()

    def export(self,selection):
        if not self.controller or not self.controller.view.key:
            return
        stamp=datetime.fromtimestamp(self.position,ZoneInfo(self.zone)).strftime('%Y%m%d-%H%M%S')
        name=f'C{self.camera_id}-{stamp}'+('-plage.mkv' if selection else '-original.bin')
        path=filedialog.asksaveasfilename(parent=self.window,title='Conserver la plage MKV' if selection else 'Conserver l’archive originale',
            initialfile=name,defaultextension='.mkv' if selection else '.bin')
        if path:
            self.controller.export(path,self.mark_a if selection else None,self.mark_b if selection else None)

    def _load_fields(self):
        if not self.initialized:
            return
        settings=self.controller.settings
        local=settings.cameras.get(str(self.camera_id),{})
        defaults={'backend':'auto','zone':'','track':'','revision':'','time_shift':0,
                  'display_zone':settings.display_zone,'cache_gib':settings.cache_gib}
        for name,widget in self.fields.items():
            widget.delete(0,'end')
            widget.insert(0,str(local.get(name,defaults[name])))

    def apply_settings(self):
        if self.fixture_directory:
            self.footer.config(text='Le banc local utilise ses propres réglages et son propre cache.')
            return
        if not self.initialized or self.camera_id is None:
            return
        try:
            values={name:widget.get().strip() for name,widget in self.fields.items()}
            if values['backend'] not in ('auto','isapi','videolink'):
                raise ValueError
            ZoneInfo(values['display_zone'])
            if values['zone']:
                ZoneInfo(values['zone'])
            shift=int(values['time_shift'])
            quota=float(values['cache_gib'])
            if abs(shift)>86400 or not max(1,self.controller.settings.max_archive_gib)<=quota<=1024:
                raise ValueError
            settings=replace(self.controller.settings,cameras=dict(self.controller.settings.cameras),
                             display_zone=values['display_zone'],cache_gib=quota)
            previous=settings.cameras.get(str(self.camera_id),{})
            settings.cameras[str(self.camera_id)]=dict(previous,backend=values['backend'],zone=values['zone'],
                track=values['track'],revision=values['revision'],time_shift=shift)
            save_settings(settings)
            self.footer.config(text='Réglages enregistrés. Fermez puis rouvrez Enregistrements pour les appliquer.')
        except Exception:
            self.footer.config(text='Réglages invalides : vérifiez les fuseaux, la correction et le quota.')

    def toggle_panel(self):
        self.panel_open=not self.panel_open
        self._layout()

    def show_tab(self,index):
        self.panel_open=True
        self.tabs.select(index)
        self._layout()
        if index==1:
            self._show_details()

    def _configure(self,event):
        if event.widget!=self.window or self.closing or self.layout_timer is not None:
            return
        self.layout_timer=self.window.after(70,self._layout)

    def _layout(self):
        if self.layout_timer is not None:
            self.window.after_cancel(self.layout_timer)
            self.layout_timer=None
        if self.closing:
            return
        self.help.hide()
        width,height=self.window.winfo_width(),self.window.winfo_height()
        self.mode=layout_mode(width,height,self.icons.scale,self.mode)
        self.footer.config(wraplength=max(200,width-32))
        wide=self.mode in ('wide','medium')
        self.area.grid_columnconfigure(0,weight=0,minsize=0)
        self.area.grid_rowconfigure(3,weight=0,minsize=0)
        # Video stays mapped throughout. Only its grid coordinates/spans change.
        self.video.grid_configure(row=0,column=1 if wide else 0,columnspan=1 if wide else 2)
        self.commands.grid_configure(row=1,column=1 if wide else 0,columnspan=1 if wide else 2)
        if wide:
            self.side.place_forget()
            self.side.grid(row=0,column=0,rowspan=2,sticky='nsew',padx=(0,10))
            self.side.configure(width=round(320*self.icons.scale))
        else:
            self.side.grid_remove()
            if self.panel_open:
                # A dismissible in-window drawer for short/compact layouts. The
                # renderer behind it keeps the same HWND, media and dimensions.
                self.side.place(x=0,y=0,width=min(width-28,round(360*self.icons.scale)),relheight=1)
                self.side.lift()
            else:
                self.side.place_forget()
        available=max(300,width-(350*self.icons.scale if wide else 32))
        row=col=used=0
        for group in self.groups:
            requested=group.winfo_reqwidth()+8
            if used and used+requested>available:
                row+=1
                col=used=0
            group.grid(row=row,column=col,sticky='w',padx=3,pady=2)
            used+=requested
            col+=1
        self.timeline.config(height=60 if self.mode=='short' else 110)

    def close(self):
        if self.closing:
            return
        self.closing=True
        self.help.hide()
        for name in ('timer','layout_timer','seek_timer','start_timer'):
            value=getattr(self,name,None)
            if value is not None:
                self.window.after_cancel(value)
                setattr(self,name,None)
        self.window.withdraw()
        if self.controller:
            self.controller.close()
        self._finish_close()

    def _finish_close(self):
        if self.controller is not None and not self.controller.closed.is_set():
            self.window.after(50,self._finish_close)
            return
        self.window.destroy()
        if self.on_closed:
            self.on_closed()
