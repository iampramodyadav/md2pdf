@echo off
call 
REM Builds dist\MD2PDF.exe  (run from the folder containing md2pdf.py and md2pdf_gui.py)
pip install markdown pygments playwright tkinterdnd2 pyinstaller || goto :err

REM Bundle mermaid.js so the exe works offline
if not exist mermaid.min.js curl -L -o mermaid.min.js https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js

pyinstaller --noconfirm --onefile --windowed --name MD2PDF ^
  --add-data "mermaid.min.js;." ^
  --collect-all tkinterdnd2 ^
  --collect-all playwright ^
  --collect-submodules pygments ^
  --collect-submodules markdown ^
  md2pdf_gui.py || goto :err

echo.
echo Done: dist\MD2PDF.exe
goto :eof

:err
echo Build failed.
exit /b 1
