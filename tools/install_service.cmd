@echo off
rem ---------------------------------------------------------------------------
rem  Install ScanBridge for ONE FRAME as a Windows service using NSSM.
rem  Usage:  install_service.cmd <FRAME e.g. PM8-SP1> <full path to scanbridge.toml> <win_dcom_user password>
rem  Requires nssm.exe on PATH and an elevated prompt.
rem ---------------------------------------------------------------------------
setlocal
if "%~3"=="" (echo usage: %~nx0 FRAME CONFIG_PATH PASSWORD & exit /b 1)
set SVC=ScanBridge-%~1
set CFG=%~2
set APPDIR=%~dp0..
for %%I in ("%APPDIR%") do set APPDIR=%%~fI
for /f "delims=" %%P in ('py -3.14-32 -c "import sys;print(sys.executable)"') do set PY=%%P
if not exist "%PY%" (echo 32-bit Python 3.14 not found & exit /b 1)
for %%I in ("%CFG%") do set SITEDIR=%%~dpI
if not exist "%SITEDIR%logs" mkdir "%SITEDIR%logs"

"%PY%" -m scanbridge check -c "%CFG%" || (echo config check failed & exit /b 1)

nssm install %SVC% "%PY%" -m scanbridge run -c "%CFG%"
nssm set %SVC% AppDirectory "%APPDIR%"
nssm set %SVC% DisplayName "ScanBridge %~1 (AccuRay -> ControlLogix)"
nssm set %SVC% Description "AccuRay Smart Platform frame %~1 to ControlLogix data transport"
nssm set %SVC% ObjectName ".\win_dcom_user" "%~3"
nssm set %SVC% Start SERVICE_AUTO_START
nssm set %SVC% AppExit Default Restart
nssm set %SVC% AppRestartDelay 5000
nssm set %SVC% AppThrottle 10000
nssm set %SVC% AppStopMethodConsole 15000
nssm set %SVC% AppStdout "%SITEDIR%logs\service.out"
nssm set %SVC% AppStderr "%SITEDIR%logs\service.out"
nssm set %SVC% AppRotateFiles 1
nssm set %SVC% AppRotateBytes 5000000

rem register the event-log source once (needs admin)
"%PY%" -c "import win32evtlogutil,tomllib;c=tomllib.load(open(r'%CFG%','rb'));win32evtlogutil.AddSourceToRegistry('ScanBridge-'+c.get('site',{}).get('name','Scanner'))"

nssm start %SVC%
sc query %SVC%
endlocal
