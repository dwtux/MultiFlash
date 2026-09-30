import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from imagemap import human_size

BAR_WIDTH = 50


PLACEHOLDER = 'No default image selected. Click Browse to choose a new default.'


class FlashApp(tk.Tk):
    def __init__(self, default_image: str | None, set_default_image: Callable,
                 project_dir: str, extracted_dir: str,
                 get_devices: Callable, get_device_size: Callable,
                 flash_device: Callable, extract_device: Callable,
                 get_image_disk_usage: Callable, eject_device: Callable,
                 verify_device: Callable, shrink_image: Callable,
                 log_flash: Callable,
                 is_compressed: Callable, uncompressed_size: Callable):
        super().__init__()
        self.title('MultiFlash')
        self.minsize(800, 480)
        self.geometry('900x760')

        self._default_image       = default_image
        self._set_default_image   = set_default_image
        self._project_dir         = project_dir
        self._extracted_dir       = extracted_dir
        self._get_devices         = get_devices
        self._get_device_size     = get_device_size
        self._flash_device        = flash_device
        self._extract_device      = extract_device
        self._get_image_disk_usage = get_image_disk_usage
        self._eject_device        = eject_device
        self._verify_device       = verify_device
        self._shrink_image        = shrink_image
        self._log_flash           = log_flash
        self._is_compressed       = is_compressed
        self._uncompressed_size   = uncompressed_size

        self._image_path  = tk.StringVar(value=default_image or '')
        self._source      = tk.StringVar(value='default')
        self._flash_mode  = tk.StringVar(value='sequential')
        self._verify_after = tk.BooleanVar(value=True)
        self._queue: queue.Queue = queue.Queue()
        self._progress_rows: dict = {}
        self._cancel_events: dict = {}
        self._operation = 'flash'
        self._verify_results: dict = {}

        self._build_image_selector()
        self._build_device_list()
        self._build_progress()
        self._refresh_devices()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_image_selector(self) -> None:
        img_frame = ttk.LabelFrame(self, text='Image File')
        img_frame.pack(fill='x', padx=10, pady=10)

        radio_frame = ttk.Frame(img_frame)
        radio_frame.pack(fill='x', padx=6, pady=(6, 2))
        ttk.Radiobutton(radio_frame, text='Default image',
                        variable=self._source, value='default',
                        command=self._on_source_change).pack(side='left', padx=(0, 16))
        ttk.Radiobutton(radio_frame, text='Custom image',
                        variable=self._source, value='custom',
                        command=self._on_source_change).pack(side='left')
        ttk.Button(radio_frame, text='Details',
                   command=self._open_details).pack(side='right')

        path_frame = ttk.Frame(img_frame)
        path_frame.pack(fill='x', padx=6, pady=(2, 6))
        self._path_entry = ttk.Entry(path_frame, textvariable=self._image_path,
                                     width=70, state='disabled')
        self._path_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))
        self._browse_btn = ttk.Button(path_frame, text='Browse…',
                                      command=self._browse_image)
        self._browse_btn.pack(side='left')

        self._update_entry_style()

    def _build_device_list(self) -> None:
        dev_frame = ttk.LabelFrame(self, text='SD Card Devices')
        dev_frame.pack(fill='both', expand=True, padx=10, pady=(0, 10))

        columns = ('path', 'size', 'model', 'extract')
        self._tree = ttk.Treeview(dev_frame, columns=columns, height=14,
                                  show='headings', selectmode='extended')
        self._tree.heading('path',    text='Device')
        self._tree.heading('size',    text='Size')
        self._tree.heading('model',   text='Model')
        self._tree.heading('extract', text='')
        self._tree.column('path',    width=120)
        self._tree.column('size',    width=80,  anchor='center')
        self._tree.column('model',   width=200)
        self._tree.column('extract', width=100, anchor='center', stretch=False)
        self._tree.bind('<ButtonRelease-1>', self._on_tree_click)
        self._tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        # Slots with no card report 0B: shown, but dimmed and not selectable.
        self._tree.tag_configure('empty', foreground='#9a9a9a')

        scrollbar = ttk.Scrollbar(dev_frame, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        scrollbar.pack(side='left', fill='y', pady=6, padx=(0, 6))

        btn_frame = ttk.Frame(dev_frame)
        btn_frame.pack(side='right', anchor='n', padx=6, pady=6)
        ttk.Button(btn_frame, text='↺  Refresh',
                   command=self._refresh_devices).pack(fill='x', pady=(0, 4))
        ttk.Radiobutton(btn_frame, text='Sequential',
                        variable=self._flash_mode, value='sequential').pack(anchor='w')
        ttk.Radiobutton(btn_frame, text='Concurrent',
                        variable=self._flash_mode, value='concurrent').pack(anchor='w')
        ttk.Checkbutton(btn_frame, text='Verify after',
                        variable=self._verify_after).pack(anchor='w', pady=(4, 4))
        self._flash_btn = ttk.Button(btn_frame, text='Flash', command=self._flash)
        self._flash_btn.pack(fill='x')
        self._verify_btn = ttk.Button(btn_frame, text='Verify', command=self._verify)
        self._update_verify_button()

    def _build_progress(self) -> None:
        self._progress_frame = ttk.LabelFrame(self, text='Progress')
        self._progress_frame.pack(fill='x', padx=10, pady=(0, 10))

    def _add_progress_row(self, device: str) -> None:
        row = ttk.Frame(self._progress_frame)
        row.pack(fill='x', padx=6, pady=3)

        ttk.Label(row, text=device, width=16, anchor='w').pack(side='left', padx=(0, 6))

        pct_var = tk.DoubleVar(value=0)
        ttk.Progressbar(row, variable=pct_var, maximum=100, length=300).pack(
            side='left', padx=(0, 6))

        speed_label = ttk.Label(row, text='', width=16, anchor='w')
        speed_label.pack(side='left')

        # Shown (packed) only while this device is verifying; see _show_cancel.
        cancel_btn = ttk.Button(row, text='Cancel', width=11)

        self._progress_rows[device] = (pct_var, speed_label, row, cancel_btn)

    def _show_cancel(self, device: str) -> None:
        """Reveal the per-device Cancel button and wire it to its cancel event."""
        if device not in self._progress_rows:
            return
        cancel_btn = self._progress_rows[device][3]
        event = self._cancel_events.get(device)
        if event is None:
            return

        def do_cancel():
            event.set()
            cancel_btn.configure(state='disabled', text='Cancelling…')

        cancel_btn.configure(text='Cancel', state='normal', command=do_cancel)
        cancel_btn.pack(side='left', padx=(6, 0))

    def _clear_progress_rows(self) -> None:
        for _, _, row, _ in self._progress_rows.values():
            row.destroy()
        self._progress_rows.clear()
        self._cancel_events.clear()

    # ── Callbacks ──────────────────────────────────────────────────────────────

    def _update_entry_style(self) -> None:
        if self._source.get() == 'default' and not self._default_image:
            self._path_entry.configure(foreground='grey')
            self._image_path.set(PLACEHOLDER)
        elif self._source.get() == 'default':
            self._path_entry.configure(foreground='')
            self._image_path.set(self._default_image)

    def _on_source_change(self) -> None:
        if self._source.get() == 'default':
            self._path_entry.configure(state='disabled')
            self._update_entry_style()
        else:
            self._image_path.set('')
            self._path_entry.configure(state='normal', foreground='')
            self._browse_btn.configure(text='Browse…')
        self._update_verify_button()

    def _update_verify_button(self) -> None:
        """Show Verify only for a raw default image (verify needs the raw .img)."""
        if (self._source.get() == 'default' and self._default_image
                and not self._is_compressed(self._default_image)):
            self._verify_btn.pack(fill='x', pady=(4, 0))
        else:
            self._verify_btn.pack_forget()

    def _browse_image(self) -> None:
        current = self._image_path.get().strip()
        initialdir = (os.path.dirname(current)
                      if current and os.path.isfile(current) else self._project_dir)
        path = filedialog.askopenfilename(
            title='Select Image',
            initialdir=initialdir,
            filetypes=[('Image files', '*.img *.img.gz *.img.xz *.gz *.xz *.zip'),
                       ('All files', '*.*')],
        )
        if not path:
            return
        if self._source.get() == 'default':
            self._default_image = path
            self._set_default_image(path)
            self._path_entry.configure(foreground='')
            self._browse_btn.configure(text='Browse…')
            self._update_verify_button()
        self._image_path.set(path)

    def _is_empty_slot(self, row_id: str) -> bool:
        return 'empty' in self._tree.item(row_id)['tags']

    def _on_tree_select(self, _event=None) -> None:
        """Keep empty slots out of the selection.

        Treeview has no per-row disable, so instead of blocking the click we
        undo it. The selection_remove re-fires this handler once, which then
        finds nothing to drop and stops.
        """
        empty = [iid for iid in self._tree.selection() if self._is_empty_slot(iid)]
        if empty:
            self._tree.selection_remove(*empty)

    def _on_tree_click(self, event) -> None:
        col    = self._tree.identify_column(event.x)
        row_id = self._tree.identify_row(event.y)
        if row_id and self._is_empty_slot(row_id):
            return
        if col == '#4' and row_id:
            values = self._tree.item(row_id)['values']
            self._open_extract(str(values[0]), str(values[1]))

    def _open_details(self) -> None:
        image = self._image_path.get().strip()
        if not image or not os.path.isfile(image):
            messagebox.showerror('No Image', 'Please select a valid image file first.')
            return
        if self._is_compressed(image):
            messagebox.showinfo(
                'Details Unavailable',
                'Image Details are available for raw .img files only, '
                'not compressed images.')
            return

        win = tk.Toplevel(self)
        win.title('Image Details')
        win.minsize(480, 220)
        win.resizable(False, False)

        text = tk.Text(win, font=('Monospace', 11), state='disabled',
                       bg=self.cget('bg'), relief='flat', height=6, width=58)
        text.tag_configure('used',    foreground='#4caf50')
        text.tag_configure('free',    foreground='#555555')
        text.tag_configure('unalloc', foreground='#e0a030')
        text.tag_configure('label',   foreground='#aaaaaa')
        text.pack(padx=16, pady=16)

        def _set(content: list[tuple[str, str]]) -> None:
            text.configure(state='normal')
            text.delete('1.0', 'end')
            for tag, chunk in content:
                text.insert('end', chunk, tag)
            text.configure(state='disabled')

        status_var = tk.StringVar()
        ttk.Label(win, textvariable=status_var, foreground='grey').pack(padx=16)

        def fmt(b):
            for unit in ('B', 'KB', 'MB', 'GB'):
                if b < 1024:
                    return f'{b:.1f} {unit}'
                b /= 1024
            return f'{b:.1f} TB'

        def load_disk_usage():
            _set([('label', 'Loading…')])

            def worker():
                result = self._get_image_disk_usage(image)
                if result is None:
                    win.after(0, lambda: _set([('label', 'Could not read image disk usage.')]))
                    return
                file_size = result['file_size']
                part_size = result['part_size']
                fs_used   = result['fs_used']
                fs_total  = result['fs_total']
                pct       = fs_used / fs_total if fs_total else 0
                # Space inside the root partition the filesystem doesn't occupy: what
                # PiShrink reclaims. df only sees inside the filesystem, so it misses this.
                unused    = max(part_size - fs_total, 0)

                # Bar spans the root partition: used | free-in-fs | unused partition space
                used_w   = min(round(BAR_WIDTH * fs_used / part_size), BAR_WIDTH) if part_size else 0
                free_w   = round(BAR_WIDTH * max(fs_total - fs_used, 0) / part_size) if part_size else 0
                free_w   = min(free_w, BAR_WIDTH - used_w)
                unused_w = BAR_WIDTH - used_w - free_w
                bar = [('used', '█' * used_w), ('free', '░' * free_w), ('unalloc', '▒' * unused_w)]

                stats = [
                    ('label', f'\nFilesystem:  {fmt(fs_used)} used / {fmt(fs_total)}  ({pct*100:.1f}%)'),
                    ('label', f'\nPartition:   {fmt(part_size)}'),
                    ('label', f'\nImage file:  {fmt(file_size)}'),
                ]
                if unused_w > 0:
                    stats.append(
                        ('unalloc', f'\nUnused:      {fmt(unused)}  (partition space PiShrink reclaims)'))
                win.after(0, lambda: _set(bar + stats))

            threading.Thread(target=worker, daemon=True).start()

        def run_pishrink():
            shrink_btn.configure(state='disabled')
            status_var.set('Running PiShrink…')

            def worker():
                ok, err = self._shrink_image(image)
                if ok:
                    win.after(0, lambda: status_var.set('PiShrink complete.'))
                    win.after(0, load_disk_usage)
                else:
                    win.after(0, lambda: status_var.set(f'PiShrink failed: {err}'))
                win.after(0, lambda: shrink_btn.configure(state='normal'))

            threading.Thread(target=worker, daemon=True).start()

        shrink_btn = ttk.Button(win, text='Run PiShrink', command=run_pishrink)
        shrink_btn.pack(padx=16, pady=(0, 16))

        load_disk_usage()

    def _open_extract(self, device: str, size: str) -> None:
        try:
            size_bytes = self._get_device_size(device)
            size_gb    = size_bytes / (1024 ** 3)
        except Exception:
            size_bytes = 0
            size_gb    = 0

        win = tk.Toplevel(self)
        win.title(f'Extract Image - {device}')
        win.resizable(False, False)

        pad = dict(padx=16, pady=4)

        ttk.Label(win, text=f'Device:   {device}  ({size})').pack(anchor='w', **pad)
        ttk.Label(win, text=f'Save to:  {self._extracted_dir}/').pack(anchor='w', **pad)

        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=16, pady=6)

        warning = f'⚠  Requires ~{size_gb:.1f} GB of free disk space before compression'
        ttk.Label(win, text=warning, foreground='red').pack(anchor='w', **pad)

        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=16, pady=6)

        device_name  = device.split('/')[-1]
        default_name = f'{device_name}_{datetime.now().strftime("%Y-%m-%d")}'
        filename_var = tk.StringVar(value=default_name)

        name_frame = ttk.Frame(win)
        name_frame.pack(fill='x', padx=16, pady=4)
        ttk.Label(name_frame, text='Filename:').pack(side='left', padx=(0, 8))
        ttk.Entry(name_frame, textvariable=filename_var, width=30).pack(side='left')
        ttk.Label(name_frame, text='.img(.gz)', foreground='grey').pack(side='left', padx=(4, 0))

        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=16, pady=6)

        shrink_var   = tk.BooleanVar(value=True)
        compress_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(win, text='Shrink with PiShrink', variable=shrink_var).pack(
            anchor='w', padx=16, pady=2)
        ttk.Checkbutton(win, text='Compress to .img.gz',  variable=compress_var).pack(
            anchor='w', padx=16, pady=2)

        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=16, pady=6)

        btn_frame = ttk.Frame(win)
        btn_frame.pack(padx=16, pady=(0, 12))
        ttk.Button(btn_frame, text='Cancel', command=win.destroy).pack(side='left', padx=(0, 8))
        ttk.Button(btn_frame, text='Start',
                   command=lambda: self._start_extract(
                       win, device, filename_var.get().strip(), shrink_var.get(), compress_var.get()
                   )).pack(side='left')

    def _start_extract(self, win: tk.Toplevel, device: str, filename: str,
                       shrink: bool, compress: bool) -> None:
        if not filename:
            messagebox.showerror('No Filename', 'Please enter a filename.', parent=win)
            return
        win.destroy()
        self._operation = 'extract'
        self._flash_btn.configure(state='disabled')
        self._verify_btn.configure(state='disabled')
        self._clear_progress_rows()
        self._add_progress_row(device)
        self._remaining = 1

        def worker():
            def progress_cb(pct, speed):
                self._queue.put(('progress', device, pct, speed))
            ok, result = self._extract_device(device, filename, shrink, compress, progress_cb)
            self._queue.put(('done', device, ok, False, result))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_queue()

    def _refresh_devices(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for dev in self._get_devices():
            empty = dev['size'].strip() in ('0B', '0')
            self._tree.insert('', 'end',
                              values=(dev['path'], dev['size'], dev['model'],
                                      '' if empty else 'Extract ↓'),
                              tags=('empty',) if empty else ())

    def _flash(self) -> None:
        image = self._image_path.get().strip()
        if not image:
            messagebox.showerror('No Image', 'Please select an image file first.')
            return
        if not os.path.isfile(image):
            messagebox.showerror('File Not Found', f'Image not found:\n{image}')
            return

        selected = [self._tree.item(iid)['values'][0] for iid in self._tree.selection()]
        if not selected:
            messagebox.showerror('No Device', 'Please select at least one device.')
            return

        # For a compressed image, the card must fit the *uncompressed* size; if
        # that can't be determined (e.g. .gz), skip the check rather than guess.
        image_size = (self._uncompressed_size(image) or 0) if self._is_compressed(image) \
            else os.path.getsize(image)
        too_small = []
        for device in selected:
            try:
                dsize = self._get_device_size(str(device))
            except Exception:
                continue
            if dsize < image_size:
                too_small.append((device, dsize))
        if too_small:
            lines = '\n'.join(f'  {d}  ({human_size(sz)})' for d, sz in too_small)
            messagebox.showerror(
                'Card Too Small',
                f'The image needs {human_size(image_size)}, but these card(s) are '
                f'smaller:\n\n{lines}')
            return

        targets = '\n'.join(f'  {d}' for d in selected)
        if not messagebox.askyesno(
            'Confirm', f'This will erase all data on:\n\n{targets}\n\nContinue?'
        ):
            return

        self._operation = 'flash'
        self._flash_btn.configure(state='disabled')
        self._verify_btn.configure(state='disabled')
        self._clear_progress_rows()
        for device in selected:
            self._add_progress_row(device)
        self._remaining = len(selected)

        verify_after  = self._verify_after.get()
        is_default    = self._source.get() == 'default'
        selected_info = {self._tree.item(iid)['values'][0]: self._tree.item(iid)['values'][1]
                         for iid in self._tree.selection()}

        def flash_one(device):
            def progress_cb(pct, speed):
                self._queue.put(('progress', device, pct, speed))
            start = time.monotonic()
            ok, err = self._flash_device(image, device, progress_cb)
            if ok and verify_after and not self._is_compressed(image):
                event = threading.Event()
                self._cancel_events[device] = event
                self._queue.put(('verifying', device))
                self._queue.put(('progress', device, 0, 'Verifying…'))
                result = self._verify_device(image, device, progress_cb, event.is_set)
                if result.status == 'cancelled':
                    ok = True            # flash already succeeded; verify was skipped
                else:
                    ok = result.status in ('match', 'benign')
                    if not ok:
                        err = f'Verification failed: {result.detail}'
            duration = time.monotonic() - start
            if is_default:
                self._log_flash(image, selected_info.get(device, '?'), duration, ok)
            ejected = False
            if ok:
                self._eject_device(device)
                ejected = True
            self._queue.put(('done', device, ok, ejected, err))

        if self._flash_mode.get() == 'concurrent':
            for device in selected:
                threading.Thread(target=flash_one, args=(device,), daemon=True).start()
        else:
            def run_sequential():
                for device in selected:
                    flash_one(device)
            threading.Thread(target=run_sequential, daemon=True).start()

        self._poll_queue()

    def _verify(self) -> None:
        image = self._image_path.get().strip()
        if not image:
            messagebox.showerror('No Image', 'Please select an image file first.')
            return
        if not os.path.isfile(image):
            messagebox.showerror('File Not Found', f'Image not found:\n{image}')
            return
        if self._is_compressed(image):
            messagebox.showinfo(
                'Verify Unavailable',
                'Verify works on raw .img files only, not compressed images.')
            return

        selected = [self._tree.item(iid)['values'][0] for iid in self._tree.selection()]
        if not selected:
            messagebox.showerror('No Device', 'Please select at least one device.')
            return

        self._operation = 'verify'
        self._verify_results = {}
        self._flash_btn.configure(state='disabled')
        self._verify_btn.configure(state='disabled')
        self._clear_progress_rows()
        for device in selected:
            self._add_progress_row(device)
        self._remaining = len(selected)

        def verify_one(device):
            event = threading.Event()
            self._cancel_events[device] = event
            self._queue.put(('verifying', device))

            def progress_cb(pct, speed):
                self._queue.put(('progress', device, pct, speed))
            result = self._verify_device(image, str(device), progress_cb, event.is_set)
            self._verify_results[device] = result
            ok = result.status in ('match', 'benign')
            self._queue.put(('done', device, ok, False, result.detail))

        if self._flash_mode.get() == 'concurrent':
            for device in selected:
                threading.Thread(target=verify_one, args=(device,), daemon=True).start()
        else:
            def run_sequential():
                for device in selected:
                    verify_one(device)
            threading.Thread(target=run_sequential, daemon=True).start()

        self._poll_queue()

    def _done_text(self, device: str, ok: bool, ejected: bool) -> str:
        if self._operation == 'verify':
            result = self._verify_results.get(device)
            status = result.status if result else 'mismatch'
            return {
                'match':     'Verified ✓',
                'benign':    'Verified ⚠',
                'mismatch':  'Mismatch ✗',
                'cancelled': 'Cancelled',
                'error':     'Error',
            }.get(status, 'Mismatch ✗')
        if not ok:
            return 'Error'
        return 'Done, Ejected' if ejected else 'Done'

    def _diff_bar(self, res, width: int = BAR_WIDTH) -> list[tuple[str, str]]:
        """A BAR_WIDTH-cell map of the card: each cell flags the worst diff in it.

        Returns (tag, text) runs; real (red) outranks benign (green) outranks
        match (grey), and any region (however tiny) claims at least one cell.
        Cells past where the scan stopped are shown as 'unknown', not match.
        """
        total = res.total or 1
        scanned = res.scanned or total
        marks = [0] * width
        # cells lying entirely beyond the scanned point: not checked, so unknown
        for c in range(width):
            if c * total // width >= scanned:
                marks[c] = 3
        for r in res.regions:
            if r.start >= total:
                continue
            c0 = max(r.start * width // total, 0)
            c1 = min((min(r.end, total) - 1) * width // total, width - 1)
            val = 2 if not r.benign else 1
            for c in range(c0, c1 + 1):
                marks[c] = val if marks[c] == 3 else max(marks[c], val)
        chars = {0: '░', 1: '▒', 2: '█', 3: '·'}
        tags  = {0: 'match', 1: 'benign', 2: 'real', 3: 'unknown'}
        runs: list[tuple[str, str]] = []
        for m in marks:
            if runs and runs[-1][0] == tags[m]:
                runs[-1] = (tags[m], runs[-1][1] + chars[m])
            else:
                runs.append((tags[m], chars[m]))
        return runs

    def _show_verify_report(self) -> None:
        results = self._verify_results  # device -> VerifyResult
        if all(r.status == 'match' for r in results.values()):
            messagebox.showinfo(
                'Verify Complete', f'All {len(results)} device(s) match the image.')
            return

        win = tk.Toplevel(self)
        win.title('Verify Report')
        win.minsize(660, 460)

        intro = {'match': 'matches', 'benign': 'OS mount metadata only',
                 'mismatch': 'REAL differences', 'cancelled': 'cancelled (partial scan)',
                 'error': 'error'}

        # ── card map (one coloured bar per device, Details-window style) ──────
        text = tk.Text(win, font=('Monospace', 11), state='disabled',
                       bg=self.cget('bg'), relief='flat', width=58,
                       height=len(results) * 3 + 2)
        text.tag_configure('match',   foreground='#555555')
        text.tag_configure('benign',  foreground='#4caf50')
        text.tag_configure('real',    foreground='#c0392b')
        text.tag_configure('unknown', foreground='#c9a227')
        text.tag_configure('label',   foreground='#aaaaaa')
        text.pack(fill='x', padx=10, pady=(10, 4))

        any_unscanned = any(0 < r.scanned < r.total for r in results.values())
        content: list[tuple[str, str]] = []
        for device, res in results.items():
            content.append(('label', f'{device}   -   {intro.get(res.status, res.status)}\n'))
            content.append(('label', '  '))
            content.extend(self._diff_bar(res))
            content.append(('label', '\n\n'))
        content += [('label', '  '), ('real', '█'), ('label', ' real   '),
                    ('benign', '▒'), ('label', ' benign   '),
                    ('match', '░'), ('label', ' match')]
        if any_unscanned:
            content += [('label', '   '), ('unknown', '·'), ('label', ' not scanned')]
        text.configure(state='normal')
        for tag, chunk in content:
            text.insert('end', chunk, tag)
        text.configure(state='disabled')

        # ── per-region breakdown ─────────────────────────────────────────────
        tree = ttk.Treeview(win, columns=('size', 'kind'), show='tree headings')
        tree.heading('#0', text='Device / region')
        tree.column('#0', width=380)
        tree.heading('size', text='Size')
        tree.column('size', width=110, anchor='e')
        tree.heading('kind', text='REAL / benign')
        tree.column('kind', width=110, anchor='center')
        tree.tag_configure('real',    foreground='#c0392b')
        tree.tag_configure('benign',  foreground='#4caf50')
        tree.tag_configure('unknown', foreground='#c9a227')

        for device, res in results.items():
            parent = tree.insert('', 'end', open=True,
                                 text=f'{device}   -   {intro.get(res.status, res.status)}')
            if res.status == 'error':
                tree.insert(parent, 'end', text=res.detail)
                continue
            groups: dict = {}
            for r in res.regions:
                sz, cnt = groups.get((r.label, r.benign), (0, 0))
                groups[(r.label, r.benign)] = (sz + r.size, cnt + 1)
            # real differences first, then benign, each alphabetised by label
            for (label, benign), (sz, cnt) in sorted(
                    groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                text_ = label + (f'   ({cnt} regions)' if cnt > 1 else '')
                tree.insert(parent, 'end', text=text_,
                            values=(human_size(sz), 'benign' if benign else 'REAL'),
                            tags=('benign' if benign else 'real',))
            if 0 < res.scanned < res.total:
                tree.insert(parent, 'end',
                            text='stopped early: remainder not scanned',
                            values=(human_size(res.total - res.scanned), 'not scanned'),
                            tags=('unknown',))
            elif res.truncated:
                tree.insert(parent, 'end', text='… more regions not shown')

        tree.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        ttk.Button(win, text='Close', command=win.destroy).pack(pady=(0, 10))

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg[0] == 'progress':
                    _, device, pct, speed = msg
                    if device in self._progress_rows:
                        pct_var, speed_label, _, _ = self._progress_rows[device]
                        pct_var.set(pct)
                        speed_label.configure(text=speed)
                elif msg[0] == 'verifying':
                    self._show_cancel(msg[1])
                elif msg[0] == 'done':
                    _, device, ok, ejected, result = msg
                    if device in self._progress_rows:
                        pct_var, speed_label, _, cancel_btn = self._progress_rows[device]
                        pct_var.set(100)
                        speed_label.configure(text=self._done_text(device, ok, ejected))
                        cancel_btn.pack_forget()
                    self._remaining -= 1
                    if not ok and self._operation != 'verify':
                        messagebox.showerror('Error', f'{device}:\n{result}')
                    if self._remaining == 0:
                        self._flash_btn.configure(state='normal')
                        self._verify_btn.configure(state='normal')
                        if self._operation == 'verify':
                            self._show_verify_report()
                        return
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)
