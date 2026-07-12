@echo off
setlocal EnableExtensions

rem ============================================================
rem AI English Teacher one-click launcher
rem ============================================================

rem ===================== USER SETTINGS ========================
set "CONDA_ACTIVATE_BAT=C:\Users\lenovo\miniconda3\Scripts\activate.bat"
set "CONDA_ENV_NAME=ai_teacher"
set "PROJECT_DIR=D:\AIEnglish_grammar_teacher"

set "BACKEND_HOST=127.0.0.1"
set "BACKEND_PORT=8000"
set "FRONTEND_URL=http://127.0.0.1:8000/frontend/index.html"

set "LIVEKIT_ENABLED=1"
set "LIVEKIT_HOST=127.0.0.1"
set "LIVEKIT_PORT=7880"
set "LIVEKIT_API_KEY=devkey"
set "LIVEKIT_API_SECRET=secret"
set "LIVEKIT_EXE=D:\AI_English_teacher_tools\livekit\livekit_1.10.1_windows_amd64\livekit-server.exe"

set "FUNASR_ENABLED=1"
set "FUNASR_HOST=127.0.0.1"
set "FUNASR_PORT=10095"
set "FUNASR_WORKDIR=D:\AI_English_teacher_tools\FunASR\runtime\python\websocket"
set "FUNASR_SCRIPT=%FUNASR_WORKDIR%\funasr_wss_server.py"
set "FUNASR_SSL_CERT=D:\AI_English_teacher_tools\FunASR\runtime\ssl_key\server.crt"
set "FUNASR_SSL_KEY=D:\AI_English_teacher_tools\FunASR\runtime\ssl_key\server.key"
set "FUNASR_ASR_MODEL=iic/speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404"
set "FUNASR_ASR_MODEL_ONLINE=iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
set "FUNASR_VAD_MODEL=iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
set "FUNASR_PUNC_MODEL=iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727"

set "SERVICE_WAIT_SECONDS=45"
rem =================== END USER SETTINGS ======================

set "LIVEKIT_LAUNCHER=%TEMP%\ai_teacher_livekit.cmd"
set "FUNASR_LAUNCHER=%TEMP%\ai_teacher_funasr.cmd"
set "BACKEND_LAUNCHER=%TEMP%\ai_teacher_backend.cmd"

echo.
echo [1/7] Validating launcher settings...

if not exist "%CONDA_ACTIVATE_BAT%" (
    echo ERROR: Conda activate.bat was not found.
    echo Current value: %CONDA_ACTIVATE_BAT%
    goto :fail
)

if not exist "%PROJECT_DIR%" (
    echo ERROR: Project directory was not found.
    echo Current value: %PROJECT_DIR%
    goto :fail
)

if "%LIVEKIT_ENABLED%"=="1" (
    if not exist "%LIVEKIT_EXE%" (
        echo ERROR: LiveKit executable was not found.
        echo Current value: %LIVEKIT_EXE%
        goto :fail
    )
)

if "%FUNASR_ENABLED%"=="1" (
    if not exist "%FUNASR_SCRIPT%" (
        echo ERROR: FunASR websocket server script was not found.
        echo Current value: %FUNASR_SCRIPT%
        goto :fail
    )
    if not exist "%FUNASR_SSL_CERT%" (
        echo ERROR: FunASR SSL cert was not found.
        echo Current value: %FUNASR_SSL_CERT%
        goto :fail
    )
    if not exist "%FUNASR_SSL_KEY%" (
        echo ERROR: FunASR SSL key was not found.
        echo Current value: %FUNASR_SSL_KEY%
        goto :fail
    )
)

echo [2/7] Switching to project directory...
pushd "%PROJECT_DIR%" || goto :fail

echo [3/7] Activating Conda environment: %CONDA_ENV_NAME%
call "%CONDA_ACTIVATE_BAT%" "%CONDA_ENV_NAME%"
if errorlevel 1 (
    echo ERROR: Failed to activate Conda environment "%CONDA_ENV_NAME%".
    goto :fail_after_pushd
)

echo [4/7] Verifying Python...
where python
python --version
if errorlevel 1 (
    echo ERROR: Python is not available after Conda activation.
    goto :fail_after_pushd
)

