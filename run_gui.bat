@echo off
echo ============================================================
echo  Vantage - Dev Mode
echo ============================================================
echo.
python gui_app.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo *** Crashed with exit code %ERRORLEVEL% ***
)
echo.
pause
