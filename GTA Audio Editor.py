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

class StreamArchive:
    def __init__(self, path, progress_callback=None):
        self.filepath = Path(path)
        self.tracks = []
        self._decode_and_parse(progress_callback)

    def _decode_and_parse(self, progress_callback):
        data = bytearray(self.filepath.read_bytes())
        total = len(data)
        for i in range(total):
            data[i] ^= ENCODE_KEY[i % len(ENCODE_KEY)]
            if progress_callback and i % 4096 == 0:
                progress_callback(i, total)
        offset, idx = 0, 1
        while offset + TRACK_HEADER_SIZE <= total:
            hdr = data[offset:offset+TRACK_HEADER_SIZE]
            length = next((l for j in range(8)
                           for l,_ in [struct.unpack_from('<II', hdr, 8000 + j*8)]
                           if l != 0xCDCDCDCD), 0)
            start = offset + TRACK_HEADER_SIZE
            self.tracks.append({
                'header': bytes(hdr),
                'data': bytes(data[start:start+length]),
                'name': f"{self.filepath.stem}_{idx}"
            })
            offset = start + length
            idx += 1

    def export(self, idx, out_dir):
        t = self.tracks[idx]
        Path(out_dir, f"{t['name']}.ogg").write_bytes(t['data'])

    def export_all(self, out_dir, progress_callback=None):
        total = len(self.tracks)
        for i, _ in enumerate(self.tracks):
            self.export(i, out_dir)
            if progress_callback:
                progress_callback(i+1, total)

    def replace(self, idx, newfile):
        self.tracks[idx]['data'] = Path(newfile).read_bytes()

    def rebuild(self, progress_callback=None):
        buf = bytearray()
        total = sum(len(t['header']) + len(t['data']) for t in self.tracks)
        count = 0
        for t in self.tracks:
            buf.extend(t['header']); count += len(t['header'])
            if progress_callback: progress_callback(count, total)
            buf.extend(t['data']); count += len(t['data'])
            if progress_callback: progress_callback(count, total)
        for i in range(len(buf)):
            buf[i] ^= ENCODE_KEY[i % len(ENCODE_KEY)]
            if progress_callback and i % 4096 == 0:
                progress_callback(i, len(buf))
        self.filepath.write_bytes(buf)

