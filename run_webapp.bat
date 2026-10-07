@echo off
cd /d "%~dp0"

rem 若设置了 PORT 环境变量则使用，否则默认 5604
if "%PORT%"=="" (
    python -m uvicorn webapp.main:app --host 127.0.0.1 --port 5604 --reload
) else (
    python -m uvicorn webapp.main:app --host 127.0.0.1 --port %PORT% --reload
)