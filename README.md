## GTA SA Audio Editor (BETA)

> **Note**: This is a BETA version. The SFX functionality is a little buggy at the moment but will be fixed in the near future.

A simple, cross-platform GUI tool for extracting, replacing, and rebuilding audio tracks (`stream`) and sound effects (`.dat` banks) in *Grand Theft Auto: San Andreas*.

---

### 📝 About

* **Stream Archive**

  * Decodes `stream` files by XOR’ing with the built-in key.
  * Parses out each track header and raw OGG data.
  * Allows exporting individual tracks or all at once, replacing tracks, and rebuilding the file in place.

* **SFX Archive**

  * Loads the game’s `audio/CONFIG/PakFiles.dat` and `BankLkup.dat` to locate soundbanks.
  * Parses each bank header to extract raw PCM samples.
  * Wraps samples as WAV, supports exporting, replacing, and rebuilding banks back into the original `.dat` files.

* **GUI**

  * Built with `tkinter` + `ttk.Notebook` for two tabs: **Stream** and **SFX**.
  * Play/stop buttons via `pygame.mixer` for quick previews.
  * Progress bar feedback during heavy operations.

---

### ⚙️ How It Works

1. **Load**

   * **Stream**: Select a `stream` file — it’s XOR-decoded, headers parsed, and track list populated in the Treeview.
   * **SFX**: Point to your GTA SA root folder — it finds `audio/CONFIG`, parses pak and bank lookup data, then lists all sounds.

2. **Export / Export All**

   * Choose a single entry or batch-export to a folder of your choice.
   * Stream exports as `.ogg`; SFX exports as `.wav`.

3. **Replace**

   * Select a track or sound, pick a new `.ogg` (for streams) or `.wav` (for SFX), and it updates in memory.

4. **Rebuild**

   * Writes all in-memory changes back to the original file(s), re-encoding streams and overwriting SFX banks.

5. **Playback**

   * Double-click selected item or hit **Play** to preview; **Stop** to end playback.

---

### 📦 Prerequisites

* **Python 3.7+**
* Install dependencies via Command Prompt / Terminal:

  ```bash
  pip install pygame
  ```

*(`tkinter` and `wave` are included in the Python standard library.)*

---

### 🚀 Usage

1. **Download** the `gta_audio_editor.py` file.
2. **Double-click** `gta_audio_editor.py` (or run `python gta_audio_editor.py`).
3. The GUI window will launch—use the **Stream** and **SFX** tabs to begin editing.

---
Preview

![Screenshot 2025-05-10 105601](https://github.com/user-attachments/assets/f4520fb3-d300-41ee-8a93-40e75659ec14)
![Screenshot 2025-05-10 105612](https://github.com/user-attachments/assets/17ec4be7-b76f-4dcc-8877-207356c6fe6f)
