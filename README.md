**GTA Audio Editor**

A simple Python-based GUI tool for extracting, replacing, and rebuilding audio assets in **Grand Theft Auto: San Andreas**. Supports both streamed music files (`stream`) and sound effect banks (`SFX`).

---

## Features

* **Stream (stream) Handling**

  * **Load**: Decrypt and parse `stream` archives, listing individual tracks.
  * **Export**: Save selected track as `.ogg`.
  * **Export All**: Batch-export every track to a chosen folder.
  * **Replace**: Swap out a track’s audio data with a user-selected `.ogg` file.
  * **Rebuild**: Re-encrypt and rebuild the original `stream` archive with any replaced tracks.
  * **Play / Stop**: Preview individual tracks in-app using the Pygame mixer.

* **SFX (Sound Effects Bank) Handling**

  * **Load**: Read the game’s `CONFIG/PakFiles.dat`, `CONFIG/BankLkup.dat`, and SFX packages under `audio/SFX`. Automatically extract each PCM sound entry.
  * **Export**: Save a selected sound as a WAV file.
  * **Export All**: Batch-export every discovered SFX to a chosen folder.
  * **Replace**: Swap a sound effect with a user-selected `.wav` file (mono, 16‑bit PCM).
  * **Rebuild**: Write any replaced PCM data back into the original bank files, preserving bank headers.
  * **Play / Stop**: Preview individual SFX in-app via the Pygame mixer.

---

## Requirements

* **Python 3.6+**
* **Dependencies**

  * `tkinter` (standard with most Python installs)
  * `pygame` (for audio playback)
  * No other external libraries are required.

Install Pygame with:

```bash
pip install pygame
```

It’s assumed that `tkinter` is already available; if not, install it via your system’s package manager (e.g., `sudo apt install python3-tk` on Debian/Ubuntu).

---

## Usage

1. **Run the Application**

   ```bash
   python gta_audio_editor.py
   ```

   A window titled **“GTA SA Audio Editor”** will open, sized at approximately 600×400 pixels.

2. **Navigating Tabs**

   * **Stream Tab** (first tab)

     1. Click **Load** → select a `stream` file (usually found in the game’s `audio/stream` folder).

        * The tool will decrypt using the built-in key (`EA3AC4A1…`) and parse out each track’s header and data.
        * Once loaded, a list of track names (e.g., `streamfile_1`, `streamfile_2`, …) appears in the listbox.
     2. Select a track from the list to perform single-track operations.

        * **Export**: Prompts for an output folder; saves the selected track as `*.ogg`.
        * **Replace**: Prompts for an `.ogg` file; replaces the in‑memory track data.
        * **Play / Stop**: Plays or stops the currently selected track using Pygame.
     3. **Expt All (Export All)**: Prompts for an output folder, then exports every track in the `stream` archive.
     4. **Rebuild**: After any replacements, re‑encrypts and writes the updated archive back to disk. A progress bar at the bottom indicates progress. When complete, a message box confirms “Stream Rebuilt.”

   * **SFX Tab** (second tab)

     1. Click **Load** → choose the **root folder of the GTA SA installation** (the folder that contains `audio/CONFIG` and `audio/SFX`).

        * The tool reads `audio/CONFIG/PakFiles.dat` to gather package names and `audio/CONFIG/BankLkup.dat` to find bank entries (header, offsets, sizes).
        * Each discovered PCM entry is wrapped into a WAV buffer and listed as `PackageName_bX` in the listbox (e.g., `explosion.b0`, `gunshot.b3`, etc.).
     2. Select a sound from the list to perform operations:

        * **Export**: Prompts for an output folder; saves the selected sound as `*.wav` (mono, 16‑bit, native sample rate).
        * **Replace**: Prompts for a `.wav` file. The tool expects PCM data and will replace the in‑memory PCM bytes for that sound. A “Replacement successful” dialog appears once done.
        * **Play / Stop**: Plays or stops the selected SFX via Pygame mixer.
     3. **Expt All (Export All)**: Prompts for an output folder and exports every discovered sound as a `.wav`.
     4. **Rebuild**: After any replacements, writes the updated PCM bytes back into each original bank (`*.sfx` files) under `audio/SFX`. A progress bar shows rebuilding status. Upon completion, a message box confirms “All banks rebuilt.”

