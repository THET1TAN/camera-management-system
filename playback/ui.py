"""Single responsive archive window. The video HWND is never recreated on resize."""
import calendar
import ctypes
from dataclasses import replace
from datetime import date, datetime, timedelta
import math
import time
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from zoneinfo import ZoneInfo

from .config import ROOT
from .controller import Controller
from .model import Controls, Viewport, day_bounds, local_candidates, layout_mode, parse_time, PlaybackError
from .storage_ui import StoragePanel
from .presentation import DAY_STATES, ACTIONS, COLORS as C, CAMERA_COLORS, STATES, error_text, TEXT, MONTHS, WEEKDAYS
from .widgets import Icons, IconButton, Help, styles




def frame(parent, color=None, **kwargs):
    return tk.Frame(parent, bg=color or C['panel'], **kwargs)


def label(parent, text='', *, secondary=False, size=10, **kwargs):
    return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=C['secondary' if secondary else 'text'],
                    font=('Segoe UI', size), **kwargs)


class PlaybackWindow:
    def __init__(self, parent, camera_id=None, on_closed=None, fixture_directory=None, selected_day=None, day_only=False):
        self.window = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.window.title(TEXT['window_title'])
        self.window.configure(bg=C['background'])
        self.window.geometry('1160x800')
        self.window.minsize(520, 460)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.on_closed = on_closed
        self.fixture_directory = fixture_directory
        self.closing = False
        self.initial_camera = camera_id
        self.initial_day = selected_day
        self.day_only = day_only
        self.configuration_snapshot = ''
        self.diagnostic_snapshot = None
        self.configuration_links = []
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
        self.selection_camera = None
        self.selection_track = ""
        self.selecting = False
        self.selection_drag = None
        self.timeline_fingerprint = None
        self.export_prompt_seen = None
        self.calendar_snapshot = None
        self.controls = Controls()
        self.icons = Icons(self.window)
        styles(self.window)
        self.window.grid_columnconfigure(0, weight=1)
        self.window.grid_rowconfigure(1, weight=1)
        self.footer = label(self.window, TEXT['footer_help'],
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
        self.notice = label(self.window, TEXT['development_notice'],
                            secondary=True, anchor='w')
        self.notice.grid(row=2, column=0, sticky='ew', padx=16, pady=(0,5))
        self.window.bind('<Configure>', self._configure, add='+')
        self.window.bind('<F8>', self._observed_frame, add='+')
        self.start_timer = self.window.after_idle(self._start)

    def button(self, parent, action, command, *, text=False, tip=None):
        return IconButton(parent, action, command, self.icons, self.help, label=text, tip=tip)

    def _observed_frame(self, _event=None):
        if self.controller and self.controller.log:
            recorded = self.controller._native_event('screen-frame-observed', position=self.position,
                                                     evidence='user-keypress-not-automatic')
            self.footer.config(text=TEXT['frame_observation' if recorded else 'frame_observation_unavailable'])

    def _header(self):
        header = frame(self.window, C['background'])
        header.grid(row=0, column=0, sticky='ew', padx=16, pady=(14,12))
        header.grid_columnconfigure(1, weight=1)
        title = label(header, TEXT['recordings'], size=17, anchor='w')
        title.grid(row=0, column=0, columnspan=2, sticky='w')
        self.identity = label(header, TEXT['initializing'], secondary=True, anchor='w')
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
        for panel, title in ((self.nav,TEXT['calendar']), (self.details,TEXT['details_export']), (self.settings_frame,TEXT['settings'])):
            self.tabs.add(panel, text=title)
        self.nav.grid_columnconfigure(0, weight=1)
        self.nav.grid_rowconfigure(5, weight=1)
        label(self.nav, TEXT['active_camera'], secondary=True, size=9, anchor='w').grid(row=0,column=0,sticky='ew',padx=10,pady=(12,3))
        self.camera_choice = ttk.Combobox(self.nav, state='readonly', style='Playback.TCombobox')
        self.camera_choice.grid(row=1,column=0,sticky='ew',padx=10)
        self.camera_choice.bind('<<ComboboxSelected>>', self._camera_changed)
        filter_frame = frame(self.nav)
        filter_frame.grid(row=2,column=0,sticky='ew',padx=10,pady=8)
        label(filter_frame, TEXT['calendar_cameras'], secondary=True).pack(anchor='w')
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
        label(self.nav,TEXT['calendar_legend'],
              secondary=True,size=9,justify='left').grid(row=6,column=0,sticky='w',padx=10,pady=(0,8))
        self._build_calendar()
        self._details_panel()
        self._settings_panel()

    def _video(self):
        self.video = frame(self.area, '#080d14')
        self.video.grid(row=0,column=1,sticky='nsew')
        self.video.grid_rowconfigure(0,weight=1)
        self.video.grid_columnconfigure(0,weight=1)
        self.picture = frame(self.video, '#080d14')
        self.picture.grid(row=0,column=0,sticky='nsew')
        self.surface = frame(self.picture,'#080d14')
        self.surface.place(x=0,y=0,relwidth=1,relheight=1)
        self.status_area = frame(self.video, height=round(76*self.icons.scale))
        self.status_area.grid(row=1,column=0,sticky='ew')
        self.status_area.grid_propagate(False)
        self.status_area.grid_columnconfigure(0,weight=1)
        self.overlay = label(self.status_area,TEXT['choose_camera'],secondary=True,
                             justify='left',anchor='nw',wraplength=420)
        self.overlay.grid(row=0,column=0,sticky='nsew',padx=8,pady=6)
        self.button(self.status_area,'details',lambda:self.show_tab(1)).grid(row=0,column=1,sticky='ne',padx=4,pady=4)

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
        label(g,TEXT['speed']).pack(side='left',padx=(6,4))
        self.rate = ttk.Combobox(g,state='readonly',width=5,values=('0.5×','1×','2×','4×'),style='Playback.TCombobox')
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
        self.help.bind(self.volume,TEXT['volume_help'])
        g = self.groups[2]
        self.button(g,'previous',lambda:self.select_date(self.selected-timedelta(days=1)),tip=TEXT['previous_day']).pack(side='left',padx=2)
        self.clock_entry = ttk.Entry(g,width=10,font=('Segoe UI',10))
        self.clock_entry.insert(0,'12:00:00')
        self.clock_entry.pack(side='left',padx=3)
        self.clock_entry.bind('<Return>',lambda _e:self.goto())
        self.button(g,'goto',self.goto).pack(side='left',padx=2)
        self.button(g,'next',lambda:self.select_date(self.selected+timedelta(days=1)),tip=TEXT['next_day']).pack(side='left',padx=2)

    def _timeline(self):
        self.timeframe = frame(self.area)
        self.timeframe.grid(row=2,column=0,columnspan=2,sticky='ew',pady=(10,0))
        self.timeframe.grid_columnconfigure(0,weight=1)
        bar = frame(self.timeframe)
        bar.grid(row=0,column=0,columnspan=2,sticky='ew',padx=8,pady=5)
        self.time_label = label(bar,'—',size=11)
        self.time_label.pack(side='left',fill='x',expand=True)
        self.selection_button = self.button(bar,'select_range',self.toggle_selection,text=True)
        self.selection_button.pack(side='right',padx=2)
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
        self.timeline_legend = label(self.timeframe,TEXT['timeline_legend'],
                                     secondary=True,size=9,anchor='w',justify='left',wraplength=460)
        self.timeline_legend.grid(row=3,column=0,rowspan=1,columnspan=2,sticky='ew',padx=8,pady=(0,6))

    def _details_panel(self):
        canvas = tk.Canvas(self.details,bg=C['panel'],highlightthickness=0)
        canvas.pack(side='left',fill='both',expand=True)
        outer_scroll = ttk.Scrollbar(self.details,command=canvas.yview)
        outer_scroll.pack(side='right',fill='y')
        canvas.config(yscrollcommand=outer_scroll.set)
        inner = frame(canvas)
        slot = canvas.create_window(0,0,window=inner,anchor='nw')
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(slot,width=e.width))
        inner.bind('<Configure>',lambda _e:canvas.config(scrollregion=canvas.bbox('all')))
        inner.grid_columnconfigure(0,weight=1)
        self.detail_text = tk.Text(inner,wrap='word',width=30,height=8,bg=C['panel'],fg=C['text'],
            font=('Segoe UI',10),bd=0,padx=10,pady=10,state='disabled')
        self.detail_text.grid(row=0,column=0,sticky='nsew')
        scroll = ttk.Scrollbar(inner,command=self.detail_text.yview)
        scroll.grid(row=0,column=1,sticky='ns')
        self.detail_text.config(yscrollcommand=scroll.set)
        box = frame(inner)
        box.grid(row=1,column=0,columnspan=2,sticky='ew',padx=8,pady=8)
        self.original_button = self.button(box,'original',lambda:self.export(False),text=True)
        self.original_button.pack(fill='x',pady=3)
        marks = frame(box)
        marks.pack(fill='x',pady=3)
        for text,which in ((TEXT['mark_a'],'a'),(TEXT['mark_b'],'b')):
            button = tk.Button(marks,text=text,command=lambda w=which:self.mark(w),bg=C['raised'],fg=C['text'],
                activebackground=C['border'],activeforeground=C['text'],bd=0,padx=8,pady=6,takefocus=True)
            button.pack(side='left',fill='x',expand=True,padx=2)
            self.help.bind(button,TEXT['mark_help_prefix']+(TEXT['mark_start'] if which=='a' else TEXT['mark_end'])+TEXT['mark_help_suffix'])
        self.range_label = label(box,TEXT['no_selection'],secondary=True,anchor='w',wraplength=270)
        self.range_label.pack(fill='x',pady=4)
        self.range_inputs = {}
        for name,title in (('start','Start (ISO date/time)'),('end','End (ISO date/time)'),('track','Recording track')):
            label(box,title,secondary=True,anchor='w').pack(fill='x')
            entry = ttk.Entry(box)
            entry.pack(fill='x',pady=(0,3))
            self.range_inputs[name] = entry
        self.button(box,'apply',self.apply_selection,text=True,tip='Apply selection times without seeking').pack(fill='x',pady=3)
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
        self.settings_identity = label(inner, '', anchor='w', wraplength=270)
        self.settings_identity.pack(fill='x',padx=10,pady=8)
        for name,title,values in (
            ('backend',TEXT['setting_backend'],('auto','isapi','videolink')),
            ('zone',TEXT['setting_zone'],('','America/Toronto','UTC','Europe/Paris')),
            ('track',TEXT['setting_track'],None),
            ('revision',TEXT['setting_revision'],None),
            ('time_shift',TEXT['setting_shift'],None),
            ('display_zone',TEXT['setting_display_zone'],('America/Toronto','UTC','Europe/Paris')),
            ('cache_gib',TEXT['setting_quota'],None)):
            label(inner,title,secondary=True,anchor='w').pack(fill='x',padx=10,pady=(12,3))
            widget = ttk.Combobox(inner,values=values,style='Playback.TCombobox') if values else ttk.Entry(inner,font=('Segoe UI',10))
            widget.pack(fill='x',padx=10)
            self.fields[name] = widget
        label(inner,TEXT['settings_help'],secondary=True,justify='left',wraplength=270).pack(fill='x',padx=10,pady=12)
        self.apply_button = self.button(inner,'apply',self.apply_settings,text=True)
        self.apply_button.pack(fill='x',padx=10,pady=8)
        self.storage_panel = StoragePanel(self,inner)

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
        if self.initial_day is not None:
            self.selected = self.initial_day
        if self.controller.fixture_data:
            self.selected = datetime.fromtimestamp(self.controller.fixture_data['recordings'][0]['start'],ZoneInfo(self.zone)).date()
        self.year,self.month = self.selected.year,self.selected.month
        self.ids = [c.camera_id for c in self.controller.cameras]
        values = [f'C{i}' for i in self.ids]
        self.camera_choice.config(values=values)
        for value in values:
            self.filter_list.insert('end',value)
        self.camera_id = self.initial_camera if self.initial_camera in self.ids else (self.ids[0] if self.ids else None)
        if self.camera_id is not None:
            self.camera_choice.set(f'C{self.camera_id}')
        if self.initial_camera in self.ids:
            self.filter_list.selection_set(self.ids.index(self.initial_camera))
        else:
            self.filter_list.selection_set(0,'end')
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
        configuration = self.controller.configuration_status
        self.apply_button.set_enabled(configuration != 'pending', TEXT['applying'])
        if configuration != self.configuration_snapshot:
            self.configuration_snapshot = configuration
            if configuration == 'complete':
                self.zone = self.controller.settings.display_zone
                self.footer.config(text=TEXT['applied'])
                self._load_fields()
            elif configuration:
                self.footer.config(text=TEXT['applying_progress'] if configuration == 'pending' else error_text(configuration))
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
            text += TEXT['received_mib'].format(size=status.received/1048576)
            if status.expected:
                text += TEXT['expected_mib'].format(size=status.expected/1048576)
        elif status.source == 'cache':
            text += TEXT['current_cached_suffix']
        elif status.expected and status.received < status.expected:
            text += TEXT['background_download'].format(received=status.received/1048576, expected=status.expected/1048576)
        if status.reason:
            text += '\n'+error_text(status.reason)
        self.status_message=text
        self.overlay.config(text=text.split('\n')[0]+(' · Details' if status.reason else ''),wraplength=max(160,self.video.winfo_width()-70))
        # Status occupies a permanently reserved row outside the native HWND.
        # Even a last known preview stays unobscured during loading and errors.
        try:
            instant = datetime.fromtimestamp(self.position,ZoneInfo(self.zone))
            shown = instant.strftime('%Y-%m-%d  %H:%M:%S %Z')
        except Exception:
            shown = '—'
        prefix=TEXT['fixture_label'] if self.fixture_directory else TEXT['archive_label']
        self.identity.config(text=f'{prefix} · C{self.camera_id or "—"} · {self.controls.rate:g}× · {shown}')
        self.time_label.config(text=f'{shown}  ·  {self.view.span/60:g} min')
        if status.state in ('PREVIEW','SEEKING','PREVIEW_LOADING'):
            actual = (datetime.fromtimestamp(status.preview_position,ZoneInfo(self.zone)).strftime('%H:%M:%S %Z')
                      if status.preview_position else '—')
            self.time_label.config(text=TEXT['preview_times'].format(requested=shown, actual=actual))
        self.notice.config(text=text.split('\n')[0])
        self.play_button.set_action('play' if self.controls.paused or status.state in ('IDLE','STOPPED','GAP','ERROR','CONFIGURATION','ENDED') else 'pause')
        self.original_button.set_enabled(bool(status.key), TEXT['original_disabled'])
        self.export_button.set_enabled(bool(self.selection_camera and self.selection_track and self.mark_a is not None and self.mark_b is not None and self.mark_b>self.mark_a),
                                       TEXT['export_disabled'])
        self.cancel_button.set_enabled(self.controller.export_request is not None,TEXT['cancel_disabled'])
        export = self.controller.export_status
        self.export_label.config(text={'pending':TEXT['export_pending'],'working':TEXT['export_working'],
            'complete':TEXT['export_complete'],'cancelled':TEXT['export_cancelled']}.get(export,error_text(export) if '-' in export and ' ' not in export else export))
        if self.calendar_snapshot is not self.controller.calendar or self.diagnostic_snapshot is not self.controller.diagnostic_events:
            self.calendar_snapshot = self.controller.calendar
            self.diagnostic_snapshot = self.controller.diagnostic_events
            self._paint_calendar()
            self._show_details()
        self.storage_panel.poll()
        prompt = self.controller.exporter.prompt if self.controller.exporter else None
        if prompt and prompt is not self.export_prompt_seen:
            self.export_prompt_seen = prompt
            missing = '\n'.join(datetime.fromtimestamp(a,ZoneInfo(self.zone)).isoformat()+' → '+datetime.fromtimestamp(b,ZoneInfo(self.zone)).isoformat() for a,b in prompt['gaps'][:12])
            choice = messagebox.askyesnocancel('Missing recordings',
                missing+'\n\nYes: keep elapsed time with neutral “No recording” sections.\nNo: export available portions and list missing intervals.\nCancel: cancel and modify the selection.',parent=self.window)
            self.controller.exporter.decide('neutral' if choice is True else 'available' if choice is False else 'cancel')
        self._draw_timeline()
        self.timer = self.window.after(200,self._poll)

    def selected_ids(self):
        return tuple(self.ids[n] for n in self.filter_list.curselection()) if self.initialized else ()

    def _request_month(self,force=False):
        if self.initialized and not self.closing:
            self.controller.month(self.year,self.month,self.selected_ids(),force,
                                  day=self.selected if self.day_only else None)

    def _build_calendar(self):
        self.help.hide()
        for child in self.calgrid.winfo_children():
            child.destroy()
        self.cells = {}
        self.month_label.config(text=f'{MONTHS[self.month-1]} {self.year}')
        for col,title in enumerate(WEEKDAYS):
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
                    symbol='R' if 'configuration' in kinds else '!' if 'error' in kinds else '…' if 'partial' in kinds else '—' if kinds and all(k=='empty' for k in kinds) else '?'
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
        lines=[getattr(self,'status_message',''), '', self.selected.strftime('%Y-%m-%d')+' · '+self.zone, '', TEXT['availability']]
        configure_ids=[]
        for cid in self.selected_ids():
            info=self.controller.calendar.get((cid,self.selected),{})
            title=DAY_STATES.get(info.get('state','unknown'),TEXT['unknown'])
            lines.append(f'C{cid} · {title}'+(TEXT['cached_suffix'] if info.get('cached') else ''))
            if info.get('checked'):
                lines.append(TEXT['search_time']+datetime.fromtimestamp(info['checked'],ZoneInfo(self.zone)).strftime('%Y-%m-%d %H:%M'))
            if info.get('reason'):
                lines.append('  '+error_text(info['reason']))
            if info.get('reason') == 'timezone-required':
                configure_ids.append(cid)
            for detail in self.controller.diagnostic_events.get(cid, ())[-8:]:
                parts=[str(detail.get('backend','')), str(detail.get('stage',''))]
                for key in ('method','endpoint','http_status','content_type','received','xml_root','xml_namespace',
                            'application_code','application_subcode','application_status','requested_track','returned_track','page_position','count','track_enabled','reason',
                            'python','python_executable_matches_parent'):
                    if key in detail:
                        parts.append(f'{key}={detail[key]}')
                lines.append('  '+' · '.join(parts))
        lines.extend(('', TEXT['python_label']+sys.executable,
                      TEXT['runtime_help']))
        if self.controller.engine and self.controller.engine.runtime:
            runtime=self.controller.engine.runtime
            lines.append(TEXT['native_python']+runtime.get('python','?')+TEXT['same_python']+
                         str(runtime.get('python_executable_matches_parent')))
        if self.day_only:
            lines.append(TEXT['day_only'])
        lines.extend(('', TEXT['badge_help'],
            TEXT['unknown_duration_help'],
            TEXT['cache_help'], '',
            TEXT['audio_help'],
            TEXT['export_help']))
        if self.icons.missing:
            lines.append(TEXT['missing_icons']+', '.join(sorted(self.icons.missing)))
        self.detail_text.config(state='normal')
        for button in self.configuration_links:
            button.destroy()
        self.configuration_links=[]
        self.detail_text.delete('1.0','end')
        for cid in configure_ids:
            button=self.button(self.detail_text,'settings',lambda c=cid:self.configure_camera(c),text=True,
                               tip=TEXT['configure_zone_tip'].format(cid=cid))
            button.config(text=TEXT['configure_zone'].format(cid=cid))
            self.detail_text.window_create('end',window=button)
            self.detail_text.insert('end','\n')
            self.configuration_links.append(button)
        self.detail_text.insert('end','\n'.join(lines))
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
        if self.day_only:
            self._request_month()

    def change_month(self,delta):
        total=self.year*12+self.month-1+delta
        self.year,self.month=total//12,total%12+1
        if self.day_only:
            self.selected=date(self.year,self.month,min(self.selected.day,calendar.monthrange(self.year,self.month)[1]))
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
        if self.day_only and self.camera_id in self.ids:
            self.filter_list.selection_clear(0,'end')
            self.filter_list.selection_set(self.ids.index(self.camera_id))
            self._request_month()
        if self.controller and self.controller.request is not None:
            self.seek(self.position)

    def seek(self,stamp):
        if self.controller and self.initialized and self.camera_id is not None:
            self.position=stamp
            self.controller.seek(self.camera_id,stamp)

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
            self.footer.config(text=TEXT['time_format'])

    def jump(self,seconds):
        self.seek(self.position+seconds)

    def play_pause(self):
        status=self.controller.view if self.controller else None
        if status is None:
            return
        if status.state in ('IDLE','STOPPED','GAP','ERROR','CONFIGURATION','ENDED','FAILED'):
            self.controls=replace(self.controls,paused=False)
            self.controller.set_controls(self.controls)
            self.seek(self.position)
        else:
            self.controls=replace(self.controls,paused=not self.controls.paused)
            self.controller.set_controls(self.controls)

    def stop(self):
        self.dragging=False
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
            self.footer.config(text=TEXT['next_jump'].format(seconds=target-self.position))
            self.seek(target)
        else:
            self.footer.config(text=TEXT['no_next_recording'])

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

    def _draw_base(self):
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
        canvas.config(scrollregion=(0,0,width+58,height))
        a,b=day_bounds(self.selected,self.zone)
        self.horizontal.set(max(0,min(1,(self.view.start-a)/(b-a))),max(0,min(1,(end-a)/(b-a))))

    def _draw_timeline(self):
        if not hasattr(self,'timeline'):
            return
        began = time.monotonic()
        canvas=self.timeline
        ids=tuple(self.selected_ids())
        entries=self.controller.entries if self.controller else ()
        playlist=self.controller.active_playlist if self.controller else None
        fingerprint=(self.view,canvas.winfo_width(),ids,id(entries),self.zone,
                     tuple((key,len(parts)) for key,parts in playlist.groups) if playlist else ())
        rebuilt=fingerprint != self.timeline_fingerprint
        if rebuilt:
            self._draw_base()
            self.timeline_fingerprint=fingerprint
        canvas.delete('cursor')
        canvas.delete('selection')
        width=max(1,canvas.winfo_width()-58)
        end=self.view.start+self.view.span
        height=38+28*max(1,len(ids))
        if self.selection_camera in ids and self.mark_a is not None and self.mark_b is not None:
            y=28+ids.index(self.selection_camera)*28
            a,b=sorted((self.mark_a,self.mark_b))
            x1=58+max(0,min(1,(a-self.view.start)/self.view.span))*width
            x2=58+max(0,min(1,(b-self.view.start)/self.view.span))*width
            if a<end and b>self.view.start:
                canvas.create_rectangle(x1,y,x2,y+22,outline='#e9bc73',fill='#e9bc73',stipple='gray25',width=2,tags='selection')
        for stamp,color,letter in ((self.position,C['text'],''),(self.mark_a,'#e9bc73','A'),(self.mark_b,'#e9bc73','B')):
            if stamp is not None and self.view.start<=stamp<=end:
                x=58+(stamp-self.view.start)/self.view.span*width
                canvas.create_line(x,23,x,height,fill=color,width=3 if letter else 2,tags='selection' if letter else 'cursor')
                if letter:
                    canvas.create_text(x+7,height-8,text=letter,fill=color,tags='selection')
        if self.controller and self.controller.log and time.monotonic()-getattr(self,'draw_logged',0)>2:
            self.draw_logged=time.monotonic()
            self.controller.log.event('timeline-render',camera_id=self.camera_id,session_id=self.controller.active_request[0] if self.controller.active_request else 0,elapsed=time.monotonic()-began,mode='base' if rebuilt else 'cursor')

    def toggle_selection(self):
        self.selecting=not self.selecting
        self.selection_button.config(text="Select range: ON" if self.selecting else "Select range")
        self.footer.config(text='Selection mode: drag a range or an A/B handle. Playback does not move.' if self.selecting else 'Scrub mode: drag to preview; release to seek.')
        self.timeline.config(cursor='crosshair' if self.selecting else '')

    def _bind_selection(self):
        if self.selection_camera == self.camera_id and self.selection_track:
            return
        if self.selection_camera != self.camera_id:
            self.mark_a=self.mark_b=None
        self.selection_camera=self.camera_id
        tracks=sorted({e.recording.track for e in self.controller.entries if e.recording.camera_id==self.camera_id}) if self.controller else []
        configured=self.controller.settings.cameras.get(str(self.camera_id),{}).get('track','') if self.controller and self.controller.settings else ''
        self.selection_track=configured or (tracks[0] if len(tracks)==1 else '')
        if self.controller and self.controller.view.camera_id==self.camera_id and self.controller.view.track:
            self.selection_track=self.controller.view.track

    def _drag_start(self,event):
        self.timeline.focus_set()
        ids=self.selected_ids()
        row=int((self.timeline.canvasy(event.y)-28)//28)
        if 0<=row<len(ids):
            self.camera_id=ids[row]
            self.camera_choice.set(f'C{self.camera_id}')
        if self.selecting:
            self._bind_selection()
            width=max(1,self.timeline.winfo_width()-58)
            stamp=self.view.at(event.x-58,width)
            near=lambda value:value is not None and abs((value-stamp)/self.view.span*width)<10
            if near(self.mark_a):
                self.selection_drag=('a',stamp,self.mark_a,self.mark_b)
            elif near(self.mark_b):
                self.selection_drag=('b',stamp,self.mark_a,self.mark_b)
            elif self.mark_a is not None and self.mark_b is not None and self.mark_a<stamp<self.mark_b:
                self.selection_drag=('move',stamp,self.mark_a,self.mark_b)
            else:
                self.mark_a=self.mark_b=stamp
                self.selection_drag=('b',stamp,stamp,stamp)
            self._drag(event)
            return
        self.dragging=True
        self._drag(event)

    def _drag(self,event):
        stamp=self.view.at(event.x-58,max(1,self.timeline.winfo_width()-58))
        if getattr(self,'selection_drag',None):
            mode,anchor,a,b=self.selection_drag
            if mode=='move':
                self.mark_a,self.mark_b=a+stamp-anchor,b+stamp-anchor
            elif mode=='a':
                self.mark_a=stamp
            else:
                self.mark_b=stamp
            self._show_selection()
        else:
            self.position=stamp
            self.pointer_at=time.monotonic()
            if self.seek_timer is None:
                self.seek_timer=self.window.after(125,self._preview_seek)
        self._draw_timeline()

    def _preview_seek(self):
        self.seek_timer=None
        if self.dragging and not self.closing and self.controller and self.initialized and self.camera_id is not None:
            self.controller.seek(self.camera_id,self.position,preview=True)
            if self.controller.log:
                self.controller.log.event('pointer-target',camera_id=self.camera_id,session_id=self.controller.active_request[0] if self.controller.active_request else 0,position=self.position,elapsed=time.monotonic()-getattr(self,'pointer_at',time.monotonic()))

    def _drag_end(self,event):
        self._drag(event)
        if getattr(self,'selection_drag',None):
            self.mark_a,self.mark_b=sorted((self.mark_a,self.mark_b))
            self.selection_drag=None
            self._show_selection()
            return
        self.dragging=False
        self._commit_seek()

    def _commit_seek(self):
        if self.seek_timer is not None:
            self.window.after_cancel(self.seek_timer)
            self.seek_timer=None
        self.seek(self.position)

    def _show_selection(self):
        show=lambda stamp:datetime.fromtimestamp(stamp,ZoneInfo(self.zone)).isoformat(timespec='milliseconds') if stamp is not None else ''
        self.range_label.config(text=f'C{self.selection_camera or "—"} · Track {self.selection_track or "choose"} · Duration {abs((self.mark_b or 0)-(self.mark_a or 0)):.3f} s' if self.mark_a is not None and self.mark_b is not None else 'Set start and end.')
        for name,value in (('start',show(self.mark_a)),('end',show(self.mark_b)),('track',self.selection_track)):
            self.range_inputs[name].delete(0,'end')
            self.range_inputs[name].insert(0,value)

    def apply_selection(self):
        try:
            a=parse_time(self.range_inputs['start'].get(),self.zone)
            b=parse_time(self.range_inputs['end'].get(),self.zone)
            track=self.range_inputs['track'].get().strip()
            if a>=b or not track:
                raise ValueError
            self.mark_a,self.mark_b=a,b
            self.selection_camera=self.selection_camera or self.camera_id
            self.selection_track=track
            self._show_selection()
            self._draw_timeline()
        except (ValueError,PlaybackError):
            self.footer.config(text='Use increasing ISO date/times with UTC offsets and a recording track.')

    def mark(self,which):
        self._bind_selection()
        if which=='a':
            self.mark_a=self.position
        else:
            self.mark_b=self.position
        self._show_selection()
        self._draw_timeline()

    def export(self,selection):
        if not self.controller or (not selection and not self.controller.view.key):
            return
        stamp=datetime.fromtimestamp(self.mark_a if selection else self.position,ZoneInfo(self.zone)).strftime('%Y%m%d-%H%M%S')
        cid=self.selection_camera if selection else self.camera_id
        name=f'C{cid}-{stamp}'+('-selection.mp4' if selection else '-original.bin')
        path=filedialog.asksaveasfilename(parent=self.window,title='Export selection — Precise (re-encoded)' if selection else TEXT['save_original'],
            initialfile=name,defaultextension='.mp4' if selection else '.bin')
        if path:
            try:
                if selection:
                    self.controller.export_selection(path,cid,self.selection_track,self.mark_a,self.mark_b)
                else:
                    self.controller.export(path)
            except PlaybackError as exc:
                self.footer.config(text=error_text(exc.code))

    def _load_fields(self):
        if not self.initialized:
            return
        settings=self.controller.settings
        local=settings.cameras.get(str(self.camera_id),{})
        self.settings_identity.config(text=TEXT['settings_identity'].format(cid=self.camera_id))
        defaults={'backend':'auto','zone':'','track':'','revision':'','time_shift':0,
                  'display_zone':settings.display_zone,'cache_gib':settings.cache_gib}
        for name,widget in self.fields.items():
            widget.delete(0,'end')
            widget.insert(0,str(local.get(name,defaults[name])))

    def configure_camera(self, camera_id):
        self.stop()
        self.camera_id=camera_id
        self.camera_choice.set(f'C{camera_id}')
        if self.day_only and camera_id in self.ids:
            self.filter_list.selection_clear(0,'end')
            self.filter_list.selection_set(self.ids.index(camera_id))
        self._load_fields()
        self.show_tab(2)
        self.fields['zone'].focus_set()

    def apply_settings(self):
        if self.fixture_directory:
            self.footer.config(text=TEXT['fixture_settings'])
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
            self.controller.apply_settings(settings,self.camera_id,self.selected)
            self.footer.config(text=TEXT['apply_search'])
        except Exception:
            self.footer.config(text=TEXT['invalid_settings'])

    def toggle_panel(self):
        self.panel_open=not self.panel_open
        self._layout()

    def show_tab(self,index):
        self.panel_open=True
        if index==1:
            self._show_details()
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
        self.timeline_legend.config(wraplength=max(200,width-48))
        self.identity.config(wraplength=max(200,width-180))
        self.time_label.config(wraplength=max(180,width-160),justify='left')
        wide=self.mode in ('wide','medium')
        self.area.grid_columnconfigure(0,weight=0,minsize=0)
        self.area.grid_rowconfigure(3,weight=0,minsize=0)
        # Video stays mapped throughout. Only its grid coordinates/spans change.
        self.video.grid_configure(row=0,column=1 if wide else 0,columnspan=1 if wide else 2)
        self.commands.grid_configure(row=1,column=1 if wide else 0,columnspan=1 if wide else 2)
        if wide:
            self.side.place_forget()
            self.side.grid(row=0,column=0,rowspan=2,columnspan=1,sticky='nsew',padx=(0,10))
            self.side.configure(width=round(320*self.icons.scale))
        else:
            self.side.grid_remove()
            if self.panel_open:
                # Compact navigation has its own grid row, outside the image.
                self.side.configure(height=min(round(250*self.icons.scale), max(120,height//3)))
                self.side.grid(row=3,column=0,rowspan=1,columnspan=2,sticky='ew',pady=(8,0))
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
