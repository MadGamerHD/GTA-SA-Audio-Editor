# GTA SA Audio Editor

A standalone Python tool to extract, replace, and rebuild audio assets in GTA San Andreas. It supports:

* Decrypting and parsing "stream files" to export or replace OGG tracks.
* Loading GTA SA SFX packs to export or replace individual WAV sounds.
* In‑place rebuilding of modified files (with automatic re‑encryption for stream files).
* Real‑time playback with current time/total duration display and seek controls for both Stream and SFX tabs.

### 🔧 Features

- ✅ OGG/WAV export and replacement
- ✅ Rebuild with automatic re-encryption
- ✅ Real-time preview with seeking
- Bug, Don't Use On SFX ATM

## Preview

![Screenshot 2025-06-01 192148](https://github.com/user-attachments/assets/41910e9b-21d6-46bb-9dc1-b92a9e833b35)


## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Directory Structure](#directory-structure)
4. [How It Works](#how-it-works)

   * [StreamArchive](#streamarchive)
   * [SFXArchive](#sfxarchive)
   * [GUI (App)](#gui-app)
5. [Usage](#usage)

   * [Launching the Tool](#launching-the-tool)
   * [Stream Tab](#stream-tab)
   * [SFX Tab](#sfx-tab)
6. [Features Overview](#features-overview)
7. [Troubleshooting](#troubleshooting)
8. [Known Limitations](#known-limitations)
9. [License & Attribution](#license--attribution)

---

## Prerequisites

* **Python 3.8+** (tested on 3.9 and 3.10)
* **Required Python libraries:**

  * `pygame` – for audio playback and duration querying
  * `tkinter` (built into standard library) – for the GUI
  * Standard libraries: `struct`, `threading`, `pathlib`, `tempfile`, `wave`, `io`, `collections`

To install `pygame`, run:

```bash
pip install pygame
```

No other external dependencies (e.g. ffmpeg) are needed. All parsing, decryption, and audio wrapping is done in pure Python.

## Installation

1. Ensure you have Python 3.8 or newer installed.
2. Install `pygame`:

   ```sh
   pip install pygame
   ```
3. Place the provided script (e.g. `gta_sa_audio_editor.py`) somewhere on your disk.
4. Verify that your GTA SA installation is accessible (i.e., you know the root folder containing `audio/CONFIG` and `audio/SFX`).

No compilation or build step is required. The tool is a self‑contained Python script.

## Directory Structure

```txt
(gta_sa_audio_editor.py)   # Main script

<some folder>/              # GTA SA root
└── audio/
    ├── CONFIG/
    │   ├── PakFiles.dat
    │   └── BankLkup.dat
    └── SFX/
        ├── <bank1>
        ├── <bank2>
        └── ...
```

* "stream files" can be located anywhere; you choose them via a file dialog.
* SFX banks must be under `<GTA_SA_root>/audio/SFX/` alongside the `PakFiles.dat` and `BankLkup.dat` in `audio/CONFIG/`.

## How It Works

### StreamArchive

1. **Initialization (`__init__`)**

   * Takes the path to a stream file and an optional progress callback.
   * Reads the entire file into a `bytearray`.

2. **Decryption (“\_decode\_and\_parse”)**

   * Applies a single-pass XOR with a 16-byte key (`EA 3A C4 A1 9A A8 14 F3 48 B0 D7 23 9D E8 FF F1`) to decrypt in-place.
   * Progress updates every 4096 bytes via the callback.

3. **Parsing Tracks**

   * Iterates through the decrypted buffer:

     * Reads a fixed-size header (8068 bytes).
     * Inside the header (starting at byte offset 8000), searches up to 8 little-endian `<I, I>` pairs to find a valid length (not `0xCDCDCDCD`).
     * Uses that length to slice out the OGG data immediately following the header.
     * Stores:

       * `header`: raw 8068-byte header as `bytes`
       * `data`: raw OGG payload as `bytes`
       * `name`: `<basename>_<track_index>` (for exporting)
   * Repeats until no more full headers remain.

4. **Export**

   * `export(idx, out_dir)`: writes `data` of track `idx` to `<out_dir>/<track_name>.ogg`.
   * `export_all(out_dir)`: loops over every track and calls `export()`, with optional progress updates.

5. **Replace**

   * `replace(idx, newfile)`: reads raw bytes from an OGG file on disk and replaces `tracks[idx]['data']` in memory.

6. **Rebuild**

   * Reconstructs a single decrypted buffer by concatenating each track’s `header` and `data` in order.
   * Applies the same XOR key (single-pass) to re-encrypt the entire buffer in-place.
   * Overwrites the original stream file with the re-encrypted data.
   * Supports progress callbacks as it copies headers/data and re-encrypts.

### SFXArchive

1. **Initialization (`__init__`)**

   * Requires the path to the root GTA SA folder (containing `audio/CONFIG` and `audio/SFX`).
   * Validates that `<root>/audio/CONFIG` exists.

2. **Reading `PakFiles.dat`**

   * `PakFiles.dat` is read in 52-byte entries. Each entry is a zero-padded ASCII name of an SFX bank file (under `audio/SFX/`).
   * Builds a list of bank file names (e.g. `sound1.dat`, `sound2.dat`).

3. **Reading `BankLkup.dat`**

   * Each entry is a 12-byte record: `<B, 3xpadding, I offset, I size>`.
   * Groups entries by `pkg_idx` (first byte) using a `defaultdict(list)`, so each package index maps to a list of `(offset, size)` pairs.

4. **Extracting Sounds**
   For each bank file:
   a. Read the entire file into memory as `bytes`, wrap in `memoryview`.
   b. For every `(offset, size)` from `BankLkup.dat` where `pkg_idx` matches this bank:

   * Read a fixed-size header (4804 bytes) at that offset.
   * From header’s first 2 bytes (little-endian `<H>`), get `count` = number of sub-sounds.
   * For each sub-sound `si` in `[0, count)`:

     1. Read 12 bytes at `4 + si*12` in the header, unpack `<I buf_off, I ?, H rate, H ?>`.
     2. Compute `pcm_start = offset + 4804 + buf_off`.
     3. Determine `nxt`: either the next `buf_off` or `size` if last.
     4. `length = nxt - buf_off`.
     5. If valid, slice the raw PCM: `pcm = bytes(mv_data[pcm_start : pcm_start + length])`.
     6. Store a dict:

        * `'pkg_file'`: the bank file `Path`
        * `'header_off'`: offset of header within bank
        * `'pcm_offset'`: `buf_off`
        * `'pcm'`: raw PCM bytes
        * `'rate'`: sample rate (or 22050 if zero)
        * `'name'`: `<bank_name>_b<si>` for exporting

   7. Appends each sub-sound to `self.sounds`.

5. **Export**

   * `export(idx, out_dir)`: wraps `pcm` & `rate` in a WAV container (via `wave` + `BytesIO`), writes `<out_dir>/<sound_name>.wav`.
   * `export_all(out_dir)`: loops over all sounds with optional progress callbacks.

6. **Replace**

   * `replace(idx, newfile)`: opens a WAV file, reads raw PCM frames, and replaces `self.sounds[idx]['pcm']`.

7. **Rebuild**

   * Groups sounds by their original bank file (`pkg_file`).
   * Reads each bank file from disk into a `bytearray`, then:

     * For each sound in that bank, compute `start = header_off + 4804 + pcm_offset` and `end = start + len(pcm)`, then overwrite that range in the bank’s bytearray with the new PCM.
   * Overwrites the original bank file on disk with the modified `bytearray`.

### GUI (`App` Class)

Uses `tkinter` for the main window and `ttk` for styling. The GUI consists of two tabs:

1. **Stream Tab**

   * **Buttons:** Load, Export, Expt All, Replace, Rebuild, Play, Stop
   * **Listbox:** shows decrypted track names
   * **Time / Seek Controls:**

     * `ttk.Label` to display “MM\:SS / MM\:SS”
     * `ttk.Scale` to allow seeking (0.0–1.0)

2. **SFX Tab**

   * **Buttons:** Load, Export, Expt All, Replace, Rebuild, Play, Stop
   * **Listbox:** shows extracted sound names
   * **Time / Seek Controls:**

     * `ttk.Label` for “MM\:SS / MM\:SS”
     * `ttk.Scale` for rough seeking via PCM slicing

#### Key GUI Methods

* `_update_progress(val, total)`: updates the bottom progress bar.
* `_populate_listbox(lb, items)`: fills a `Listbox` with given names.
* `load_stream()`: opens file dialog for a stream file, initializes `StreamArchive`, populates list.
* `load_sfx()`: opens directory dialog, initializes `SFXArchive`, populates list.
* `play_stream()`:

  1. Ensures a track is selected and archive is loaded.
  2. Writes decrypted OGG bytes to a temp file.
  3. Loads via `pygame.mixer.music.load()`, plays, and fetches length with `Sound.get_length()`.
  4. Enables the Stream seek slider and calls `_update_time_loop()` every 100 ms.
* `_update_time_loop()`: polls `pygame.mixer.music.get_pos()`, updates time label + slider value, schedules itself every 100 ms.
* `on_seek(value)`: computes target seconds, calls `pygame.mixer.music.play(start=target)`, or falls back to `set_pos()` for older pygame.
* `stop_stream()`: stops playback, deletes temp file, disables slider, resets label/slider.
* `play_sfx()`:

  1. Ensures an SFX is selected.
  2. Wraps raw PCM into WAV bytes, loads into `pygame.mixer.Sound`, fetches length, stores `sfx_start_time = time.time()`, plays.
  3. Enables the SFX seek slider and starts `_update_sfx_time_loop()`.
* `_update_sfx_time_loop()`: calculates elapsed = `time.time() - sfx_start_time`, updates label/slider, schedules itself every 100 ms.
* `on_sfx_seek(value)`: approximates a seek by slicing the raw PCM at the chosen sample offset, wrapping in a new `Sound` object, and playing from there.
* `stop_sfx()`: stops current `Sound`, disables slider, resets labels.
* `_on_exit()`: bound to `Exit` menu command; deletes any leftover temp files and closes the window.

## Usage

### Launching the Tool

```bash
python gta_sa_audio_editor.py
```

No additional flags are needed. A window will open with two tabs: **Stream** and **SFX**.

### Stream Tab

1. **Load** – Select a stream file. The progress bar will show decryption/parsing.
2. The Listbox populates with names like `myfile_1`, `myfile_2`, etc.
3. **Play** – Streams the selected track: writes its OGG to a temporary file, plays via `pygame`.

   * Time label shows `mm:ss / mm:ss`.
   * Seek slider becomes active once playback starts; drag to jump.
4. **Stop** – Stops playback and deletes the temp OGG.
5. **Export** – Select a track and choose an output folder; writes `<track_name>.ogg` there.
6. **Expt All** – Choose an output folder; all tracks are exported with progress updates.
7. **Replace** – Select a track, choose a new `.ogg`; it replaces the in-memory data for that track.
8. **Rebuild** – Reconstructs the stream file with any replaced tracks and re-encrypts in-place. A confirmation dialog appears on completion.

### SFX Tab

1. **Load** – Select the GTA SA root folder. The program looks for `audio/CONFIG/PakFiles.dat` and `audio/CONFIG/BankLkup.dat`, then all bank files under `audio/SFX/`. Progress bar updates as it parses.
2. The Listbox populates with names like `sfxbank_b0`, `sfxbank_b1`, etc.
3. **Play** – Wraps the selected sound as a WAV and plays it.

   * Time label shows `mm:ss / mm:ss`.
   * Seek slider becomes active on playback; dragging seeks roughly by slicing raw PCM.
4. **Stop** – Stops the current sound and resets time controls.
5. **Export** – Select a sound, choose an output folder; writes `<sound_name>.wav` there.
6. **Expt All** – Choose an output folder; exports all sounds with progress updates.
7. **Replace** – Select a sound, pick a new `.wav`; raw PCM is replaced in memory.
8. **Rebuild** – Overwrites each original SFX bank file on disk with the modified PCM data. A confirmation dialog appears when done.

## Features Overview

* **Fast decryption** of stream files using single-pass XOR, with progress updates.
* **Memory-efficient SFX parsing** by caching `BankLkup.dat` entries in a `defaultdict`.
* **Real-time playback controls** for both OGG streams and WAV-based SFX, including:

  * Current time / total duration display
  * Seek slider (Stream: via `pygame.mixer.music` start/`set_pos`; SFX: via PCM slicing)
* **Batch export** for all tracks or all SFX with unified progress feedback.
* **In-place rebuild** of stream files (with automatic re-encryption) and SFX banks.
* **Minimal external dependencies**: only `pygame` is needed in addition to the Python standard library.

## Troubleshooting

* **No audio on Play**

  * Ensure your system audio is available and not in use by another application.
  * Verify that `pygame` initialized successfully.
  * Check if the selected track or sound is valid (corrupted OGG/WAV will fail to load).

* **Seek slider doesn’t respond**

  * The slider remains disabled until playback starts.
  * For OGG seeking, some older versions of `pygame` may not support `set_pos()`; updating to `pygame 2.1+` is recommended.

* **Rebuild fails silently**

  * For stream files: ensure no external program has the file open.
  * For SFX: check folder permissions; bank files may be marked read-only.

* **Temp files not deleted on crash**

  * Manually delete any leftover `.ogg` files in your system’s temp directory.
  * Update to the latest Python and ensure `tempfile` uses the correct temp path.

## Known Limitations

* **OGG seeking** relies on `pygame.mixer.music.play(start=...)` or `set_pos()`. Not all versions of `pygame` support accurate mid-OGG seeking.
* **SFX seek is approximate**: it slices the raw PCM at a proportional sample offset, which means you might cut off mid-sample or mid‑loop.
* **Large files** may use significant RAM. Decrypting a 100 MB stream file still requires an in-memory `bytearray` of that size.
* **No undo for rebuild**: once you rebuild, the original file is overwritten. Keep backups if needed.

## License & Attribution

This tool is provided as-is. No explicit open-source license is included—feel free to adapt for personal or educational use.
