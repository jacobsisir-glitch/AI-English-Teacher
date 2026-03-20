@echo off
chcp 65001 >nul
setlocal
set "PROJECT_DIR=%~dp0"

echo 正在启动 AI 语法老师...
pushd "%PROJECT_DIR%"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo 未检测到 .venv\Scripts\activate.bat，将直接使用当前 Python 环境。
)

echo 正在打开前端并启动后端服务...
start "" "%PROJECT_DIR%frontend\index.html"
uvicorn main:app --reload

popd
endlocal
pause
