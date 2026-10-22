@echo off
rem  Usage: uninstall_service.cmd <FRAME e.g. PM8-SP1>
if "%~1"=="" (echo usage: %~nx0 FRAME & exit /b 1)
nssm stop ScanBridge-%~1
nssm remove ScanBridge-%~1 confirm
