@echo off
rem Runs the subtitler from source in a console (the GUI is Dilmac.exe or pc\gui.py).
chcp 65001 >nul
cd /d "%~dp0pc"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" subtitler.py %*
pause
