# FocusCoach

FocusCoach is a Windows desktop App designed to improve Focus and Workflow that records the foreground app, window title, and idle status in a local SQLite database, For ML to decide if its a distracting activity, its neutral or its not. This project is complex and is still in the works, but the Final project will be worth it!

## Setup

On Windows 10 version 1809 or later, or Windows 11, with 64-bit Python 3.12 installed, run these commands in PowerShell from the repository:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

To keep supported foreground apps marked active while Windows reports they are playing media, also install the optional media bindings:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-media.txt
```

## Run

```powershell
.\.venv\Scripts\python.exe focuscoach.py
```

Click the VS Code Run Code button again to request a graceful stop. When running directly from PowerShell, press Ctrl+C instead. FocusCoach saves its activity history in `focuscoach.db` and writes troubleshooting messages to `focuscoach.log` next to the script. The database may contain sensitive window titles; keep it private.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The Windows smoke tests check that a second process can request a graceful stop and import the optional media bindings if installed. For an interactive check, start FocusCoach, switch between apps for at least three seconds each, and confirm the changes appear in the terminal. With the media bindings installed, leave a supported browser playing in the foreground past the two minute idle threshold, then pause playback and confirm the window is marked idle after the threshold and settle period.
