@echo off
rem 风见（Windcatcher）本机服务启动器
rem 双击运行，浏览器打开 http://127.0.0.1:8765
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   风见 · 鼻腔气流工作台
echo   http://127.0.0.1:8765
echo   关闭本窗口即停止服务
echo ============================================
start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" -m uvicorn server:app --host 127.0.0.1 --port 8765
pause
