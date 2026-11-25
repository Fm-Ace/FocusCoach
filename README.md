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

Open `focuscoach.py` and click **Run Code**. Click **Run Code** again to request a graceful stop. The workspace config routes this file through `run_focuscoach.py`, a short-lived launcher, so Code Runner is ready for the next click while the tracker runs in the background.

To use the same toggle directly from PowerShell:

```powershell
.\.venv\Scripts\python.exe run_focuscoach.py
```

Run that command again to stop FocusCoach. To run the tracker attached to the current terminal instead, use `focuscoach.py` directly and press Ctrl+C to stop it.

FocusCoach saves its activity history in `focuscoach.db` and its activity and troubleshooting logs in `focuscoach.log` next to the script. The database may contain sensitive window titles; keep it private.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The Windows smoke tests check that a second process can use the launcher to request a graceful stop and import the optional media bindings if installed. For an interactive check, start FocusCoach, switch between apps for at least three seconds each, and confirm the changes appear in `focuscoach.log`. With the media bindings installed, leave a supported browser playing in the foreground past the two minute idle threshold, then pause playback and confirm the window is marked idle after the threshold and settle period.
