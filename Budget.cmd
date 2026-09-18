@echo off
rem Double-click launcher. Keeps the virtual environment outside OneDrive so
rem thousands of package files are not synced; only code + data/ live here.
setlocal
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\TheBudgetPlan\venv"
set "STAMP=%VENV%\pyproject.installed"

if not exist "%VENV%\Scripts\python.exe" (
    echo First run: creating Python environment in %VENV% ...
    py -3 -m venv "%VENV%" || goto :fail
    "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :fail
)

rem Reinstall whenever pyproject.toml changes, so new dependencies arrive automatically.
fc /b "%~dp0pyproject.toml" "%STAMP%" >nul 2>&1
if errorlevel 1 (
    echo Installing Python packages ...
    "%VENV%\Scripts\python.exe" -m pip install --quiet -e "%~dp0." || goto :fail
    copy /y "%~dp0pyproject.toml" "%STAMP%" >nul
)

rem A file named DEV_MODE next to this launcher (git-ignored, so only on this machine)
rem starts dev mode: the passphrase-less data-dev vault. Delete it to run normally.
set "DEVFLAG="
if exist "%~dp0DEV_MODE" set "DEVFLAG=--dev"
"%VENV%\Scripts\python.exe" -m budgetapp %DEVFLAG% %*
exit /b %errorlevel%

:fail
echo Setup failed. Make sure Python 3.12+ is installed (py launcher on PATH).
pause
exit /b 1
