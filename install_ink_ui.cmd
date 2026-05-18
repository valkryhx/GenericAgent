@echo off
setlocal

cd /d "%~dp0"

where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] npm was not found. Please install Node.js first.
    exit /b 1
)

if not exist "frontends\ink-ui\package.json" (
    echo [ERROR] frontends\ink-ui\package.json was not found.
    echo Run this script from the GenericAgent repository root.
    exit /b 1
)

echo Installing Ink UI npm dependencies...
pushd "frontends\ink-ui"
npm install
set "NPM_EXIT=%ERRORLEVEL%"
popd

if not "%NPM_EXIT%"=="0" (
    echo [ERROR] npm install failed.
    exit /b %NPM_EXIT%
)

echo Ink UI dependencies installed.
exit /b 0