echo [5/7] Ensuring LiveKit is running...
if "%LIVEKIT_ENABLED%"=="1" (
    netstat -ano | findstr /R /C:":%LIVEKIT_PORT% .*LISTENING" >nul 2>nul
    if errorlevel 1 (
        > "%LIVEKIT_LAUNCHER%" echo @echo off
        >> "%LIVEKIT_LAUNCHER%" echo "%LIVEKIT_EXE%" --dev --bind %LIVEKIT_HOST% --keys "%LIVEKIT_API_KEY%: %LIVEKIT_API_SECRET%"
        echo Starting LiveKit...
        start "AI Teacher LiveKit" "%ComSpec%" /k "%LIVEKIT_LAUNCHER%"
        call :wait_for_port "%LIVEKIT_PORT%" "%SERVICE_WAIT_SECONDS%" "LiveKit"
        if errorlevel 1 goto :fail_after_pushd
    ) else (
        echo LiveKit is already listening on port %LIVEKIT_PORT%.
    )
) else (
    echo LiveKit auto-start is disabled. Skipping.
)

echo [6/7] Ensuring FunASR websocket server is running...
if "%FUNASR_ENABLED%"=="1" (
    netstat -ano | findstr /R /C:":%FUNASR_PORT% .*LISTENING" >nul 2>nul
    if errorlevel 1 (
        > "%FUNASR_LAUNCHER%" echo @echo off
        >> "%FUNASR_LAUNCHER%" echo call "%CONDA_ACTIVATE_BAT%" "%CONDA_ENV_NAME%"
        >> "%FUNASR_LAUNCHER%" echo cd /d "%FUNASR_WORKDIR%"
        >> "%FUNASR_LAUNCHER%" echo python "%FUNASR_SCRIPT%" --host %FUNASR_HOST% --port %FUNASR_PORT% --certfile "%FUNASR_SSL_CERT%" --keyfile "%FUNASR_SSL_KEY%" --asr_model "%FUNASR_ASR_MODEL%" --asr_model_online "%FUNASR_ASR_MODEL_ONLINE%" --vad_model "%FUNASR_VAD_MODEL%" --punc_model "%FUNASR_PUNC_MODEL%"
        echo Starting FunASR...
        start "AI Teacher FunASR" "%ComSpec%" /k "%FUNASR_LAUNCHER%"
        call :wait_for_port "%FUNASR_PORT%" "%SERVICE_WAIT_SECONDS%" "FunASR"
        if errorlevel 1 goto :fail_after_pushd
    ) else (
        echo FunASR is already listening on port %FUNASR_PORT%.
    )
) else (
    echo FunASR auto-start is disabled. Skipping.
)

echo [7/7] Ensuring backend is running...
netstat -ano | findstr /R /C:":%BACKEND_PORT% .*LISTENING" >nul 2>nul
if errorlevel 1 (
    > "%BACKEND_LAUNCHER%" echo @echo off
    >> "%BACKEND_LAUNCHER%" echo call "%CONDA_ACTIVATE_BAT%" "%CONDA_ENV_NAME%"
    >> "%BACKEND_LAUNCHER%" echo cd /d "%PROJECT_DIR%"
    >> "%BACKEND_LAUNCHER%" echo python -m uvicorn main:app --host %BACKEND_HOST% --port %BACKEND_PORT% --reload
    echo Starting Backend...
    start "AI Teacher Backend" "%ComSpec%" /k "%BACKEND_LAUNCHER%"
    call :wait_for_port "%BACKEND_PORT%" "%SERVICE_WAIT_SECONDS%" "Backend"
    if errorlevel 1 goto :fail_after_pushd
) else (
    echo Backend is already listening on port %BACKEND_PORT%.
)

echo Opening AI Teacher web UI...
start "" "%FRONTEND_URL%"

echo.
echo All services are ready. You can start using AI Teacher now.
goto :cleanup_no_pause

:wait_for_port
set "WAIT_PORT=%~1"
set "WAIT_SECONDS=%~2"
set "WAIT_SERVICE=%~3"
for /l %%S in (%WAIT_SECONDS%,-1,0) do (
    netstat -ano | findstr /R /C:":%WAIT_PORT% .*LISTENING" >nul 2>nul
    if not errorlevel 1 exit /b 0
    if %%S EQU 0 (
        echo ERROR: %WAIT_SERVICE% did not open port %WAIT_PORT% within %WAIT_SECONDS% seconds.
        echo Please check the "%WAIT_SERVICE%" window for error details.
        exit /b 1
    )
    echo Waiting for %WAIT_SERVICE%... %%Ss remaining
    timeout /t 1 /nobreak >nul
)
exit /b 1

:fail_after_pushd
echo.
echo Launcher stopped with errors.
goto :cleanup_with_pause

:fail
echo.
echo Launcher stopped before startup.
goto :pause_only

:cleanup_with_pause
popd
goto :pause_only

:cleanup_no_pause
popd
goto :end

:pause_only
pause

:end
endlocal
