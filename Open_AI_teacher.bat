@echo off
setlocal EnableExtensions

rem ============================================================
rem AI English Teacher launcher for Windows + Conda
rem
rem You only need to edit the values in the USER SETTINGS block.
rem ============================================================

rem ===================== USER SETTINGS ========================
rem [REQUIRED] Replace this with your own Anaconda/Miniconda activate.bat path.
set "CONDA_ACTIVATE_BAT=C:\Users\lenovo\miniconda3\Scripts\activate.bat"

rem [REQUIRED] Replace this with your Conda environment name.
set "CONDA_ENV_NAME=ai_teacher"

rem [OPTIONAL] Project root. Keep this as-is if the script stays in the project root.
set "PROJECT_DIR=D:\AIEnglish_grammar_teacher"

rem [OPTIONAL] Frontend launch mode:
rem   url  = open a local static server address such as Live Server or python -m http.server
rem   file = open the local frontend file directly
set "FRONTEND_MODE=url"

rem [OPTIONAL] Used when FRONTEND_MODE=url
set "FRONTEND_URL=http://127.0.0.1:8000/frontend/index.html"

rem [OPTIONAL] Used when FRONTEND_MODE=file
set "FRONTEND_FILE=%PROJECT_DIR%\frontend\index.html"

rem [OPTIONAL] Backend host/port
set "BACKEND_HOST=127.0.0.1"
set "BACKEND_PORT=8000"

rem [OPTIONAL] Backend window mode:
rem   0 = run backend in the current window
rem   1 = open backend in a new cmd window
set "BACKEND_NEW_WINDOW=0"
rem =================== END USER SETTINGS ======================

echo.
echo [1/5] Validating configuration...

if not exist "%CONDA_ACTIVATE_BAT%" (
    echo ERROR: Conda activate.bat was not found.
    echo Please edit CONDA_ACTIVATE_BAT in this file.
    echo Current value: %CONDA_ACTIVATE_BAT%
    goto :fail
)

if not exist "%PROJECT_DIR%" (
    echo ERROR: Project directory was not found.
    echo Please edit PROJECT_DIR in this file.
    echo Current value: %PROJECT_DIR%
    goto :fail
)

if /I "%FRONTEND_MODE%"=="file" (
    if not exist "%FRONTEND_FILE%" (
        echo ERROR: Frontend file was not found.
        echo Please edit FRONTEND_FILE in this file.
        echo Current value: %FRONTEND_FILE%
        goto :fail
    )
)

echo [2/5] Switching to project directory...
pushd "%PROJECT_DIR%" || goto :fail

echo [3/5] Activating Conda environment: %CONDA_ENV_NAME%
call "%CONDA_ACTIVATE_BAT%" "%CONDA_ENV_NAME%"
if errorlevel 1 (
    echo ERROR: Failed to activate Conda environment "%CONDA_ENV_NAME%".
    echo Check that the environment name is correct.
    goto :fail_after_pushd
)

echo [4/5] Verifying active Python...
where python
python --version
if errorlevel 1 (
    echo ERROR: Python is not available after Conda activation.
    goto :fail_after_pushd
)

echo [5/5] Opening frontend...
if /I "%FRONTEND_MODE%"=="url" (
    start "" "%FRONTEND_URL%"
) else (
    start "" "%FRONTEND_FILE%"
)

echo Starting backend...
if "%BACKEND_NEW_WINDOW%"=="1" (
    start "AI Teacher Backend" cmd /k "call \"%CONDA_ACTIVATE_BAT%\" \"%CONDA_ENV_NAME%\" && cd /d \"%PROJECT_DIR%\" && python -m uvicorn main:app --host %BACKEND_HOST% --port %BACKEND_PORT% --reload"
    goto :success
)

python -m uvicorn main:app --host %BACKEND_HOST% --port %BACKEND_PORT% --reload
if errorlevel 1 (
    echo ERROR: Backend failed to start.
    goto :fail_after_pushd
)

:success
echo.
echo Launcher finished.
goto :cleanup

:fail_after_pushd
echo.
echo Launcher stopped with errors.
goto :cleanup

:fail
echo.
echo Launcher stopped before startup.
goto :pause_only

:cleanup
popd

:pause_only
endlocal
pause
