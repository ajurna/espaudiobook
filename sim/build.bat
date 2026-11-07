@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d C:\Users\ajurn\espaudiobook
mkdir byj 2>nul
cl /nologo /std:c11 /D_CRT_SECURE_NO_WARNINGS /DLV_CONF_INCLUDE_SIMPLE /DLV_KCONFIG_IGNORE /I sim\lvgl /I sim /I firmware /Fobyj\ sim\main.c firmware\ui.c @sim\objects.rsp /link user32.lib gdi32.lib shell32.lib shcore.lib dwmapi.lib user32.lib gdi32.lib shell32.lib shcore.lib dwmapi.lib /OUT:sim\sim.exe
