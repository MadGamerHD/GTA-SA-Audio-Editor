import struct
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tempfile
import pygame
import wave
import io

# Constants
ENCODE_KEY = bytes.fromhex('EA3AC4A19AA814F348B0D7239DE8FFF1')
TRACK_HEADER_SIZE = 8068
CONFIG_DIR = 'audio/CONFIG'
SFX_DIR = 'audio/SFX'
BANK_HEADER_SIZE = 4804
DEFAULT_SAMPLE_RATE = 22050  # fallback rate for invalid values

# Background task runner
def run_in_thread(fn):
    def wrapper(*args, **kwargs):
        threading.Thread(target=lambda: fn(*args, **kwargs), daemon=True).start()
    return wrapper

def xor_in_place_stride(data: bytearray, key: bytes, progress_callback=None):
    """
    XOR‐decrypt `data` in place using `key` by processing in stride‐based slices.
    Calls progress_callback(processed_bytes, total_bytes) periodically.
    """
    total = len(data)
    klen = len(key)
    processed = 0
    # We'll update progress every time we finish XOR’ing one full "j" pass,
    # but only call back if we've moved forward by at least 4096 bytes.
    last_report = 0

    for j in range(klen):
        # Take every klen‐th byte starting at j
        sl = data[j:total:klen]
        if not sl:
            continue
        # XOR that slice
        xored = bytes(b ^ key[j] for b in sl)
        data[j:total:klen] = xored

        processed += len(sl)
        # Call progress whenever we've passed another 4096 bytes
        if progress_callback and (processed - last_report >= 4096):
            last_report = processed
            progress_callback(min(processed, total), total)

    # Final callback to indicate completion
    if progress_callback:
        progress_callback(total, total)

class StreamArchive:
    def __init__(self, path, progress_callback=None):
        self.filepath = Path(path)
        self.tracks = []
        self._decode_and_parse(progress_callback)

    def _decode_and_parse(self, progress_callback):
        # Read entire encrypted file into a bytearray
        raw = self.filepath.read_bytes()
        data = bytearray(raw)
        total = len(data)
        key = ENCODE_KEY

        # Decrypt in place using stride‐based XOR
        xor_in_place_stride(data, key, progress_callback)

        # Now parse decrypted data into tracks
        mv = memoryview(data)
        offset = 0
        idx = 1

        while offset + TRACK_HEADER_SIZE <= total:
            hdr = mv[offset : offset + TRACK_HEADER_SIZE]
            length = 0
            base = 8000
            for j in range(8):
                try:
                    l, _ = struct.unpack_from('<II', hdr, base + j * 8)
                except struct.error:
                    continue
                if l != 0xCDCDCDCD:
                    length = l
                    break

            start = offset + TRACK_HEADER_SIZE
            end = start + length
            if end > total:
                break

            self.tracks.append({
                'header': bytes(hdr),
                'data': bytes(mv[start:end]),
                'name': f"{self.filepath.stem}_{idx}"
            })

            offset = end
            idx += 1

    def export(self, idx, out_dir):
        t = self.tracks[idx]
        Path(out_dir, f"{t['name']}.ogg").write_bytes(t['data'])

    def export_all(self, out_dir, progress_callback=None):
        total = len(self.tracks)
        for i, _ in enumerate(self.tracks):
            self.export(i, out_dir)
            if progress_callback:
                progress_callback(i + 1, total)

    def replace(self, idx, newfile):
        self.tracks[idx]['data'] = Path(newfile).read_bytes()

    def rebuild(self, progress_callback=None):
        total = sum(TRACK_HEADER_SIZE + len(t['data']) for t in self.tracks)
        buf = bytearray(total)
        write_ptr = 0
        count = 0

        # Rebuild the decrypted buffer (headers + data)
        for t in self.tracks:
            hdr = t['header']
            d = t['data']
            buf[write_ptr : write_ptr + len(hdr)] = hdr
            write_ptr += len(hdr)
            count += len(hdr)
            if progress_callback:
                progress_callback(count, total)

            buf[write_ptr : write_ptr + len(d)] = d
            write_ptr += len(d)
            count += len(d)
            if progress_callback:
                progress_callback(count, total)

        # Re‐encrypt entire buffer in place
        key = ENCODE_KEY
        klen = len(key)
        total_buf = len(buf)
        processed = 0
        last_report = 0

        for offset in range(0, total_buf, klen):
            chunk_size = min(klen, total_buf - offset)
            for j in range(chunk_size):
                buf[offset + j] ^= key[j]
            processed += chunk_size
            if progress_callback and (processed - last_report >= 4096):
                last_report = processed
                progress_callback(min(processed, total_buf), total_buf)

        # Final callback
        if progress_callback:
            progress_callback(total_buf, total_buf)

        self.filepath.write_bytes(buf)

