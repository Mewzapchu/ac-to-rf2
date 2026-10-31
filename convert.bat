@echo off
rem Drag an Assetto Corsa track folder onto this file to convert it to rFactor 2.
rem It installs into rFactor 2\ModDev\Locations. A copy of the track folder is
rem left in out\<track>\rf2\<track>: copy it into ModDev\Locations on any
rem machine with rF2.
rem
rem For a track with several layouts, drag the folder and then type the layout
rem when asked (for example: gp). Press Enter for the default, if it has one.
setlocal
set "HERE=%~dp0"
if "%~1"=="" (
  echo Drag an Assetto Corsa track folder onto convert.bat.
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
set "LAYOUT="
for %%F in ("%~1\models_*.ini") do (
  echo This track has layouts:
  for %%G in ("%~1\models_*.ini") do echo    %%~nG
  set /p LAYOUT="Layout (the part after models_, Enter for the default): "
  goto :run
)
:run
echo.
echo Converting "%~nx1"  -- this takes about 5 to 15 minutes, Blender does most of it.
echo.
if defined LAYOUT (
  python "%HERE%build_track.py" "%~1" --install auto --layout "%LAYOUT%"
) else (
  python "%HERE%build_track.py" "%~1" --install auto
)
if errorlevel 1 (
  echo.
  echo Conversion FAILED. Read the messages above.
  pause
  exit /b 1
)
set "TRACK=%~nx1"
if defined LAYOUT set "TRACK=%~nx1_%LAYOUT%"
echo.
echo Done. A copy of the track folder is in "%HERE%out\%TRACK%\rf2\%TRACK%".
pause
