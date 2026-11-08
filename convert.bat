@echo off
rem Drag an Assetto Corsa track folder, or the zip it was downloaded as, onto
rem this file to convert it to rFactor 2. It installs into
rem rFactor 2\ModDev\Locations. A copy of the track folder is left in
rem out\<track>\rf2\<track>: copy it into ModDev\Locations on any machine
rem with rF2. A zip is extracted into out\_zips, not into Assetto Corsa.
rem
rem For a track with several layouts, type the layout when asked (for
rem example: gp). Press Enter for the default, if it has one.
setlocal
set "HERE=%~dp0"
if "%~1"=="" (
  echo Drag an Assetto Corsa track folder or zip onto convert.bat.
  pause
  exit /b 1
)
rem Windows can put a "python" on PATH that only opens the Microsoft Store, so
rem run it rather than just looking for it.
python -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10 or newer from python.org and
  echo tick "Add python.exe to PATH" in the installer, then try again.
  pause
  exit /b 1
)
set "TRACKDIR=%~1"
if /i not "%~x1"==".zip" goto :folder
rem track_zip prints the extracted folder; its messages go to the screen.
set "TRACKDIR="
for /f "usebackq delims=" %%L in (`python "%HERE%track_zip.py" "%~1"`) do set "TRACKDIR=%%L"
if not defined TRACKDIR (
  echo.
  echo Conversion FAILED. Read the messages above.
  pause
  exit /b 1
)
:folder
for %%T in ("%TRACKDIR%") do set "TRACK=%%~nxT"
set "LAYOUT="
for %%F in ("%TRACKDIR%\models_*.ini") do (
  echo This track has layouts:
  for %%G in ("%TRACKDIR%\models_*.ini") do echo    %%~nG
  set /p LAYOUT="Layout (the part after models_, Enter for the default): "
  goto :run
)
:run
echo.
echo Converting "%TRACK%"  -- this takes about 5 to 15 minutes, Blender does most of it.
echo.
if defined LAYOUT (
  python "%HERE%build_track.py" "%TRACKDIR%" --install auto --layout "%LAYOUT%"
) else (
  python "%HERE%build_track.py" "%TRACKDIR%" --install auto
)
if errorlevel 1 (
  echo.
  echo Conversion FAILED. Read the messages above.
  pause
  exit /b 1
)
if defined LAYOUT set "TRACK=%TRACK%_%LAYOUT%"
echo.
echo Done. A copy of the track folder is in "%HERE%out\%TRACK%\rf2\%TRACK%".
pause
