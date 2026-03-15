@echo off
chcp 65001 >nul
echo 正在唤醒 AI 语法老师专属大脑...
call C:\Users\lenovo\miniconda3\Scripts\activate.bat ai_teacher
cd /d D:\AIEnglish_grammar_teacher
echo 环境就绪！正在打开浏览器并点火...

:: 自动在默认浏览器中打开微课网页
start "" "D:\AIEnglish_grammar_teacher\frontend\index.html"

:: 启动后端服务器
uvicorn main:app --reload
pause