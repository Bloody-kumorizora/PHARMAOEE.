@echo off
chcp 65001 >nul
rem Запуск PharmaOEE в Windows. Нужен Python 3.10+ с отметкой Add Python to PATH.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py pharmaoee.py %*
) else (
  python pharmaoee.py %*
)
if errorlevel 1 (
  echo.
  echo Не удалось запустить PharmaOEE. Проверьте установку Python 3.10 или новее.
  echo При установке Python отметьте Add Python to PATH.
  echo Журнал ошибок: %LOCALAPPDATA%\PharmaOEE\logs\pharmaoee.log
  pause
)
