@echo off
rem Keep this entire file ASCII-only: cmd.exe may misparse UTF-8 batch text.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto :folder_error
chcp 65001 >nul
set "KOTOBA_LAUNCHER=%~dp0beginner_launcher.py"
if not exist "%KOTOBA_LAUNCHER%" goto :package_error
set "KOTOBA_PYTHON="
set "KOTOBA_SELECTOR="
set "KOTOBA_LAST_MANAGER="
rem Never let Python Launcher/Install Manager install a missing runtime.
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
echo Kotoba no Mado: checking Python and Google Chrome...
rem Prefer explicit, ordinarily installed versions. No assistant-bundled path.
for %%V in (314 313 312) do (
  call :try_python "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
  call :try_python "%ProgramFiles%\Python%%V\python.exe"
  call :try_python "%SystemDrive%\Python%%V\python.exe"
)
rem Read installed runtime paths from the official manager (custom install_dir too).
rem Only pymanager list is allowed through WindowsApps; never python/python3 aliases.
call :try_manager "%LOCALAPPDATA%\Microsoft\WindowsApps\pymanager.exe"
for /f "delims=" %%P in ('where.exe pymanager.exe 2^>nul') do call :try_manager "%%P"
rem py.exe must be a real launcher, not an unexamined WindowsApps alias.
call :try_py "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
call :try_py "%SystemRoot%\py.exe"
for /f "delims=" %%P in ('where.exe py.exe 2^>nul') do call :try_py "%%P"
rem A custom PATH install is allowed after excluding Store aliases.
for /f "delims=" %%P in ('where.exe python.exe 2^>nul') do call :try_python "%%P"
if not defined KOTOBA_PYTHON goto :missing_python
"%KOTOBA_PYTHON%" %KOTOBA_SELECTOR% -I -B -X utf8 "%KOTOBA_LAUNCHER%"
set "KOTOBA_RESULT=%errorlevel%"
goto :finished

:try_python
if defined KOTOBA_PYTHON exit /b
if not exist "%~1" exit /b
set "KOTOBA_CANDIDATE=%~1"
if not "%KOTOBA_CANDIDATE:WindowsApps=%"=="%KOTOBA_CANDIDATE%" exit /b
"%~1" -I -B -X utf8 "%KOTOBA_LAUNCHER%" --check-python 2>nul
if errorlevel 1 exit /b
set "KOTOBA_PYTHON=%~1"
set "KOTOBA_SELECTOR="
exit /b

:try_manager
if defined KOTOBA_PYTHON exit /b
if not exist "%~1" exit /b
if /i "%KOTOBA_LAST_MANAGER%"=="%~1" exit /b
set "KOTOBA_LAST_MANAGER=%~1"
for %%V in (3.14-64 3.13-64 3.12-64) do call :try_manager_version "%~1" "%%V"
exit /b

:try_manager_version
if defined KOTOBA_PYTHON exit /b
rem list does not launch Python, install runtimes, or run initial configuration.
for /f "delims=" %%P in ('""%~1" list --format=exe --one %~2 2^>nul"') do call :try_python "%%P"
exit /b

:try_py
if defined KOTOBA_PYTHON exit /b
if not exist "%~1" exit /b
set "KOTOBA_CANDIDATE=%~1"
if not "%KOTOBA_CANDIDATE:WindowsApps=%"=="%KOTOBA_CANDIDATE%" exit /b
for %%V in (3.14 3.13 3.12) do call :try_py_version "%~1" "-V:%%V"
exit /b

:try_py_version
if defined KOTOBA_PYTHON exit /b
"%~1" %~2 -I -B -X utf8 "%KOTOBA_LAUNCHER%" --check-python 2>nul
if errorlevel 1 exit /b
set "KOTOBA_PYTHON=%~1"
set "KOTOBA_SELECTOR=%~2"
exit /b

:missing_python
echo.
echo A compatible Python installation was not found.
echo Use standard CPython 3.14 64-bit, or an existing 3.12 / 3.13.
echo Python 3.15 and free-threaded builds are not supported by this preview.
echo Windows Store python/python3 aliases are not launched.
echo Official download: https://www.python.org/downloads/windows/
echo Japanese instructions: open START-HERE-ja.html in this folder.
echo No software was installed and no system settings were changed.
set "KOTOBA_RESULT=2"
goto :finished

:package_error
echo App files are missing. Right-click the ZIP and choose Extract All.
echo Keep this launcher and all extracted files together in one folder.
echo Japanese instructions: open START-HERE-ja.html.
set "KOTOBA_RESULT=5"
goto :finished

:folder_error
echo Cannot open the app folder. Extract the entire ZIP, then try again.
echo Japanese instructions: open START-HERE-ja.html.
set "KOTOBA_RESULT=1"

:finished
echo.
pause
exit /b %KOTOBA_RESULT%
