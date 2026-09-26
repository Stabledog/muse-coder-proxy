@echo off
rem This file belongs in the Windows Startup folder:
rem   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\muse-coder-proxy.cmd
rem (it lives here in the repo too so it survives a rebuild - copy it back
rem into the Startup folder above; the Startup folder itself is not backed
rem up by git).
rem
rem What it does: at Windows login, launches muse-coder-proxy in the
rem background. See scripts\start-proxy.ps1 in this repo for what that
rem script does and why the proxy needs to be running at all - short
rem version: it's the only way an external agent (Bert) can reach Coder
rem workspaces on this machine, so it needs to come up whenever this
rem machine does.
rem
rem This stub only exists because Windows won't run a .ps1 directly from
rem the Startup folder (no file association, blocked by execution policy).
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\lesma\workarea\muse-coder-proxy\scripts\start-proxy.ps1"
