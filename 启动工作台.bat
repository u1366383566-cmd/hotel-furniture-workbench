@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   酒店家具智能套图工作台 (本地 Mock)
echo   启动后请用浏览器打开:
echo   http://127.0.0.1:18234
echo   关闭服务: 直接关闭本窗口或按 Ctrl+C
echo ============================================
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 18234
pause
