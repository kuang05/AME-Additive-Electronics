@echo off
REM 启动图形界面；可传数据目录，例如: run_app.bat D:\ame-data
cd /d "%~dp0"
python -m ame %1
