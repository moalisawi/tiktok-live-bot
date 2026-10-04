@echo off
rem Starts the TikTok live recorder. Keep this window open (or run it at logon).
cd /d "%~dp0"
:loop
python recorder.py
echo recorder stopped, restarting in 10s... (close this window to quit)
timeout /t 10 >nul
goto loop
