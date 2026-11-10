@echo off
rem Windows launcher: storyboard.cmd [command ...]   (no arguments = open the boards in a browser)
rem Set STORYBOARD_ROOT / STORYBOARD_BOARDS / COMFY_URL first, or edit the lines below.
setlocal
if "%STORYBOARD_ROOT%"=="" set STORYBOARD_ROOT=%~dp0
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%~dp0;%PYTHONPATH%
if "%~1"=="" (
  py -3 -m storyboard serve
) else (
  py -3 -m storyboard %*
)
