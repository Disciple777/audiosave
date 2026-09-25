@echo off
REM ---------------------------------------------------------------------------
REM  Builds a standalone AudioSave.exe into dist\ (no Python needed to run it).
REM  Needs Python with the app's dependencies; PyInstaller is installed for you.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY=python"
%PY% -c "import pyaudiowpatch, numpy, imageio_ffmpeg" >nul 2>&1 || set "PY=py -3"
%PY% -c "import pyaudiowpatch, numpy, imageio_ffmpeg" >nul 2>&1 || (
  echo Dependencies are missing. Run AudioSave.bat once first, then retry.
  pause & exit /b 1
)

%PY% -c "import PyInstaller" >nul 2>&1 || %PY% -m pip install pyinstaller || (
  echo Could not install PyInstaller. & pause & exit /b 1
)

REM The bundled ffmpeg binary has to be copied in where imageio_ffmpeg looks.
for /f "delims=" %%i in ('%PY% -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"') do set "FFMPEG=%%i"
echo Bundling ffmpeg: %FFMPEG%

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name AudioSave ^
  --add-binary "%FFMPEG%;imageio_ffmpeg/binaries" ^
  --hidden-import pyaudiowpatch ^
  app.py || (echo. & echo Build failed - see the output above. & pause & exit /b 1)

echo.
echo Done: "%~dp0dist\AudioSave.exe"
echo Recordings are saved next to the .exe, in dist\recordings\.
pause
