import struct
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tempfile
import pygame
import wave
import io
from collections import defaultdict
import time

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

def xor_in_place_simple(data: bytearray, key: bytes, progress_callback=None):
    """
    XOR-decrypt `data` in place using `key` in one single pass.
    Calls progress_callback(processed_bytes, total_bytes) periodically.
    """
    total = len(data)
    klen = len(key)
    processed = 0
    last_report = 0

    for i in range(total):
        data[i] ^= key[i % klen]
        processed += 1
        if progress_callback and (processed - last_report >= 4096):
            last_report = processed
            progress_callback(processed, total)

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

        # Decrypt in place using single-pass XOR
        xor_in_place_simple(data, key, progress_callback)

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
        out_path = Path(out_dir) / f"{t['name']}.ogg"
        out_path.write_bytes(t['data'])

    def export_all(self, out_dir, progress_callback=None):
        total = len(self.tracks)
        for i in range(total):
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

        # Re-encrypt entire buffer in place
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

        # Cache bank entries by package index
        bank_map = defaultdict(list)  # pkg_idx -> list of (off, size)
        fmt = '<B3xII'
        for pkg_idx, off, size in struct.iter_unpack(fmt, bl_data):
            bank_map[pkg_idx].append((off, size))

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

            # Look up entries for this package directly
            for off, size in bank_map.get(pi, []):
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
        out_path = Path(out_dir) / f"{s['name']}.wav"
        out_path.write_bytes(wav)

    def export_all(self, out_dir, progress_callback=None):
        total = len(self.sounds)
        for i in range(total):
            self.export(i, out_dir)
            if progress_callback:
                progress_callback(i + 1, total)

    def replace(self, idx, newfile):
        with wave.open(newfile, 'rb') as wf:
            pcm = wf.readframes(wf.getnframes())
        self.sounds[idx]['pcm'] = pcm

    def rebuild(self, progress_callback=None):
        pkg_map = defaultdict(list)
        for s in self.sounds:
            pkg_map[s['pkg_file']].append(s)

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
        self.geometry('600x450')  # slightly taller to fit time controls

        pygame.init()
        pygame.mixer.init()

        self.stream_arc = None
        self.sfx_arc = None

        # For playback tracking
        self.current_stream_temp = None
        self.current_duration = 0.0

        self.current_sound = None
        self.current_sfx_length = 0.0
        self.sfx_start_time = 0.0

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
        menu.add_command(label='Exit', command=self._on_exit)
        self.config(menu=menu)

    def _on_exit(self):
        # Clean up any temp files
        if self.current_stream_temp:
            try:
                Path(self.current_stream_temp).unlink()
            except Exception:
                pass
        self.destroy()

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

        self.stream_listbox = tk.Listbox(list_frame, height=12)
        self.stream_listbox.pack(side='left', fill='both', expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.stream_listbox.yview)
        scrollbar.pack(side='right', fill='y')
        self.stream_listbox.config(yscrollcommand=scrollbar.set)

        # Time label + seek slider
        times_frame = ttk.Frame(st)
        times_frame.pack(fill='x', padx=5, pady=5)

        self.time_label = ttk.Label(times_frame, text="00:00 / 00:00")
        self.time_label.pack(side='left')

        self.seek_slider = ttk.Scale(
            times_frame,
            from_=0.0, to=1.0,
            orient='horizontal',
            command=self.on_seek
        )
        self.seek_slider.pack(fill='x', expand=True, side='left', padx=5)
        self.seek_slider.state(['disabled'])

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

        self.sfx_listbox = tk.Listbox(list_frame, height=12)
        self.sfx_listbox.pack(side='left', fill='both', expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.sfx_listbox.yview)
        scrollbar.pack(side='right', fill='y')
        self.sfx_listbox.config(yscrollcommand=scrollbar.set)

        # Time label + seek slider for SFX
        sfx_times = ttk.Frame(sx)
        sfx_times.pack(fill='x', padx=5, pady=5)

        self.sfx_time_label = ttk.Label(sfx_times, text="00:00 / 00:00")
        self.sfx_time_label.pack(side='left')

        self.sfx_seek_slider = ttk.Scale(
            sfx_times,
            from_=0.0, to=1.0,
            orient='horizontal',
            command=self.on_sfx_seek
        )
        self.sfx_seek_slider.pack(fill='x', expand=True, side='left', padx=5)
        self.sfx_seek_slider.state(['disabled'])

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
        if not sel or not self.stream_arc:
            messagebox.showwarning("No track", "Select a track first.")
            return

        idx = sel[0]
        data = self.stream_arc.tracks[idx]['data']
        tf = tempfile.NamedTemporaryFile(suffix='.ogg', delete=False)
        tf.write(data)
        tf.close()

        self.stop_stream()

        try:
            pygame.mixer.music.load(tf.name)
        except pygame.error as e:
            messagebox.showerror("Playback Error", f"Could not load OGG: {e}")
            Path(tf.name).unlink(missing_ok=True)
            return

        # Determine total duration
        try:
            sound_obj = pygame.mixer.Sound(tf.name)
            self.current_duration = sound_obj.get_length()
        except pygame.error:
            self.current_duration = 0.0

        self.current_stream_temp = tf.name
        pygame.mixer.music.play()

        # Enable slider & start polling
        self.seek_slider.state(['!disabled'])
        self._update_time_loop()

    def stop_stream(self):
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
        if self.current_stream_temp:
            try:
                Path(self.current_stream_temp).unlink()
            except Exception:
                pass
            self.current_stream_temp = None

        # Reset UI
        self.time_label.config(text="00:00 / 00:00")
        self.seek_slider.config(value=0.0)
        self.seek_slider.state(['disabled'])

    def _update_time_loop(self):
        if not pygame.mixer.music.get_busy():
            # Playback stopped or finished
            self.time_label.config(text="00:00 / 00:00")
            self.seek_slider.config(value=0.0)
            return

        pos_ms = pygame.mixer.music.get_pos()
        if pos_ms < 0:
            self.after(100, self._update_time_loop)
            return

        current_secs = pos_ms / 1000.0
        total = self.current_duration or 1.0
        cur_m, cur_s = divmod(int(current_secs), 60)
        tot_m, tot_s = divmod(int(total), 60)
        self.time_label.config(text=f"{cur_m:02d}:{cur_s:02d} / {tot_m:02d}:{tot_s:02d}")
        self.seek_slider.config(value=min(current_secs / total, 1.0))

        self.after(100, self._update_time_loop)

    def on_seek(self, slider_value):
        if not self.current_stream_temp or self.current_duration <= 0:
            return

        ratio = float(slider_value)
        target = ratio * self.current_duration

        try:
            pygame.mixer.music.play(start=target)
        except TypeError:
            pygame.mixer.music.stop()
            pygame.mixer.music.load(self.current_stream_temp)
            try:
                pygame.mixer.music.set_pos(target)
            except Exception:
                pass
            pygame.mixer.music.play()

        self._update_time_loop()

    def play_sfx(self):
        sel = self.sfx_listbox.curselection()
        if not sel or not self.sfx_arc:
            messagebox.showwarning("No sound", "Select an SFX first.")
            return

        idx = sel[0]
        snd = self.sfx_arc.sounds[idx]
        wav = self.sfx_arc._wrap_wav(snd['pcm'], snd['rate'])
        if self.current_sound:
            self.current_sound.stop()

        # Load into Sound and play
        sound_obj = pygame.mixer.Sound(buffer=wav)
        self.current_sound = sound_obj
        self.current_sfx_length = sound_obj.get_length()
        self.sfx_start_time = time.time()

        sound_obj.play()

        # Enable slider & start polling
        self.sfx_seek_slider.state(['!disabled'])
        self._update_sfx_time_loop()

    def stop_sfx(self):
        if self.current_sound:
            self.current_sound.stop()
        self.current_sound = None

        # Reset UI
        self.sfx_time_label.config(text="00:00 / 00:00")
        self.sfx_seek_slider.config(value=0.0)
        self.sfx_seek_slider.state(['disabled'])

    def _update_sfx_time_loop(self):
        if not self.current_sound or not pygame.mixer.get_busy():
            # Playback finished
            self.sfx_time_label.config(text="00:00 / 00:00")
            self.sfx_seek_slider.config(value=0.0)
            return

        elapsed = time.time() - self.sfx_start_time
        total = self.current_sfx_length or 1.0
        cur_m, cur_s = divmod(int(elapsed), 60)
        tot_m, tot_s = divmod(int(total), 60)
        self.sfx_time_label.config(text=f"{cur_m:02d}:{cur_s:02d} / {tot_m:02d}:{tot_s:02d}")
        self.sfx_seek_slider.config(value=min(elapsed / total, 1.0))

        self.after(100, self._update_sfx_time_loop)

    def on_sfx_seek(self, slider_value):
        if not self.current_sound or self.current_sfx_length <= 0:
            return

        ratio = float(slider_value)
        target = ratio * self.current_sfx_length

        # Rough seek: stop current and play from byte offset
        # Convert PCM to NumPy for precise slicing is more accurate,
        # but here we restart from approximate time by reloading buffer.
        idx = self.sfx_listbox.curselection()[0]
        snd = self.sfx_arc.sounds[idx]
        pcm = snd['pcm']
        rate = snd['rate']
        total_samples = len(pcm) // 2  # 2 bytes per sample

        sample_target = int(ratio * total_samples)
        # slice raw PCM from sample_target onward
        pcm_array = pcm[sample_target*2:]
        wav = self.sfx_arc._wrap_wav(pcm_array, rate)

        if self.current_sound:
            self.current_sound.stop()

        new_sound = pygame.mixer.Sound(buffer=wav)
        self.current_sound = new_sound
        self.current_sfx_length = new_sound.get_length()
        self.sfx_start_time = time.time()
        new_sound.play()

        self._update_sfx_time_loop()

    def _update_progress(self, val, total):
        self.progress.config(maximum=total, value=val)
        self.update_idletasks()


if __name__ == '__main__':
    App().mainloop()
