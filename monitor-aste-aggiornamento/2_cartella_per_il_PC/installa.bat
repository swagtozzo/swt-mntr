@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Monitor Aste - installazione lettore Gobid
echo ============================================
echo.

if not exist token.txt (
  echo Manca il file token.txt in questa cartella. Leggi ISTRUZIONI.txt, passo 1.
  pause
  exit /b 1
)

echo Installo la libreria "requests"...
python -m pip install --user --quiet requests
if errorlevel 1 (
  echo Python non trovato o pip non funzionante. Controlla che "python --version" funzioni.
  pause
  exit /b 1
)

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('where python') do if not defined PYW set "PYW=%%i"

echo Creo l'attivita' pianificata "Monitor Aste Gobid" (ogni ora)...
schtasks /create /f /sc hourly /mo 1 /tn "Monitor Aste Gobid" /tr "\"%PYW%\" \"%~dp0gobid_locale.py\""
if errorlevel 1 (
  echo Creazione attivita' non riuscita.
  pause
  exit /b 1
)

echo.
echo Avvio il primo giro adesso. La prima volta controlla circa 2.500 aste:
echo ci vogliono 40-60 minuti. Puoi lasciare aperta questa finestra e fare altro.
echo.
python gobid_locale.py
echo.
echo Finito. Da ora il programma gira da solo ogni ora quando il PC e' acceso.
echo Il registro e' nel file gobid_log.txt in questa cartella.
pause
