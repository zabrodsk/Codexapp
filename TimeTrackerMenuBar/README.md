# TimeTrackerMenuBar (macOS menu bar timer)

This is a simple macOS menu bar app (the area next to Control Center is called the **menu bar**).

## What it does
- Adds a timer icon in the macOS menu bar.
- Click the icon to open a small window/popover.
- Shows elapsed work time in `HH:MM:SS`.
- Has **Start/Stop** and **Reset** buttons.
- Changes icon state when tracking is active.

## Why this matches your request
You asked for a very simple app you can click from the top of your Mac and start/stop to track work time for payroll. This implementation is intentionally minimal and focused on that single workflow.

## Open in Xcode
1. On a Mac with Xcode 15+ installed, open `TimeTrackerMenuBar/Package.swift`.
2. Choose **My Mac** as the run target.
3. Press **Run**.
4. Look for a timer icon in the menu bar (top-right area of macOS).

## Notes for next step (if you want)
- Save sessions to a local file (CSV/JSON).
- Add total time per day/week.
- Export a timesheet for your employer.
