@echo off
chcp 65001 >nul
rem Обновление ISTORIK VIDEO FACTORY до последней версии (ветка main на GitHub) прямо в этой папке.
rem Ваши .env, data\, projects\, logs\ и browser_profile\ не трогаются — переносить ничего не нужно.
rem Положите этот файл в папку istorik_factory (рядом со старым START_ISTORIK.bat) и запустите двойным щелчком.
setlocal
cd /d "%~dp0"
set "ZIP=%TEMP%\istorik_update.zip"
set "TMPD=%TEMP%\istorik_update"
echo Скачиваю последнюю версию…
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "Invoke-WebRequest -UseBasicParsing 'https://github.com/ayansagyngali/arabic-diacritizer/archive/refs/heads/main.zip' -OutFile '%ZIP%';" ^
  "if (Test-Path '%TMPD%') { Remove-Item -Recurse -Force '%TMPD%' };" ^
  "Expand-Archive -Force '%ZIP%' '%TMPD%';" ^
  "$src = Get-ChildItem '%TMPD%' | Select-Object -First 1 | ForEach-Object { Join-Path $_.FullName 'istorik_factory' };" ^
  "$keep = @('.env','data','projects','logs','browser_profile','.venv','START_ISTORIK.bat');" ^
  "Get-ChildItem $src -Force | Where-Object { $keep -notcontains $_.Name } | ForEach-Object { Copy-Item $_.FullName -Destination '.' -Recurse -Force };" ^
  "Remove-Item -Recurse -Force '%TMPD%'; Remove-Item -Force '%ZIP%'"
if errorlevel 1 (
  echo.
  echo Не удалось обновить: проверьте интернет и запустите ещё раз.
  pause
  exit /b 1
)
echo Готово: программа обновлена, ваши ключи и проекты на месте. Запускаю…
if exist START_ISTORIK.bat (call START_ISTORIK.bat) else (python run.py)
