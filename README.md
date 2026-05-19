# BootMark

BootMark is a minimal Windows desktop assistant for Intel FPT-based BIOS region backup, validation, and flashing. It does **not** edit BIOS logos; place your edited BIOS-region file manually (for example via H2OEZE) into the session `modified` folder.

## Requirements

- Windows 10/11 (64-bit)
- Python 3.10+ (for development builds)
- Administrator privileges at runtime
- Intel Flash Programming Tool: `FPTW64.exe` and its companion DLLs / `fparts.txt`

## Runtime layout

Deploy beside `BootMark.exe`:

```
BootMark/
  BootMark.exe
  tools/
    fpt/
      WIN64/
        FPTW64.exe
        fparts.txt
        *.dll
  sessions/
    YYYY-MM-DD_HHMMSS_MANUFACTURER_MODEL/
      device_info/
        device_summary.txt   ← main human-readable summary (one file)
        summary.json
        computer_system.txt
        bios.txt
        baseboard.txt
        msinfo32.nfo
      backups/
      modified/
      hashes/
      logs/
```

`FPTW64.exe` is searched in order:

1. `tools\fpt\WIN64\FPTW64.exe`
2. `tools\FPTW64.exe`
3. Same folder as `BootMark.exe`

## Build

```bat
build.bat
```

`logo.ico` in the project root is embedded in `BootMark.exe` and used for the window and taskbar icon. Output: `dist\BootMark.exe`. Copy it into your runtime folder with the `tools` tree above.

## Usage (typical flow)

1. Run **BootMark.exe** as Administrator (use **Restart as Administrator** if prompted).
2. **Check Admin / Device Info** — verifies elevation and reads system identifiers.
3. **Create Session** — creates a timestamped session folder and saves device info.
4. **Backup BIOS Region** then **Backup Full SPI** — original backups are never overwritten.
5. **Hash Backups** — writes SHA256 hashes to `hashes\hashes.txt`.
6. **Test Rewrite Original BIOS Region** — confirms FPT can rewrite the saved backup.
7. Edit your BIOS region externally, then save as `modified\logo_modified.bin` (or use **Select Modified BIOS File**).
8. **Validate Modified File** — size and hash checks vs. the original BIOS-region backup.
9. **Flash Modified BIOS Region** — enabled only after backup, rewrite test, and validation all pass.
10. **Restart Now** after a successful flash, or **Restore Original BIOS Region** if needed.

All operations log to the GUI and `sessions\...\logs\bootmark_session.log`.

## Safety

- Flash stays disabled until backup, rewrite test, and modified-file validation succeed.
- Firmware commands run one at a time.
- Original backup files are never replaced.

## Disclaimer

Flashing BIOS/firmware can brick hardware. Use only on the intended machine, with valid backups, and at your own risk.
