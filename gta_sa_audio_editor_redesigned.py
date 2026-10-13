"""
GTA San Andreas Audio Editor — redesigned foundation
----------------------------------------------------
Python 3.10+ recommended.
Dependencies: pygame (playback); Tkinter is included with most Python installs.

Supports:
- Stream archive browse / play / export / replace / rebuild
- SFX browse / play / export / replace / rebuild
- Full-length SFX replacements by rebuilding affected package layouts
- BankLkup.dat offsets and sizes updated when banks grow/shrink
- Automatic backups before writing game files
- Strict WAV validation: PCM, mono, 16-bit (GTA SA SFX format)
- Tkinter UI operations kept on the UI thread

Important:
Test on a COPY of the GTA SA audio folder first. GTA SA mods/variants may
have different layouts. The code validates retail-style 4,804-byte SFX banks.
"""

from __future__ import annotations

import io
import shutil
import struct
import tempfile
import threading
import time
import wave
from collections import defaultdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import pygame
except ImportError:
    pygame = None


# ------------------------------ Format constants -----------------------------

ENCODE_KEY = bytes.fromhex("EA3AC4A19AA814F348B0D7239DE8FFF1")
TRACK_HEADER_SIZE = 8068
CONFIG_DIR = Path("audio/CONFIG")
SFX_DIR = Path("audio/SFX")
BANK_HEADER_SIZE = 4804
MAX_SOUNDS_PER_BANK = 400
DEFAULT_SAMPLE_RATE = 22050


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def backup_file(path: Path) -> Path:
    """Create a timestamped side-by-side backup and return its path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.backup-{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{stamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    return backup


def xor_in_place(data: bytearray, key: bytes, progress=None) -> None:
    if not key:
        raise ValueError("XOR key cannot be empty")
    total = len(data)
    for i in range(total):
        data[i] ^= key[i % len(key)]
        if progress and (i % 65536 == 0 or i + 1 == total):
            progress(i + 1, total)


# -------------------------------- Stream files -------------------------------

class StreamArchive:
    def __init__(self, path, progress_callback=None):
        self.filepath = Path(path)
        self.tracks = []
        self._decode_and_parse(progress_callback)

    def _decode_and_parse(self, progress_callback=None):
        data = bytearray(self.filepath.read_bytes())
        xor_in_place(data, ENCODE_KEY, progress_callback)
        total = len(data)
        offset = 0
        idx = 1

        while offset + TRACK_HEADER_SIZE <= total:
            header = bytes(data[offset:offset + TRACK_HEADER_SIZE])
            length = None
            for j in range(8):
                pos = 8000 + j * 8
                if pos + 4 > len(header):
                    break
                candidate = struct.unpack_from("<I", header, pos)[0]
                if candidate != 0xCDCDCDCD:
                    length = candidate
                    break
            if length is None:
                break
            start = offset + TRACK_HEADER_SIZE
            end = start + length
            if length < 0 or end > total:
                break
            self.tracks.append({
                "header": header,
                "data": bytes(data[start:end]),
                "name": f"{self.filepath.stem}_{idx:04d}",
            })
            offset = end
            idx += 1

    def export(self, idx, out_dir):
        track = self.tracks[idx]
        out = Path(out_dir) / f"{track['name']}.ogg"
        out.write_bytes(track["data"])
        return out

    def export_all(self, out_dir, progress=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in range(len(self.tracks)):
            self.export(i, out_dir)
            if progress:
                progress(i + 1, len(self.tracks))

    def replace(self, idx, newfile):
        data = Path(newfile).read_bytes()
        if not data:
            raise ValueError("Replacement audio file is empty.")
        if len(data) > 0xFFFFFFFF:
            raise ValueError("Replacement stream exceeds the 32-bit track length limit.")
        track = self.tracks[idx]
        header = bytearray(track["header"])
        # The stream header stores the OGG byte length in one of the eight
        # uint32 pairs beginning at offset 8000. Update the same pair the
        # parser uses, so the rebuilt archive remains parseable after resizing.
        updated = False
        for j in range(8):
            pos = 8000 + j * 8
            candidate = struct.unpack_from("<I", header, pos)[0]
            if candidate != 0xCDCDCDCD:
                struct.pack_into("<I", header, pos, len(data))
                updated = True
                break
        if not updated:
            raise ValueError("Could not find a valid track length field in the stream header.")
        track["header"] = bytes(header)
        track["data"] = data

    def rebuild(self, progress=None):
        total = sum(len(t["header"]) + len(t["data"]) for t in self.tracks)
        out = bytearray()
        for i, track in enumerate(self.tracks, 1):
            if len(track["header"]) != TRACK_HEADER_SIZE:
                raise ValueError(f"Invalid header for {track['name']}")
            out.extend(track["header"])
            out.extend(track["data"])
            if progress:
                progress(len(out), total)

        xor_in_place(out, ENCODE_KEY, progress)
        backup_file(self.filepath)
        self.filepath.write_bytes(out)


# -------------------------------- SFX archives -------------------------------

class SFXArchive:
    """
    GTA SA retail-style SFX package reader/writer.

    BankLkup record:
      uint8 package index, 3 padding bytes, uint32 header offset,
      uint32 PCM-data size (does not include the 4,804-byte header).

    Bank header:
      uint32-ish count/padding region, followed by 400 x 12-byte descriptors.
      Descriptor = uint32 PCM offset, int32 loop offset, uint16 sample rate,
                   int16 trim/headroom.
    """

    def __init__(self, root, progress_callback=None):
        self.root = Path(root)
        self.config = self.root / CONFIG_DIR
        self.sfx_dir = self.root / SFX_DIR
        self.sounds = []
        self.packages = []
        self.bank_records = []
        self._load(progress_callback)

    def _load(self, progress_callback=None):
        pak_path = self.config / "PakFiles.dat"
        lookup_path = self.config / "BankLkup.dat"
        if not self.config.is_dir():
            raise FileNotFoundError(f"CONFIG folder not found:\n{self.config}")
        if not pak_path.is_file():
            raise FileNotFoundError(f"PakFiles.dat not found:\n{pak_path}")
        if not lookup_path.is_file():
            raise FileNotFoundError(f"BankLkup.dat not found:\n{lookup_path}")

        pak_data = pak_path.read_bytes()
        if len(pak_data) % 52:
            raise ValueError("PakFiles.dat size is not a multiple of 52 bytes.")
        self.packages = [
            chunk.split(b"\0", 1)[0].decode("ascii", errors="replace")
            for chunk in (pak_data[i:i + 52] for i in range(0, len(pak_data), 52))
            if chunk.split(b"\0", 1)[0]
        ]

        lookup_data = lookup_path.read_bytes()
        if len(lookup_data) % 12:
            raise ValueError("BankLkup.dat size is not a multiple of 12 bytes.")

        for record_index, (pkg_idx, off, size) in enumerate(
            struct.iter_unpack("<B3xII", lookup_data)
        ):
            if pkg_idx >= len(self.packages):
                continue
            record = {
                "record_index": record_index,
                "pkg_idx": pkg_idx,
                "offset": off,
                "size": size,
            }
            self.bank_records.append(record)

        total = len(self.bank_records)
        for n, record in enumerate(self.bank_records, 1):
            if progress_callback:
                progress_callback(n, max(total, 1))
            pkg_name = self.packages[record["pkg_idx"]]
            pfile = self.sfx_dir / pkg_name
            if not pfile.is_file():
                continue

            data = pfile.read_bytes()
            off, size = record["offset"], record["size"]
            header_end = off + BANK_HEADER_SIZE
            pcm_end = header_end + size
            if off < 0 or header_end > len(data) or pcm_end > len(data):
                continue

            header = data[off:header_end]
            # Retail files use a 32-bit count at byte 0; uint16 works for them too.
            count = struct.unpack_from("<I", header, 0)[0]
            if count > MAX_SOUNDS_PER_BANK:
                # Be defensive with non-retail/unknown layouts.
                count16 = struct.unpack_from("<H", header, 0)[0]
                if count16 > MAX_SOUNDS_PER_BANK:
                    continue
                count = count16

            descriptors = []
            valid = True
            for si in range(count):
                base = 4 + si * 12
                buf_off, loop_off, rate, trim = struct.unpack_from("<IiHh", header, base)
                descriptors.append((buf_off, loop_off, rate, trim))
            if any(descriptors[i][0] > descriptors[i + 1][0]
                   for i in range(max(0, count - 1))):
                valid = False
            if not valid:
                continue

            for si, (buf_off, loop_off, rate, trim) in enumerate(descriptors):
                next_off = descriptors[si + 1][0] if si + 1 < count else size
                if buf_off > next_off or next_off > size:
                    continue
                start = header_end + buf_off
                end = header_end + next_off
                pcm = data[start:end]
                if len(pcm) % 2:
                    # PCM should be signed 16-bit mono; odd data is malformed.
                    continue
                self.sounds.append({
                    "pkg_file": pfile,
                    "pkg_idx": record["pkg_idx"],
                    "bank_record_index": record["record_index"],
                    "header_off": off,
                    "bank_size": size,
                    "sound_index": si,
                    "pcm_offset": buf_off,
                    "pcm": pcm,
                    "rate": rate or DEFAULT_SAMPLE_RATE,
                    "loop_offset": loop_off,
                    "trim": trim,
                    "name": f"{pkg_name}_bank{record['record_index']:03d}_sfx{si:03d}",
                })

    @staticmethod
    def _read_wav(path):
        with wave.open(str(path), "rb") as wf:
            if wf.getcomptype() != "NONE":
                raise ValueError("Use an uncompressed PCM WAV file.")
            if wf.getnchannels() != 1:
                raise ValueError("GTA SA SFX replacements must be mono WAV files.")
            if wf.getsampwidth() != 2:
                raise ValueError("GTA SA SFX replacements must be 16-bit PCM WAV files.")
            rate = wf.getframerate()
            if not 4000 <= rate <= 48000:
                raise ValueError("Sample rate must be between 4,000 and 48,000 Hz.")
            pcm = wf.readframes(wf.getnframes())
        if not pcm or len(pcm) % 2:
            raise ValueError("Replacement WAV has no valid 16-bit PCM frames.")
        return pcm, rate

    @staticmethod
    def _wrap_wav(pcm, rate):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm)
        return buf.getvalue()

    def export(self, idx, out_dir):
        sound = self.sounds[idx]
        out = Path(out_dir) / f"{sound['name']}.wav"
        out.write_bytes(self._wrap_wav(sound["pcm"], sound["rate"]))
        return out

    def export_all(self, out_dir, progress=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        total = len(self.sounds)
        for i in range(total):
            self.export(i, out_dir)
            if progress:
                progress(i + 1, max(total, 1))

    def replace(self, idx, newfile):
        pcm, rate = self._read_wav(newfile)
        sound = self.sounds[idx]
        sound["pcm"] = pcm
        sound["rate"] = rate
        # Existing loop offset is in samples according to the format reference.
        # Keep it only if it remains inside the replacement.
        samples = len(pcm) // 2
        if sound["loop_offset"] < -1 or sound["loop_offset"] >= samples:
            sound["loop_offset"] = -1

    def rebuild(self, progress=None):
        """
        Rebuild every bank represented by the loaded lookup table, then package
        files and BankLkup.dat. Sound count/order and loop/trim metadata persist.
        Any replacement may grow or shrink; offsets are recomputed from scratch.
        """
        lookup_path = self.config / "BankLkup.dat"
        lookup = bytearray(lookup_path.read_bytes())
        records_by_index = {r["record_index"]: dict(r) for r in self.bank_records}

        # Index all sounds by their bank lookup record and sound index.
        sounds_by_bank = defaultdict(dict)
        for sound in self.sounds:
            sounds_by_bank[sound["bank_record_index"]][sound["sound_index"]] = sound

        # Load all banks, including any banks with zero valid/parsed sounds.
        banks_by_package = defaultdict(list)
        for record in self.bank_records:
            pkg_name = self.packages[record["pkg_idx"]]
            pfile = self.sfx_dir / pkg_name
            if not pfile.is_file():
                continue
            original = pfile.read_bytes()
            off, size = record["offset"], record["size"]
            if off + BANK_HEADER_SIZE + size > len(original):
                raise ValueError(f"Bank bounds invalid in {pfile.name} at {off}.")
            header = bytearray(original[off:off + BANK_HEADER_SIZE])
            count = struct.unpack_from("<I", header, 0)[0]
            if count > MAX_SOUNDS_PER_BANK:
                count = struct.unpack_from("<H", header, 0)[0]
            if count > MAX_SOUNDS_PER_BANK:
                raise ValueError(f"Unsupported sound count in {pfile.name} bank {off}.")

            parsed = []
            old_pcm_start = off + BANK_HEADER_SIZE
            for si in range(count):
                base = 4 + si * 12
                buf_off, loop_off, rate, trim = struct.unpack_from("<IiHh", header, base)
                next_off = (
                    struct.unpack_from("<I", header, base + 12)[0]
                    if si < count - 1 else size
                )
                if buf_off > next_off or next_off > size:
                    raise ValueError(f"Invalid sound offsets in {pfile.name}, bank {off}.")
                old_pcm = original[old_pcm_start + buf_off:old_pcm_start + next_off]
                sound = sounds_by_bank.get(record["record_index"], {}).get(si)
                pcm = sound["pcm"] if sound else old_pcm
                if len(pcm) % 2:
                    raise ValueError(f"Odd-sized PCM data in {pfile.name}, bank {off}, sound {si}.")
                if sound:
                    rate = sound["rate"]
                    loop_off = sound["loop_offset"]
                    trim = sound["trim"]
                parsed.append({
                    "pcm": pcm,
                    "loop": loop_off,
                    "rate": rate or DEFAULT_SAMPLE_RATE,
                    "trim": trim,
                })

            # Update the bank header's descriptors, preserving the unused tail.
            new_header = bytearray(header)
            struct.pack_into("<I", new_header, 0, count)
            pcm_data = bytearray()
            for si, entry in enumerate(parsed):
                base = 4 + si * 12
                struct.pack_into(
                    "<IiHh", new_header, base,
                    len(pcm_data), entry["loop"], entry["rate"], entry["trim"]
                )
                pcm_data.extend(entry["pcm"])
            new_bank = bytes(new_header) + bytes(pcm_data)
            banks_by_package[record["pkg_idx"]].append({
                "record": record,
                "data": new_bank,
                "new_size": len(pcm_data),
                "old_offset": record["offset"],
            })

        # Rebuild package files in original bank order. Retail archives tile
        # banks contiguously; preserve any prefix before the first indexed bank.
        rebuilt_packages = {}
        for pkg_idx, banks in banks_by_package.items():
            pkg_name = self.packages[pkg_idx]
            pfile = self.sfx_dir / pkg_name
            original = pfile.read_bytes()
            banks.sort(key=lambda b: b["old_offset"])
            if not banks:
                continue
            prefix_end = banks[0]["old_offset"]
            output = bytearray(original[:prefix_end])
            cursor = len(output)
            for bank in banks:
                # If there are gaps between banks, preserve them as opaque bytes.
                old_start = bank["old_offset"]
                if old_start > cursor:
                    gap_len = old_start - cursor
                    output.extend(original[cursor:old_start])
                new_offset = len(output)
                output.extend(bank["data"])
                rec = bank["record"]
                rec["new_offset"] = new_offset
                rec["new_size"] = bank["new_size"]
                cursor = old_start + BANK_HEADER_SIZE + rec["size"]
                if progress:
                    progress(len(output), max(len(original), len(output), 1))
            # Preserve trailing bytes not covered by the final original bank.
            if cursor < len(original):
                output.extend(original[cursor:])
            rebuilt_packages[pfile] = bytes(output)

        # Validate all new lookup values fit uint32 and update lookup entries.
        for record in self.bank_records:
            if "new_offset" not in record:
                # This package/bank wasn't rebuilt; keep original values.
                continue
            if record["new_offset"] > 0xFFFFFFFF or record["new_size"] > 0xFFFFFFFF:
                raise ValueError("Rebuilt bank exceeds the 32-bit lookup format limit.")
            pos = record["record_index"] * 12
            struct.pack_into("<II", lookup, pos + 4,
                             record["new_offset"], record["new_size"])

        # Stage all output in memory and create backups before replacing files.
        targets = list(rebuilt_packages) + [lookup_path]
        backups = {}
        for target in targets:
            backups[target] = backup_file(target)

        try:
            for target, payload in rebuilt_packages.items():
                target.write_bytes(payload)
            lookup_path.write_bytes(lookup)
        except Exception:
            # Best-effort rollback if any write fails.
            for target, backup in backups.items():
                try:
                    shutil.copy2(backup, target)
                except Exception:
                    pass
            raise


# ----------------------------------- UI --------------------------------------

class AudioEditor(tk.Tk):
    BG = "#171a21"
    PANEL = "#222733"
    PANEL_2 = "#2c3342"
    FG = "#edf1f7"
    MUTED = "#aab4c5"
    ACCENT = "#43d9b5"
    WARN = "#ffbd69"

    def __init__(self):
        super().__init__()
        self.title("GTA San Andreas Audio Editor")
        self.geometry("1020x680")
        self.minsize(850, 560)
        self.configure(bg=self.BG)

        self.stream_arc = None
        self.sfx_arc = None
        self.active_channel = None
        self.active_sound = None
        self.stream_temp = None
        self.stream_duration = 0.0
        self.sfx_duration = 0.0
        self.play_started_at = 0.0
        self.sfx_seek_base = 0.0
        self._seek_dragging = False
        self._load_token = 0

        self._setup_style()
        self._setup_audio()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.FG)
        style.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED)
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.FG)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 19), foreground=self.FG)
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground=self.MUTED)
        style.configure("TButton", padding=(12, 7), background=self.PANEL_2, foreground=self.FG)
        style.map("TButton", background=[("active", "#3a4559")])
        style.configure("Accent.TButton", background=self.ACCENT, foreground="#10221f",
                        padding=(14, 8), font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", "#6de8cc")])
        style.configure("TNotebook", background=self.BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 10), background=self.PANEL,
                        foreground=self.MUTED)
        style.map("TNotebook.Tab",
                  background=[("selected", self.PANEL_2)],
                  foreground=[("selected", self.ACCENT)])
        style.configure("Treeview", background=self.PANEL, fieldbackground=self.PANEL,
                        foreground=self.FG, rowheight=28, borderwidth=0)
        style.configure("Treeview.Heading", background=self.PANEL_2, foreground=self.FG,
                        font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#365a59")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Horizontal.TProgressbar", troughcolor=self.PANEL_2,
                        background=self.ACCENT, bordercolor=self.PANEL_2)

    def _setup_audio(self):
        if pygame is None:
            self.audio_available = False
            return
        try:
            pygame.mixer.init()
            pygame.mixer.set_num_channels(16)
            self.audio_available = True
        except Exception:
            self.audio_available = False

    def _build_ui(self):
        header = ttk.Frame(self, padding=(22, 18, 22, 12))
        header.pack(fill="x")
        title_block = ttk.Frame(header)
        title_block.pack(side="left", fill="x", expand=True)
        ttk.Label(title_block, text="SAN ANDREAS  /  AUDIO WORKSHOP",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_block, text="Stream archives and SFX banks",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        self.root_status = ttk.Label(header, text="NO ARCHIVE LOADED", style="Muted.TLabel")
        self.root_status.pack(side="right", anchor="e")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self.stream_tab = ttk.Frame(self.notebook, padding=14)
        self.sfx_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.stream_tab, text="  STREAM  ")
        self.notebook.add(self.sfx_tab, text="  SFX BANKS  ")
        self._build_archive_tab(self.stream_tab, "stream")
        self._build_archive_tab(self.sfx_tab, "sfx")

        footer = ttk.Frame(self, padding=(18, 0, 18, 14))
        footer.pack(fill="x")
        self.status = ttk.Label(footer, text="Ready", style="Muted.TLabel")
        self.status.pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100, length=220)
        self.progress.pack(side="right", padx=(12, 0))

    def _build_archive_tab(self, parent, kind):
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 12))
        if kind == "stream":
            actions = [
                ("Load Stream", self.load_stream, "Accent.TButton"),
                ("Export", lambda: self.export_selected("stream"), "TButton"),
                ("Export All", lambda: self.export_all("stream"), "TButton"),
                ("Replace", lambda: self.replace_selected("stream"), "TButton"),
                ("Rebuild", self.rebuild_stream, "TButton"),
            ]
        else:
            actions = [
                ("Load GTA Folder", self.load_sfx, "Accent.TButton"),
                ("Export", lambda: self.export_selected("sfx"), "TButton"),
                ("Export All", lambda: self.export_all("sfx"), "TButton"),
                ("Replace", lambda: self.replace_selected("sfx"), "TButton"),
                ("Rebuild Banks", self.rebuild_sfx, "TButton"),
            ]
        for label, command, style in actions:
            ttk.Button(top, text=label, command=command, style=style).pack(side="left", padx=(0, 7))

        search_row = ttk.Frame(parent)
        search_row.pack(fill="x", pady=(0, 8))
        ttk.Label(search_row, text="FILTER", style="Muted.TLabel").pack(side="left", padx=(0, 9))
        search_var = tk.StringVar()
        search = ttk.Entry(search_row, textvariable=search_var)
        search.pack(side="left", fill="x", expand=True)
        ttk.Label(search_row, text="Select an item to preview or replace it",
                  style="Muted.TLabel").pack(side="right", padx=(10, 0))

        columns = ("name", "duration", "format")
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
        tree.heading("name", text="AUDIO ASSET")
        tree.heading("duration", text="DURATION")
        tree.heading("format", text="TYPE / DETAILS")
        tree.column("name", width=440, anchor="w")
        tree.column("duration", width=100, anchor="center")
        tree.column("format", width=260, anchor="w")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Each row maps to an archive index; filtered rows keep the original index.
        if kind == "stream":
            self.stream_tree = tree
            self.stream_search = search_var
            self.stream_rows = {}
            search_var.trace_add("write", lambda *_: self._filter_tree("stream"))
            tree.bind("<Double-1>", lambda _e: self.play_selected("stream"))
            tree.bind("<<TreeviewSelect>>", lambda _e: self._select_status("stream"))
        else:
            self.sfx_tree = tree
            self.sfx_search = search_var
            self.sfx_rows = {}
            search_var.trace_add("write", lambda *_: self._filter_tree("sfx"))
            tree.bind("<Double-1>", lambda _e: self.play_selected("sfx"))
            tree.bind("<<TreeviewSelect>>", lambda _e: self._select_status("sfx"))

        player = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 12))
        player.pack(side="bottom", fill="x", pady=(12, 0))
        info = ttk.Frame(player, style="Panel.TFrame")
        info.pack(fill="x")
        label = ttk.Label(info, text="NOTHING PLAYING", style="Panel.TLabel",
                          font=("Segoe UI Semibold", 10))
        label.pack(side="left")
        time_label = ttk.Label(info, text="00:00 / 00:00", style="Panel.TLabel")
        time_label.pack(side="right")
        seek = ttk.Scale(player, from_=0, to=1, orient="horizontal")
        seek.pack(fill="x", pady=(8, 10))
        seek.state(["disabled"])
        controls = ttk.Frame(player, style="Panel.TFrame")
        controls.pack(fill="x")
        ttk.Button(controls, text="▶  Play", command=lambda k=kind: self.play_selected(k),
                   style="Accent.TButton").pack(side="left")
        ttk.Button(controls, text="■  Stop", command=lambda k=kind: self.stop_playback(k)).pack(
            side="left", padx=(8, 0))
        ttk.Label(controls, text="Preview is non-destructive until Rebuild",
                  style="Panel.TLabel").pack(side="right")

        if kind == "stream":
            self.stream_player_label, self.stream_time_label, self.stream_seek = label, time_label, seek
            seek.configure(command=self._stream_seek)
            seek.bind("<ButtonPress-1>", lambda _e: setattr(self, "_seek_dragging", True))
            seek.bind("<ButtonRelease-1>", lambda _e: self._finish_seek("stream"))
        else:
            self.sfx_player_label, self.sfx_time_label, self.sfx_seek = label, time_label, seek
            seek.configure(command=self._sfx_seek)
            seek.bind("<ButtonPress-1>", lambda _e: setattr(self, "_seek_dragging", True))
            seek.bind("<ButtonRelease-1>", lambda _e: self._finish_seek("sfx"))

    def _archive(self, kind):
        return self.stream_arc if kind == "stream" else self.sfx_arc

    def _tree(self, kind):
        return self.stream_tree if kind == "stream" else self.sfx_tree

    def _rows(self, kind):
        return self.stream_rows if kind == "stream" else self.sfx_rows

    def _filter_tree(self, kind):
        tree = self._tree(kind)
        rows = self._rows(kind)
        query = (self.stream_search if kind == "stream" else self.sfx_search).get().lower()
        for item in tree.get_children():
            tree.delete(item)
        for idx, values in rows.items():
            if query in values[0].lower():
                tree.insert("", "end", iid=str(idx), values=values)

    def _populate(self, kind):
        tree = self._tree(kind)
        rows = self._rows(kind)
        rows.clear()
        tree.delete(*tree.get_children())
        arc = self._archive(kind)
        if not arc:
            return
        if kind == "stream":
            for i, track in enumerate(arc.tracks):
                rows[i] = (track["name"], "—", "OGG stream")
        else:
            for i, sound in enumerate(arc.sounds):
                duration = len(sound["pcm"]) / 2 / max(sound["rate"], 1)
                rows[i] = (sound["name"], fmt_time(duration), f"PCM 16-bit · {sound['rate']} Hz")
        self._filter_tree(kind)

    def _selected_index(self, kind):
        selection = self._tree(kind).selection()
        return int(selection[0]) if selection else None

    def _select_status(self, kind):
        idx = self._selected_index(kind)
        arc = self._archive(kind)
        if idx is not None and arc:
            self.status.configure(text=f"Selected: {self._rows(kind)[idx][0]}")

    def _set_status(self, text):
        self.status.configure(text=text)

    def _progress(self, value, total):
        def update():
            self.progress.configure(maximum=max(total, 1), value=value)
        self.after(0, update)

    def _run_worker(self, label, fn, on_success=None):
        self._set_status(label)
        def worker():
            try:
                result = fn()
            except Exception as exc:
                self.after(0, lambda err=str(exc): self._show_error(err))
            else:
                if on_success:
                    self.after(0, lambda: on_success(result))
                self.after(0, lambda: self._set_status("Ready"))
        threading.Thread(target=worker, daemon=True).start()

    def _show_error(self, text):
        self._set_status("Operation failed")
        messagebox.showerror("GTA SA Audio Editor", text)

    def load_stream(self):
        path = filedialog.askopenfilename(
            title="Select GTA SA stream archive",
            filetypes=[("Stream archives", "*.stream"), ("All files", "*.*")]
        )
        if not path:
            return
        self._run_worker(
            "Loading and decoding stream archive…",
            lambda: StreamArchive(path, self._progress),
            lambda arc: self._stream_loaded(arc)
        )

    def _stream_loaded(self, arc):
        self.stream_arc = arc
        self._populate("stream")
        self.root_status.configure(text=f"STREAM: {arc.filepath.name}")
        self._set_status(f"Loaded {len(arc.tracks)} stream tracks")

    def load_sfx(self):
        root = filedialog.askdirectory(title="Select GTA San Andreas root folder")
        if not root:
            return
        self._run_worker(
            "Reading SFX banks…",
            lambda: SFXArchive(root, self._progress),
            lambda arc: self._sfx_loaded(arc)
        )

    def _sfx_loaded(self, arc):
        self.sfx_arc = arc
        self._populate("sfx")
        self.root_status.configure(text=f"SFX: {arc.root.name or arc.root}")
        self._set_status(f"Loaded {len(arc.sounds)} SFX sounds from {len(arc.bank_records)} banks")

    def export_selected(self, kind):
        arc = self._archive(kind)
        idx = self._selected_index(kind)
        if not arc or idx is None:
            messagebox.showwarning("Select audio", "Load an archive and select an audio item first.")
            return
        folder = filedialog.askdirectory(title="Export selected audio to")
        if not folder:
            return
        try:
            out = arc.export(idx, folder)
            self._set_status(f"Exported {out.name}")
        except Exception as exc:
            self._show_error(str(exc))

    def export_all(self, kind):
        arc = self._archive(kind)
        if not arc:
            messagebox.showwarning("Load archive", "Load an archive first.")
            return
        folder = filedialog.askdirectory(title="Export all audio to")
        if not folder:
            return
        self._run_worker(
            "Exporting audio…",
            lambda: arc.export_all(folder, self._progress),
            lambda _r: self._set_status("Export complete")
        )

    def replace_selected(self, kind):
        arc = self._archive(kind)
        idx = self._selected_index(kind)
        if not arc or idx is None:
            messagebox.showwarning("Select audio", "Load an archive and select an audio item first.")
            return
        if kind == "stream":
            path = filedialog.askopenfilename(
                title="Choose replacement OGG",
                filetypes=[("OGG audio", "*.ogg"), ("All files", "*.*")]
            )
        else:
            path = filedialog.askopenfilename(
                title="Choose replacement WAV (mono, 16-bit PCM)",
                filetypes=[("WAV audio", "*.wav")]
            )
        if not path:
            return
        try:
            arc.replace(idx, path)
            self._populate(kind)
            self._set_status(f"Replacement loaded in memory: {Path(path).name}")
            messagebox.showinfo("Replacement loaded", "Replacement is in memory. Use Rebuild to write it.")
        except Exception as exc:
            self._show_error(str(exc))

    def rebuild_stream(self):
        if not self.stream_arc:
            messagebox.showwarning("Load stream", "Load a stream archive first.")
            return
        if not messagebox.askyesno("Rebuild stream", "A backup will be created before writing. Continue?"):
            return
        self._run_worker(
            "Rebuilding stream archive…",
            lambda: self.stream_arc.rebuild(self._progress),
            lambda _r: messagebox.showinfo("Rebuild complete", "Stream rebuilt. A backup was created.")
        )

    def rebuild_sfx(self):
        if not self.sfx_arc:
            messagebox.showwarning("Load SFX", "Load a GTA SA root folder first.")
            return
        if not messagebox.askyesno(
            "Rebuild SFX banks",
            "This rewrites SFX package files and BankLkup.dat. Backups will be created first.\n\n"
            "Test on a copy of the game folder. Continue?"
        ):
            return
        self._run_worker(
            "Rebuilding SFX banks and lookup offsets…",
            lambda: self.sfx_arc.rebuild(self._progress),
            lambda _r: messagebox.showinfo(
                "SFX rebuild complete",
                "SFX banks rebuilt and lookup offsets updated. Backups were created."
            )
        )

    def play_selected(self, kind):
        if not self.audio_available:
            messagebox.showerror(
                "Audio unavailable",
                "pygame audio could not initialize. Install pygame and check your audio device."
            )
            return
        arc = self._archive(kind)
        idx = self._selected_index(kind)
        if not arc or idx is None:
            messagebox.showwarning("Select audio", "Load an archive and select an audio item first.")
            return
        self.stop_playback("stream")
        self.stop_playback("sfx")
        try:
            if kind == "stream":
                data = arc.tracks[idx]["data"]
                temp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
                temp.write(data)
                temp.close()
                try:
                    pygame.mixer.music.load(temp.name)
                    duration = pygame.mixer.Sound(temp.name).get_length()
                except Exception:
                    Path(temp.name).unlink(missing_ok=True)
                    raise
                self.stream_temp = temp.name
                self.stream_duration = duration
                pygame.mixer.music.play()
                self.active_channel = "stream"
                self.stream_player_label.configure(text=arc.tracks[idx]["name"])
                self.stream_time_label.configure(text=f"00:00 / {fmt_time(duration)}")
                self.stream_seek.configure(value=0)
                self.stream_seek.state(["!disabled"])
            else:
                sound = arc.sounds[idx]
                wav = arc._wrap_wav(sound["pcm"], sound["rate"])
                snd = pygame.mixer.Sound(buffer=wav)
                channel = snd.play()
                if channel is None:
                    raise RuntimeError("No free mixer channel is available.")
                self.active_sound = snd
                self.active_channel_obj = channel
                self.sfx_duration = snd.get_length()
                self.sfx_seek_base = 0.0
                self.play_started_at = time.monotonic()
                self.active_channel = "sfx"
                self.sfx_player_label.configure(text=sound["name"])
                self.sfx_time_label.configure(text=f"00:00 / {fmt_time(self.sfx_duration)}")
                self.sfx_seek.configure(value=0)
                self.sfx_seek.state(["!disabled"])
            self._set_status(f"Playing {kind.upper()} preview")
            self._poll_playback()
        except Exception as exc:
            self._show_error(f"Playback failed:\n{exc}")

    def stop_playback(self, kind):
        if kind == "stream":
            try:
                if pygame and self.audio_available:
                    pygame.mixer.music.stop()
            except Exception:
                pass
            if self.stream_temp:
                Path(self.stream_temp).unlink(missing_ok=True)
                self.stream_temp = None
            self.stream_seek.state(["disabled"])
            self.stream_seek.configure(value=0)
            self.stream_time_label.configure(text="00:00 / 00:00")
            self.stream_player_label.configure(text="NOTHING PLAYING")
        else:
            try:
                if getattr(self, "active_channel_obj", None):
                    self.active_channel_obj.stop()
            except Exception:
                pass
            self.active_sound = None
            self.active_channel_obj = None
            self.sfx_seek.state(["disabled"])
            self.sfx_seek.configure(value=0)
            self.sfx_time_label.configure(text="00:00 / 00:00")
            self.sfx_player_label.configure(text="NOTHING PLAYING")
        if self.active_channel == kind:
            self.active_channel = None

    def _stream_seek(self, value):
        if self._seek_dragging or self.active_channel != "stream" or not self.stream_temp:
            return
        # Seeking is committed on mouse release to avoid restarting during every slider tick.

    def _sfx_seek(self, value):
        # Commit only when the user releases the slider.
        return

    def _finish_seek(self, kind):
        self._seek_dragging = False
        if not self.audio_available:
            return
        if kind == "stream" and self.active_channel == "stream" and self.stream_temp:
            target = float(self.stream_seek.get()) * self.stream_duration
            try:
                pygame.mixer.music.play(start=target)
                self._set_status(f"Seeking to {fmt_time(target)}")
            except Exception:
                # pygame music seeking support varies by codec/backend.
                self._set_status("This OGG backend does not support reliable seeking.")
        elif kind == "sfx" and self.active_channel == "sfx":
            idx = self._selected_index("sfx")
            if idx is None or not self.sfx_arc:
                return
            sound = self.sfx_arc.sounds[idx]
            ratio = float(self.sfx_seek.get())
            total_samples = len(sound["pcm"]) // 2
            sample_target = min(total_samples, int(ratio * total_samples))
            pcm = sound["pcm"][sample_target * 2:]
            if not pcm:
                self.stop_playback("sfx")
                return
            try:
                if self.active_channel_obj:
                    self.active_channel_obj.stop()
                snd = pygame.mixer.Sound(buffer=self.sfx_arc._wrap_wav(pcm, sound["rate"]))
                channel = snd.play()
                self.active_sound = snd
                self.active_channel_obj = channel
                self.sfx_seek_base = sample_target / sound["rate"]
                self.play_started_at = time.monotonic()
                self.sfx_duration = len(sound["pcm"]) / 2 / sound["rate"]
            except Exception as exc:
                self._show_error(f"SFX seek failed:\n{exc}")

    def _poll_playback(self):
        if not self.winfo_exists():
            return
        if self.active_channel == "stream":
            if not pygame.mixer.music.get_busy():
                self.stop_playback("stream")
                return
            pos = max(0, pygame.mixer.music.get_pos() / 1000.0)
            duration = max(self.stream_duration, 0.001)
            self.stream_time_label.configure(text=f"{fmt_time(pos)} / {fmt_time(duration)}")
            if not self._seek_dragging:
                self.stream_seek.configure(value=min(1, pos / duration))
        elif self.active_channel == "sfx":
            channel = getattr(self, "active_channel_obj", None)
            if not channel or not channel.get_busy():
                self.stop_playback("sfx")
                return
            elapsed = time.monotonic() - self.play_started_at + self.sfx_seek_base
            duration = max(self.sfx_duration, 0.001)
            self.sfx_time_label.configure(text=f"{fmt_time(elapsed)} / {fmt_time(duration)}")
            if not self._seek_dragging:
                self.sfx_seek.configure(value=min(1, elapsed / duration))
        else:
            return
        self.after(100, self._poll_playback)

    def on_close(self):
        try:
            self.stop_playback("stream")
            self.stop_playback("sfx")
            if pygame and self.audio_available:
                pygame.mixer.quit()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = AudioEditor()
    app.mainloop()