class SFXArchive:
    def __init__(self, root, progress_callback=None):
        self.root = Path(root)
        self.config = self.root / CONFIG_DIR
        self.sfx = self.root / SFX_DIR
        self.sounds = []
        self._load(progress_callback)

    def _load(self, progress_callback):
        # 1) make sure CONFIG dir exists
        if not self.config.exists():
            messagebox.showerror('Error', f"CONFIG folder not found:\n{self.config}")
            return

        # 1) read and parse PakFiles.dat
        try:
            pak = (self.config / 'PakFiles.dat').read_bytes()
        except FileNotFoundError:
            messagebox.showerror('Error', f"PakFiles.dat not found in {self.config}")
            return

        packages = []
        for i in range(len(pak) // 52):
            name = pak[i*52:(i+1)*52].split(b'\x00', 1)[0].decode(errors='ignore')
            if name:                            # 3) skip empty names
                packages.append(name)

        # 1) read and parse BankLkup.dat
        try:
            bl = (self.config / 'BankLkup.dat').read_bytes()
        except FileNotFoundError:
            messagebox.showerror('Error', f"BankLkup.dat not found in {self.config}")
            return

        bank_entries = []
        for i in range(len(bl) // 12):
            try:
                pkg_idx, off, size = struct.unpack_from('<B3xII', bl, i*12)
                bank_entries.append((pkg_idx, off, size))
            except struct.error:
                # malformed entry, skip it
                continue

        total_pkgs = len(packages)
        for pi, pkg in enumerate(packages):
            if progress_callback:
                progress_callback(pi, total_pkgs)

            pfile = self.sfx / pkg
            if not pfile.exists():
                # no pak file for this entry, skip
                continue

            data = pfile.read_bytes()
            data_len = len(data)

            # pull out only the banks for this package
            for pkg_idx, off, size in bank_entries:
                if pkg_idx != pi:
                    continue

                # 2) make sure the header block is in-range
                if off < 0 or off + BANK_HEADER_SIZE > data_len:
                    continue

                hdr = data[off:off + BANK_HEADER_SIZE]

                # how many sounds in this bank?
                try:
                    count = struct.unpack_from('<H', hdr, 0)[0]
                except struct.error:
                    continue

                for si in range(count):
                    base = 4 + si * 12
                    # ensure we can actually read the 12-byte entry
                    if base + 12 > len(hdr):
                        break

                    buf_off, _, rate, _ = struct.unpack_from('<IIHH', hdr, base)
                    pcm_start = off + BANK_HEADER_SIZE + buf_off

                    # figure out length: look at the next entry’s buf_off, or fall back to 'size'
                    if si < count - 1:
                        try:
                            nxt = struct.unpack_from('<I', hdr, base + 12)[0]
                        except struct.error:
                            nxt = buf_off
                    else:
                        nxt = size

                    length = nxt - buf_off
                    # 2) skip if the PCM slice would run off the end
                    if length <= 0 or pcm_start + length > data_len:
                        continue

                    pcm = data[pcm_start:pcm_start + length]
                    self.sounds.append({
                        'pkg_file': pfile,
                        'header_off': off,
                        'pcm_offset': buf_off,
                        'pcm': pcm,
                        'rate': rate or DEFAULT_SAMPLE_RATE,
                        'name': f"{pkg}_b{si}"
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
                progress_callback(i+1, total)

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
            for s in sounds:
                start = s['header_off'] + BANK_HEADER_SIZE + s['pcm_offset']
                orig[start:start+len(s['pcm'])] = s['pcm']
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
        self.geometry('900x600')
        pygame.init(); pygame.mixer.init()
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
        st = ttk.Frame(notebook); notebook.add(st, text='Stream')
        btns = ttk.Frame(st); btns.pack(fill='x', pady=5)
        for txt, cmd in [('Load', self.load_stream),
                         ('Export', self.export_track),
                         ('Export All', self.batch_export_stream),
                         ('Replace', self.replace_track),
                         ('Rebuild', self.rebuild_stream),
                         ('Play', self.play_stream),
                         ('Stop', self.stop_stream)]:
            ttk.Button(btns, text=txt, command=cmd).pack(side='left', padx=2)
        self.stream_tree = ttk.Treeview(st, columns=('Idx','Name'), show='headings')
        for col, w in [('Idx',60),('Name',300)]:
            self.stream_tree.heading(col, text=col);
            self.stream_tree.column(col, width=w)
        self.stream_tree.pack(fill='both', expand=True)

    def _build_sfx_tab(self, notebook):
        sx = ttk.Frame(notebook); notebook.add(sx, text='SFX')
        btns = ttk.Frame(sx); btns.pack(fill='x', pady=5)
        for txt, cmd in [('Load', self.load_sfx),
                         ('Export', self.export_sfx),
                         ('Export All', self.batch_export_sfx),
                         ('Replace', self.replace_sfx),
                         ('Rebuild', self.rebuild_sfx),
                         ('Play', self.play_sfx),
                         ('Stop', self.stop_sfx)]:
            ttk.Button(btns, text=txt, command=cmd).pack(side='left', padx=2)
        self.sfx_tree = ttk.Treeview(sx, columns=('Name','Rate'), show='headings')
        for col, w in [('Name',400),('Rate',80)]:
            self.sfx_tree.heading(col, text=col);
            self.sfx_tree.column(col, width=w)
        self.sfx_tree.pack(fill='both', expand=True)

    @run_in_thread
    def load_stream(self):
        path = filedialog.askopenfilename(title='Select .stream')
        if not path: return
        self.stream_arc = StreamArchive(path, progress_callback=self._update_progress)
        self._populate_tree(self.stream_tree, [(i+1,t['name']) for i,t in enumerate(self.stream_arc.tracks)])

    @run_in_thread
    def load_sfx(self):
        root = filedialog.askdirectory(title='Select GTA SA Root')
        if not root: return
        self.sfx_arc = SFXArchive(root, progress_callback=self._update_progress)
        self._populate_tree(self.sfx_tree, [(s['name'], s['rate']) for s in self.sfx_arc.sounds])

    def _populate_tree(self, tree, items):
        tree.delete(*tree.get_children())
        for i, vals in enumerate(items): tree.insert('', 'end', iid=i, values=vals)

    @run_in_thread
    def batch_export_stream(self):
        out = filedialog.askdirectory(title='Export All Stream to:')
        if out: self.stream_arc.export_all(out, progress_callback=self._update_progress)

    @run_in_thread
    def batch_export_sfx(self):
        out = filedialog.askdirectory(title='Export All SFX to:')
        if out: self.sfx_arc.export_all(out, progress_callback=self._update_progress)

    def export_track(self):
        sel = self.stream_tree.selection()
        if sel: path = filedialog.askdirectory(title='Export Stream to:')
        if sel and path: self.stream_arc.export(int(sel[0]), path)

    def export_sfx(self):
        sel = self.sfx_tree.selection()
        if sel: path = filedialog.askdirectory(title='Export SFX to:')
        if sel and path: self.sfx_arc.export(int(sel[0]), path)

    def replace_track(self):
        sel = self.stream_tree.selection()
        if sel:
            nf = filedialog.askopenfilename(filetypes=[('Ogg','*.ogg')])
            if nf: self.stream_arc.replace(int(sel[0]), nf)

    def replace_sfx(self):
        sel = self.sfx_tree.selection()
        if sel:
            nf = filedialog.askopenfilename(filetypes=[('WAV','*.wav')])
            if nf:
                self.sfx_arc.replace(int(sel[0]), nf)
                messagebox.showinfo('SFX Replaced', 'Replacement successful')

    @run_in_thread
    def rebuild_stream(self):
        if self.stream_arc:
            self.stream_arc.rebuild(progress_callback=self._update_progress)
            messagebox.showinfo('Stream Rebuilt','Done')

    @run_in_thread
    def rebuild_sfx(self):
        if self.sfx_arc:
            self.sfx_arc.rebuild(progress_callback=self._update_progress)
            messagebox.showinfo('SFX Rebuilt', 'All banks rebuilt')

    def play_stream(self):
        sel = self.stream_tree.selection()
        if sel:
            data = self.stream_arc.tracks[int(sel[0])]['data']
            tf = tempfile.NamedTemporaryFile(suffix='.ogg', delete=False)
            tf.write(data); tf.close()
            self.stop_stream()
            pygame.mixer.music.load(tf.name)
            pygame.mixer.music.play()
            self.current_stream_temp = tf.name

    def stop_stream(self):
        if pygame.mixer.music.get_busy(): pygame.mixer.music.stop()
        if self.current_stream_temp:
            try: Path(self.current_stream_temp).unlink()
            except: pass
            self.current_stream_temp = None

    def play_sfx(self):
        sel = self.sfx_tree.selection()
        if sel:
            snd = self.sfx_arc.sounds[int(sel[0])]
            wav = self.sfx_arc._wrap_wav(snd['pcm'], snd['rate'])
            if self.current_sound: self.current_sound.stop()
            self.current_sound = pygame.mixer.Sound(buffer=wav)
            self.current_sound.play()

    def stop_sfx(self):
        if self.current_sound: self.current_sound.stop()

    def _update_progress(self, val, total):
        self.progress.config(maximum=total, value=val)
        self.update_idletasks()

if __name__ == '__main__':
    App().mainloop()
