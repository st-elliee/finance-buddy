@echo off
REM Starts the Finance Buddy bot. Put a shortcut to this file in the
REM Windows Startup folder so it starts automatically when you log in.

REM Go to the folder this file is in (wherever that is).
cd /d "%~dp0"
title Finance Buddy bot

REM Use the venv's Python directly, so no "activate" is needed.
"%~dp0venv\Scripts\python.exe" bot.py

REM If the bot stops with an error, keep the window open so you can read it.
echo.
echo The bot has stopped. Press any key to close this window.
pause > nul
