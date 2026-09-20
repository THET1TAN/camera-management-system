"""Storage controls; all scans and deletions are delegated to Store's worker."""
from dataclasses import replace
import tkinter as tk
from tkinter import ttk, messagebox


class StoragePanel:
    def __init__(self, window, parent):
        self.window = window
        self.values = {}
        ttk.Separator(parent).pack(fill='x',padx=10,pady=12)
        ttk.Label(parent,style='Playback.TLabel',text='Storage & Cache').pack(anchor='w',padx=10)
        self.summary = ttk.Label(parent,style='Playback.TLabel',text='Waiting for cache…',wraplength=270,justify='left')
        self.summary.pack(fill='x',padx=10,pady=8)
        for name,title in (('cache_gib','Media limit (GiB)'),('ttl_days','Unused retention (days)'),
                           ('free_gib','Free disk reserve (GiB)'),('cleanup_high','Preventive threshold (%)'),
                           ('cleanup_low','Cleanup target (%)')):
            ttk.Label(parent,style='Playback.TLabel',text=title).pack(anchor='w',padx=10)
            widget=ttk.Entry(parent)
            widget.pack(fill='x',padx=10,pady=(0,5))
            self.values[name]=widget
        window.button(parent,'storage_apply',self.apply,text=True).pack(fill='x',padx=10,pady=4)
        window.button(parent,'clean_cache',lambda:self.clean(None),text=True).pack(fill='x',padx=10,pady=4)
        window.button(parent,'clear_camera',lambda:self.clean(window.camera_id),text=True).pack(fill='x',padx=10,pady=4)
        self.initialized=False
        self.seen=None

    def clean(self,camera):
        c=self.window.controller
        if c and c.store:
            c.store.request_cleanup(camera)

    def apply(self):
        c=self.window.controller
        if not c or not c.settings:
            return
        try:
            values={k:float(w.get()) for k,w in self.values.items()}
            values['ttl_days']=int(values['ttl_days'])
            values['cleanup_high']/=100
            values['cleanup_low']/=100
            if not (max(1,c.settings.max_archive_gib)<=values['cache_gib']<=1024 and
                    1<=values['ttl_days']<=365 and .25<=values['free_gib']<=100 and
                    0<values['cleanup_low']<values['cleanup_high']<=1):
                raise ValueError
            c.storage_request=values
            self.window.footer.config(text='Saving storage settings…')
        except ValueError:
            self.window.footer.config(text='Check the storage limits, retention and percentages.')

    def poll(self):
        c=self.window.controller
        if not c or not c.store:
            return
        if not self.initialized:
            for k,w in self.values.items():
                value=getattr(c.settings,k)
                w.insert(0,str(value*100 if k.startswith('cleanup_') else value))
            self.initialized=True
        s=c.store.cache_snapshot
        if s:
            gib=lambda value:f'{value/1024**3:.2f} GiB'
            last=s.get('last_result') or {}
            self.summary.config(text=(f"Media: {gib(s['media'])} / {gib(s['limit'])}\n"
                f"Index/logs: {gib(s['other'])} · Total: {gib(s['total'])}\n"
                f"Protected: {gib(s['protected'])} · Reclaimable: {gib(s['reclaimable'])}\n"
                f"Reserved: {gib(s['reserved'])} · Disk free: {gib(s['free'])}\n"
                f"Retention: {s['retention']} days · Free reserve: {s['margin']:g} GiB\n"
                f"Uncertain paths preserved: {s['uncertain']}\n"
                f"Last cleanup: {s['last_cleanup'] or 'None'}\n"
                f"Last result: {gib(last.get('freed',0))} freed, {last.get('errors',0)} deferred/errors\n{s['path']}"))
        result=c.store.cache_result
        if result is None or result is self.seen:
            return
        self.seen=result
        if result['kind']=='plan':
            scope='all cameras' if result['camera'] is None else f"C{result['camera']}"
            message=(f"Remove {len(result['candidates'])} unused local cache entries for {scope}?\n"
                     f"Estimated space: {result['estimated']/1024**3:.2f} GiB.\n"
                     f"Protected media: {result['protected']/1024**3:.2f} GiB.\n"
                     'Active files and incident evidence are protected. Camera recordings and saved exports are untouched. '
                     'The local copy may be unavailable on the camera: use Download original first to keep it.')
            if messagebox.askyesno('Review cache cleanup',message,parent=self.window.window):
                c.store.request_cleanup(plan=result)
        elif result['kind']=='result':
            self.window.footer.config(text=f"Freed {result['freed']/1048576:.1f} MiB · "
                f"{result['protected']} entries protected · {result['errors']} deferred/errors")
        else:
            self.window.footer.config(text='Cache maintenance deferred; existing files are preserved.')