class SFXArchive:
    def __init__(self, root, progress_callback=None):
        self.root = Path(root)
        self.config = self.root / CONFIG_DIR
        self.sfx = self.root / SFX_DIR
        self.sounds = []
        self._load(progress_callback)

    def _load(self, progress_callback):
        if not self.config.exists():
            messagebox.showerror('Error', f"CONFIG folder not found:\n{self.config}")
            return

        pak_path = self.config / 'PakFiles.dat'
        try:
            pak_data = pak_path.read_bytes()
        except FileNotFoundError:
            messagebox.showerror('Error', f"PakFiles.dat not found in {self.config}")
            return

        packages = []
        entry_size = 52
        for i in range(0, len(pak_data), entry_size):
            raw_name = pak_data[i : i + entry_size]
            name = raw_name.split(b'\x00', 1)[0].decode(errors='ignore')
            if name:
                packages.append(name)

        bl_path = self.config / 'BankLkup.dat'
        try:
            bl_data = bl_path.read_bytes()
        except FileNotFoundError:
            messagebox.showerror('Error', f"BankLkup.dat not found in {self.config}")
            return

        bank_entries = []
        fmt = '<B3xII'
        for entry in struct.iter_unpack(fmt, bl_data):
            pkg_idx, off, size = entry
            bank_entries.append((pkg_idx, off, size))

        total_pkgs = len(packages)
        for pi, pkg_name in enumerate(packages):
            if progress_callback:
                progress_callback(pi, total_pkgs)

            pfile = self.sfx / pkg_name
            if not pfile.exists():
                continue

            data = pfile.read_bytes()
            data_len = len(data)
            mv_data = memoryview(data)

            for pkg_idx, off, size in bank_entries:
                if pkg_idx != pi:
                    continue

                if off < 0 or off + BANK_HEADER_SIZE > data_len:
                    continue

                hdr = mv_data[off : off + BANK_HEADER_SIZE]

                try:
                    count = struct.unpack_from('<H', hdr, 0)[0]
                except struct.error:
                    continue

                for si in range(count):
                    base = 4 + si * 12
                    if base + 12 > len(hdr):
                        break

                    buf_off, _, rate, _ = struct.unpack_from('<IIHH', hdr, base)
                    pcm_start = off + BANK_HEADER_SIZE + buf_off

                    if si < count - 1:
                        try:
                            nxt = struct.unpack_from('<I', hdr, base + 12)[0]
                        except struct.error:
                            nxt = buf_off
                    else:
                        nxt = size

                    length = nxt - buf_off
                    if length <= 0 or pcm_start + length > data_len:
                        continue

                    pcm = bytes(mv_data[pcm_start : pcm_start + length])
                    self.sounds.append({
                        'pkg_file': pfile,
                        'header_off': off,
                        'pcm_offset': buf_off,
                        'pcm': pcm,
                        'rate': rate or DEFAULT_SAMPLE_RATE,
                        'name': f"{pkg_name}_b{si}"
                    })

    def export(self, idx, out_dir):
        s = self.sounds[idx]
        wav = self._wrap_wav(s['pcm'], s['rate'])
        Path(out_dir, f"{s['name']}.wav").write_bytes(wav)

    def export_all(self, out_dir, progress_callback=None):
        total = len(self.sounds)
        for i, _ in enumerate(self.sounds):
            self.export(i, out_dir)
            if progress_callback:
                progress_callback(i + 1, total)

    def replace(self, idx, newfile):
        with wave.open(newfile, 'rb') as wf:
            pcm = wf.readframes(wf.getnframes())
        self.sounds[idx]['pcm'] = pcm

    def rebuild(self, progress_callback=None):
        pkg_map = {}
        for s in self.sounds:
            pkg_map.setdefault(s['pkg_file'], []).append(s)

        for pfile, sounds in pkg_map.items():
            orig = bytearray(pfile.read_bytes())
            data_len = len(orig)

            for s in sounds:
                start = s['header_off'] + BANK_HEADER_SIZE + s['pcm_offset']
                end = start + len(s['pcm'])
                if 0 <= start < data_len and end <= data_len:
                    orig[start:end] = s['pcm']
            pfile.write_bytes(orig)

    def _wrap_wav(self, pcm, rate):
        buf = io.BytesIO()
        wf = wave.open(buf, 'wb')
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
        wf.close()
        return buf.getvalue()

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('GTA SA Audio Editor')
        # Make window smaller
        self.geometry('600x400')

        pygame.init()
        pygame.mixer.init()

        self.stream_arc = None
        self.sfx_arc = None
        self.current_sound = None
        self.current_stream_temp = None

        self._build_ui()

    def _build_ui(self):
        self._create_menu()
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True)

        self._build_stream_tab(nb)
        self._build_sfx_tab(nb)

        self.progress = ttk.Progressbar(self, mode='determinate')
        self.progress.pack(fill='x', padx=5, pady=5)

    def _create_menu(self):
        menu = tk.Menu(self)
        menu.add_command(label='Exit', command=self.destroy)
        self.config(menu=menu)

    def _build_stream_tab(self, notebook):
        st = ttk.Frame(notebook)
        notebook.add(st, text='Stream')

        btns = ttk.Frame(st)
        btns.pack(fill='x', pady=5)
        for txt, cmd in [
            ('Load', self.load_stream),
            ('Export', self.export_track),
            ('Expt All', self.batch_export_stream),
            ('Replace', self.replace_track),
            ('Rebuild', self.rebuild_stream),
            ('Play', self.play_stream),
            ('Stop', self.stop_stream)
        ]:
            ttk.Button(btns, text=txt, command=cmd, width=8).pack(side='left', padx=2)

        # Frame to hold Listbox + Scrollbar
        list_frame = ttk.Frame(st)
        list_frame.pack(fill='both', expand=True, padx=5, pady=5)

        self.stream_listbox = tk.Listbox(list_frame, height=15)
        self.stream_listbox.pack(side='left', fill='both', expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.stream_listbox.yview)
        scrollbar.pack(side='right', fill='y')
        self.stream_listbox.config(yscrollcommand=scrollbar.set)

    def _build_sfx_tab(self, notebook):
        sx = ttk.Frame(notebook)
        notebook.add(sx, text='SFX')

        btns = ttk.Frame(sx)
        btns.pack(fill='x', pady=5)
        for txt, cmd in [
            ('Load', self.load_sfx),
            ('Export', self.export_sfx),
            ('Expt All', self.batch_export_sfx),
            ('Replace', self.replace_sfx),
            ('Rebuild', self.rebuild_sfx),
            ('Play', self.play_sfx),
            ('Stop', self.stop_sfx)
        ]:
            ttk.Button(btns, text=txt, command=cmd, width=8).pack(side='left', padx=2)

        list_frame = ttk.Frame(sx)
        list_frame.pack(fill='both', expand=True, padx=5, pady=5)

        self.sfx_listbox = tk.Listbox(list_frame, height=15)
        self.sfx_listbox.pack(side='left', fill='both', expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.sfx_listbox.yview)
        scrollbar.pack(side='right', fill='y')
        self.sfx_listbox.config(yscrollcommand=scrollbar.set)

    @run_in_thread
    def load_stream(self):
        path = filedialog.askopenfilename(title='Select .stream')
        if not path:
            return

        def progress_cb(v, t):
            self.after(0, self._update_progress, v, t)

        arc = StreamArchive(path, progress_callback=progress_cb)
        self.stream_arc = arc

        names = [t['name'] for t in arc.tracks]
        self.after(0, lambda: self._populate_listbox(self.stream_listbox, names))

    @run_in_thread
    def load_sfx(self):
        root = filedialog.askdirectory(title='Select GTA SA Root')
        if not root:
            return

        def progress_cb(v, t):
            self.after(0, self._update_progress, v, t)

        arc = SFXArchive(root, progress_callback=progress_cb)
        self.sfx_arc = arc

        names = [s['name'] for s in arc.sounds]
        self.after(0, lambda: self._populate_listbox(self.sfx_listbox, names))

    def _populate_listbox(self, lb: tk.Listbox, items):
        lb.delete(0, tk.END)
        for name in items:
            lb.insert(tk.END, name)

    @run_in_thread
    def batch_export_stream(self):
        out = filedialog.askdirectory(title='Expt All Stream to:')
        if out and self.stream_arc:
            def progress_cb(v, t):
                self.after(0, self._update_progress, v, t)
            self.stream_arc.export_all(out, progress_callback=progress_cb)

    @run_in_thread
    def batch_export_sfx(self):
        out = filedialog.askdirectory(title='Expt All SFX to:')
        if out and self.sfx_arc:
            def progress_cb(v, t):
                self.after(0, self._update_progress, v, t)
            self.sfx_arc.export_all(out, progress_callback=progress_cb)

    def export_track(self):
        sel = self.stream_listbox.curselection()
        if sel and self.stream_arc:
            idx = sel[0]
            path = filedialog.askdirectory(title='Export Stream to:')
            if path:
                self.stream_arc.export(idx, path)

    def export_sfx(self):
        sel = self.sfx_listbox.curselection()
        if sel and self.sfx_arc:
            idx = sel[0]
            path = filedialog.askdirectory(title='Export SFX to:')
            if path:
                self.sfx_arc.export(idx, path)

    def replace_track(self):
        sel = self.stream_listbox.curselection()
        if sel and self.stream_arc:
            idx = sel[0]
            nf = filedialog.askopenfilename(filetypes=[('Ogg', '*.ogg')])
            if nf:
                self.stream_arc.replace(idx, nf)

    def replace_sfx(self):
        sel = self.sfx_listbox.curselection()
        if sel and self.sfx_arc:
            idx = sel[0]
            nf = filedialog.askopenfilename(filetypes=[('WAV', '*.wav')])
            if nf:
                self.sfx_arc.replace(idx, nf)
                messagebox.showinfo('SFX Replaced', 'Replacement successful')

    @run_in_thread
    def rebuild_stream(self):
        if self.stream_arc:
            def progress_cb(v, t):
                self.after(0, self._update_progress, v, t)
            self.stream_arc.rebuild(progress_callback=progress_cb)
            self.after(0, lambda: messagebox.showinfo('Stream Rebuilt', 'Done'))

    @run_in_thread
    def rebuild_sfx(self):
        if self.sfx_arc:
            def progress_cb(v, t):
                self.after(0, self._update_progress, v, t)
            self.sfx_arc.rebuild(progress_callback=progress_cb)
            self.after(0, lambda: messagebox.showinfo('SFX Rebuilt', 'All banks rebuilt'))

    def play_stream(self):
        sel = self.stream_listbox.curselection()
        if sel and self.stream_arc:
            idx = sel[0]
            data = self.stream_arc.tracks[idx]['data']
            tf = tempfile.NamedTemporaryFile(suffix='.ogg', delete=False)
            tf.write(data)
            tf.close()
            self.stop_stream()
            pygame.mixer.music.load(tf.name)
            pygame.mixer.music.play()
            self.current_stream_temp = tf.name

    def stop_stream(self):
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
        if self.current_stream_temp:
            try:
                Path(self.current_stream_temp).unlink()
            except:
                pass
            self.current_stream_temp = None

    def play_sfx(self):
        sel = self.sfx_listbox.curselection()
        if sel and self.sfx_arc:
            idx = sel[0]
            snd = self.sfx_arc.sounds[idx]
            wav = self.sfx_arc._wrap_wav(snd['pcm'], snd['rate'])
            if self.current_sound:
                self.current_sound.stop()
            self.current_sound = pygame.mixer.Sound(buffer=wav)
            self.current_sound.play()

    def stop_sfx(self):
        if self.current_sound:
            self.current_sound.stop()

    def _update_progress(self, val, total):
        self.progress.config(maximum=total, value=val)
        self.update_idletasks()

if __name__ == '__main__':
    App().mainloop()
