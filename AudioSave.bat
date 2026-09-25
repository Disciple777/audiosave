@echo off
REM ---------------------------------------------------------------------------
REM  AudioSave launcher - double-click this file to run the app.
REM  Finds a Python that has the dependencies, installs them if needed, and
REM  starts the app with no console window.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
set "DEPS=import pyaudiowpatch, numpy, imageio_ffmpeg"

REM Prefer whatever "python" is on PATH, then the py launcher.
set "PY=python"
set "PYW=pythonw"
%PY% -c "%DEPS%" >nul 2>&1 && goto :launch

set "PY=py -3"
set "PYW=pyw -3"
%PY% -c "%DEPS%" >nul 2>&1 && goto :launch

REM No interpreter has everything - install into the first one that exists.
set "PY=python"
set "PYW=pythonw"
%PY% --version >nul 2>&1 || (set "PY=py -3" & set "PYW=pyw -3")
%PY% --version >nul 2>&1 || goto :nopython

echo Installing AudioSave's dependencies (first run only)...
echo.
%PY% -m pip install -r requirements.txt || goto :pipfailed
%PY% -c "%DEPS%" >nul 2>&1 || goto :pipfailed
echo.
echo Done. Starting AudioSave...

:launch
start "AudioSave" %PYW% "%~dp0app.py"
exit /b 0

:nopython
echo.
echo Python was not found. Install Python 3.9 or newer from python.org
echo (tick "Add python.exe to PATH" in the installer), then run this file again.
echo.
pause
exit /b 1

:pipfailed
echo.
echo Could not install the dependencies. The output above says why.
echo.
pause
exit /b 1
