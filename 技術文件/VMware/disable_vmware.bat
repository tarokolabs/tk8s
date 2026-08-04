@echo off
title VMWare Tools
:ADMIN
openfiles >nul 2>nul ||(
  echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
  echo UAC.ShellExecute "%~s0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
  "%temp%\getadmin.vbs" >nul 2>&1
  goto:eof
)

del /f /q "%temp%\getadmin.vbs" >nul 2>nul
pushd "%~dp0"

bcdedit /set hypervisorlaunchtype auto
dism /Online /Enable-Feature /FeatureName:Microsoft-Hyper-V-All
dism /Online /Enable-Feature /FeatureName:HypervisorPlatform /FeatureName:VirtualMachinePlatform

echo "Reboot your computer."

pause

@rem dism /Online /Get-Features /Format:table