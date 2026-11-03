@echo off
rem Data Explorer - creates the virtual environment if missing, installs, starts the server, opens the browser.
rem   run.bat            start with your saved workspaces
rem   run.bat --demo     start with the demo workspace (sample telecom data)
rem   run.bat --port 8010 / --no-browser
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment in .venv ...
    where py >nul 2>nul
    if errorlevel 1 (python -m venv .venv) else (py -3 -m venv .venv)
    if errorlevel 1 goto :no_python
)

rem Install only when requirements.txt changed since the last successful install.
rem These are the packages the app cannot start without.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
    echo Installing packages ^(first run, or requirements.txt changed^) ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 goto :install_failed
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

rem The database drivers are installed one by one. A driver that cannot be installed is only a
rem warning: the app still starts and shows that engine as "driver not installed".
fc /b requirements-drivers.txt ".venv\requirements-drivers.installed" >nul 2>nul
if errorlevel 1 call :install_drivers

".venv\Scripts\python.exe" -m app %*
goto :eof

:install_drivers
echo Installing the database drivers ...
for /f "usebackq eol=# tokens=1" %%D in ("requirements-drivers.txt") do call :install_driver "%%D"
copy /y requirements-drivers.txt ".venv\requirements-drivers.installed" >nul
exit /b 0

:install_driver
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q %1
if not errorlevel 1 exit /b 0
echo.
echo   WARNING: the database driver %1 could not be installed.
echo   The app starts anyway; that engine is shown as "driver not installed".
echo   To try again later:  .venv\Scripts\python -m pip install %1
echo.
exit /b 0

:no_python
echo.
echo Could not create the virtual environment. Install Python 3.11 or newer from python.org
echo ^(tick "Add python.exe to PATH"^), then run this file again.
pause
exit /b 1

:install_failed
echo.
echo Installing the packages failed. Check the messages above ^(internet or proxy access is needed
echo for the first install; see README.md for installing without internet^).
pause
exit /b 1