3. **Progress Bar**

   * Located at the bottom of the window. Any long‑running operation (decrypting, parsing, exporting all, rebuilding) updates this progress bar.

---

## Code Structure Overview

* **`StreamArchive`** (handles `stream` archives)

  * **`__init__(path, progress_callback=None)`**: Reads the file, XOR‑decrypts using `ENCODE_KEY`, and parses out individual tracks by scanning for each 8068‑byte header (`TRACK_HEADER_SIZE`).
  * **`export(idx, out_dir)`**: Writes track data to `out_dir/<track_name>.ogg`.
  * **`export_all(out_dir, progress_callback=None)`**: Exports every track in sequence, reporting progress.
  * **`replace(idx, newfile)`**: Loads a new `.ogg` file into memory, replacing the bytes of track `idx`.
  * **`rebuild(progress_callback=None)`**: Concatenates all headers + data in order, XOR‑encrypts the entire buffer, and overwrites the original `stream` file.

* **`SFXArchive`** (handles SFX banks)

  * **`__init__(root, progress_callback=None)`**:

    1. Locates `root/audio/CONFIG/PakFiles.dat` → extracts package file names (each 52‑byte entry, null‑terminated).
    2. Locates `root/audio/CONFIG/BankLkup.dat` → unpacks entries (`<B3xII`: `pkg_idx`, `offset`, `size`) for every sound bank.
    3. Iterates through each package under `root/audio/SFX` matching a `PakFiles.dat` entry. For each, reads raw bytes, then for every bank entry pointing to that package:

       * Reads a 4804‑byte header (`BANK_HEADER_SIZE`), unpacks `<H` at offset 0 = number of sounds (`count`).
       * Iterates through each sound slot (12 bytes per entry): gets `buf_off`, `rate`, and computes the next offset to determine PCM length.
       * Extracts PCM bytes (`length`), wraps into a temporary WAV buffer (mono, 16‑bit at `rate` or fallback `22050` Hz), and records metadata (package, offsets, PCM, sample rate, name).
  * **`export(idx, out_dir)`**: Wraps `sounds[idx]['pcm']` as WAV and writes `out_dir/<sound_name>.wav`.
  * **`export_all(out_dir, progress_callback=None)`**: Exports every discovered sound.
  * **`replace(idx, newfile)`**: Reads a user‑supplied `.wav`, extracts its PCM frames, and replaces `sounds[idx]['pcm']`.
  * **`rebuild(progress_callback=None)`**: Groups sounds by their original bank file; for each bank file, reads raw bytes into a `bytearray`, then for every associated sound:

    * Computes where in the bank file to write the new PCM (`header_off + BANK_HEADER_SIZE + pcm_offset`) and overwrites with updated PCM bytes.
    * Saves the modified bank file back to disk.

* **`App` (Tkinter GUI)**

  * Uses a `ttk.Notebook` with two tabs (“Stream” and “SFX”). Each tab:

    * A row of buttons (`Load`, `Export`, `Expt All`, `Replace`, `Rebuild`, `Play`, `Stop`) wired to corresponding handler methods.
    * A scrollable `Listbox` showing track names (`stream`) or sound names (`SFX`).
  * **Progress Bar** at the bottom is updated via `self._update_progress(val, total)` (called from worker threads).
  * **Audio Playback**

    * **Stream**: Writes the selected track’s raw `.ogg` bytes to a temporary file, loading and playing it via `pygame.mixer.music`.
    * **SFX**: Wraps the selected PCM into a WAV buffer, creates a `pygame.mixer.Sound` object from memory, and plays it.

* **Multithreading**

  * All I/O‑heavy operations (`load`, `export_all`, `rebuild`) are decorated with `@run_in_thread`, which spins up a daemon thread so the GUI stays responsive. Progress callbacks marshal back to the main thread via `self.after(0, …)`.

---

## Installation & Setup

1. **Ensure Python 3.6+ is installed.**

2. **Install Pygame**

   ```bash
   pip install pygame
   ```

