@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Bot Futures - chi chay mo phong, khong giao dich tien that.
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 "%~dp0mo_bot.py"
goto finished
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing_python
python "%~dp0mo_bot.py"
goto finished
:missing_python
echo Can cai Python 3.12 tro len tu python.org, sau do mo lai tep nay.
:finished
echo.
echo Bam phim bat ky de dong cua so nay.
pause >nul
