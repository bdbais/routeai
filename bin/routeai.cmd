@echo off
rem routeai launcher for Windows: runs run.py with Python 3.11+ (py launcher first, then python).
rem Set ROUTEAI_PYTHON to force a specific interpreter. Each branch returns the interpreter's exit code.
setlocal
set "ROOT=%~dp0.."
if defined ROUTEAI_PYTHON goto forced
where py >nul 2>nul || goto python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul || goto python
py -3 "%ROOT%\run.py" %*
exit /b %ERRORLEVEL%

:forced
"%ROUTEAI_PYTHON%" "%ROOT%\run.py" %*
exit /b %ERRORLEVEL%

:python
where python >nul 2>nul || goto nopython
python "%ROOT%\run.py" %*
exit /b %ERRORLEVEL%

:nopython
echo routeai: no Python 3.11+ found (tried the py launcher and python). Install Python from python.org or set ROUTEAI_PYTHON. 1>&2
exit /b 1
