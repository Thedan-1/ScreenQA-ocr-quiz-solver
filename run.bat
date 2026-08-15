@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 也可以不填这里，直接在 config.json 的 api.api_key 写死
rem set DEEPSEEK_API_KEY=sk-...
python main.py
pause
