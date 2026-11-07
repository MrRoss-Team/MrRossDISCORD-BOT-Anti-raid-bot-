@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Tworze srodowisko Pythona...
    python -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Utworzono plik .env - wklej w nim token bota i uruchom ponownie.
    notepad ".env"
    goto :end
)
".venv\Scripts\python.exe" bot.py
:error
pause
:end
