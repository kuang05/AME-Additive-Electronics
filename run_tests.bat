@echo off
REM 一键运行全部验收测试（离线，不访问网络）
cd /d "%~dp0"
python -m pytest tests -q
