@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo  Vantage - Build
echo ============================================================
echo.

:: Read version from version.py
for /f "tokens=3 delims= " %%v in ('findstr "__version__" version.py') do (
    set RAW_VER=%%v
)
:: Strip surrounding quotes
set VERSION=%RAW_VER:"=%
echo Version: %VERSION%
echo.

:: Regenerate icon (skip gracefully if script missing)
if exist create_icon.py (
    echo [1/4] Generating icon...
    python create_icon.py
) else (
    echo [1/4] Skipping icon generation (create_icon.py not found)
)
echo.

:: Clean previous build
echo [2/4] Cleaning previous build...
if exist build        rmdir /s /q build
if exist dist\Vantage rmdir /s /q dist\Vantage
echo.

:: Build with spec file
echo [3/4] Running PyInstaller...
python -m PyInstaller --noconfirm Vantage.spec
if errorlevel 1 (
    echo.
    echo FAILED - PyInstaller returned an error.
    pause
    exit /b 1
)
echo.

:: Zip the output folder
echo [4/4] Creating release archive...
set ZIP_NAME=Vantage-v%VERSION%-win64.zip
if exist dist\%ZIP_NAME% del dist\%ZIP_NAME%

powershell -NoProfile -Command ^
  "Compress-Archive -Path 'dist\Vantage\*' -DestinationPath 'dist\%ZIP_NAME%' -Force"

if exist dist\%ZIP_NAME% (
    echo.
    echo ============================================================
    echo  BUILD SUCCESSFUL
    echo.
    echo  Executable folder : dist\Vantage\
    echo  Release archive   : dist\%ZIP_NAME%
    echo.
    echo  INSTALL INSTRUCTIONS (send these with the zip):
    echo    1. Extract Vantage-v%VERSION%-win64.zip anywhere
    echo       e.g.  C:\Users\YOU\AppData\Local\Vantage\
    echo    2. Copy your .env file into the extracted folder
    echo    3. Run Vantage.exe
    echo    4. Right-click Vantage.exe ^> Send to ^> Desktop (shortcut)
    echo.
    echo  TO RELEASE:
    echo    git tag v%VERSION%
    echo    git push origin v%VERSION%
    echo    (GitHub Actions will build + publish automatically)
    echo ============================================================
) else (
    echo FAILED - zip was not created.
)
echo.
pause
