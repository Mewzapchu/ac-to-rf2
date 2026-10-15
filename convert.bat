@echo off
rem Drag an Assetto Corsa track folder onto this file to convert it to rFactor 2.
rem It installs into rFactor 2\ModDev\Locations. A copy of the track folder is
rem left in out\<track>\rf2\<track>: copy it into ModDev\Locations on any
rem machine with rF2.
rem
rem For a track with several layouts, drag the folder and then type the layout
rem when asked (for example: touristenfahrten). Press Enter for the default.
setlocal
set "HERE=%~dp0"
set "RF2=C:\Program Files (x86)\Steam\steamapps\common\rFactor 2\ModDev\Locations"
if "%~1"=="" (
  echo Drag an Assetto Corsa track folder onto convert.bat.
  pause
  exit /b 1
)
set "LAYOUT="
for %%F in ("%~1\models_*.ini") do (
  echo This track has layouts:
  for %%G in ("%~1\models_*.ini") do echo    %%~nG
  set /p LAYOUT="Layout (the part after models_, Enter for default): "
  goto :run
)
:run
echo.
echo Converting "%~nx1"  -- this takes 10 minutes to over an hour, Blender does most of it.
echo.
if defined LAYOUT (
  python "%HERE%build_track.py" "%~1" --install "%RF2%" --layout "%LAYOUT%"
) else (
  python "%HERE%build_track.py" "%~1" --install "%RF2%"
)
echo.
echo Done. Installed into "%RF2%"
echo A copy of the track folder is in "%HERE%out\<track>\rf2\<track>".
pause