3. **Verify Tkinter** (should come with standard Python). If missing:

   * **Ubuntu/Debian**:

     ```bash
     sudo apt install python3-tk
     ```
   * **Fedora**:

     ```bash
     sudo dnf install python3-tkinter
     ```
   * **Windows/macOS**: Usually included by default.

4. **Run the Editor**

   ```bash
   python gta_audio_editor.py
   ```

No special environment variables or external tools are required. The script will prompt you for file/directory selection via standard OS file dialogs.

---

## How It Works

1. **Decrypting & Parsing `stream` Archives**

   * `stream` files in GTA SA are simply XOR‑encrypted with a fixed 16‑byte key (`EA 3A C4 A1 9A A8 14 F3 48 B0 D7 23 9D E8 FF F1`).
   * The first 8068 bytes after each track header represent metadata (unknown fields plus a 32‑bit “length” field somewhere in the first 8 positions).
   * After decrypting, the code scans sequentially:

     1. Read 8068‑byte header (`TRACK_HEADER_SIZE`).
     2. Unpack eight 4‑byte little‑endian integers from offsets `8000, 8008, …, 8056` until a non‑`0xCDCDCDCD` value is found → this is the data length.
     3. Slice out that many bytes immediately following the header → raw OGG data.
     4. Advance the offset pointer by `header + data length` and repeat.

2. **Extracting & Rebuilding SFX Banks**

   * The game stores SFX as PCM in large “bank” packages. There are two index files:

     * `PakFiles.dat`: A fixed‑length listing of package file names (each entry is 52 bytes; null-terminated).
     * `BankLkup.dat`: A series of records (`<B3xII>`) per bank:

       * `pkg_idx` (which entry in `PakFiles.dat`),
       * `offset` (where the 4804‑byte bank header begins inside the package),
       * `size` (how many bytes the entire bank occupies).
   * Inside each bank header:

     1. First 2 bytes (`<H>`) = count of sounds in that bank (`count`).
     2. Next `count` entries of 12 bytes each:

        * `<I I H H>` = `(pcm_offset, ?, sample_rate, ?)`.
     3. These offsets point to raw PCM chunks somewhere after the 4804‑byte header. The code calculates each chunk’s length by peeking at the next entry’s `pcm_offset` (or using `size` if it’s the last one).
   * To **export**, the script wraps each raw PCM chunk into a minimal WAV header (mono, 16-bit) at the correct sample rate, letting you save `.wav` files.
   * To **replace**, you supply a `.wav` file; its entire PCM frame data is read and stored in the in‑memory representation for that sound.
   * When you **rebuild**, the script re‑opens each original `.sfx` package as a `bytearray`, then seeks to `header_offset + 4804 + pcm_offset` for every modified sound and overwrites the bytes. The package is then saved back to disk.

---

## Tips & Notes

* **Progress Indicator**:

  * Large `stream` files or huge SFX directories may take several seconds to decrypt/parse. Watch the bottom progress bar. If it pauses, it’s usually waiting on I/O.

* **File Permissions**:

  * Make sure the `stream` or `.sfx` files are writable if you intend to use **Replace** → **Rebuild**. Otherwise, the tool will fail silently or pop up an error.

* **PCM Compatibility**:

  * Replaced SFX must be mono, 16-bit WAV. The sample rate can differ; the tool will use whatever rate is declared in the bank header (or fallback to 22050 Hz if the header’s rate is zero). If you have stereo or 24-bit PCM, convert it beforehand using any audio editor (Audacity, ffmpeg, etc.).

* **Temporary Files**:

  * When you **Play** a stream track, a temporary `.ogg` file is created in your system’s temp directory. The tool attempts to delete it once playback stops, but if it crashes, you may see leftover `.ogg` files. They are safe to delete.

* **Thread Safety**:

  * Loading, exporting, and rebuilding occur in background threads so that the UI remains responsive. Avoid rapidly clicking multiple buttons; wait for the progress bar to finish before starting another long operation.

---

With this editor, you can quickly browse, extract, and swap out your favorite tracks and sound effects in **GTA: San Andreas**. Enjoy customizing your in‑game soundtrack or swapping in custom sound packs!
