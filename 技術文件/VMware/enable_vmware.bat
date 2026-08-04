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


echo "Disable windows defender ram protection"
pause

bcdedit /set hypervisorlaunchtype off
dism /Online /Disable-Feature /FeatureName:Microsoft-Hyper-V-All
dism /Online /Enable-Feature /FeatureName:HypervisorPlatform /FeatureName:VirtualMachinePlatform

echo "Reboot your computer."

msinfo32
echo "Check Virtual protection is close."

pause

@rem dism /Online /Get-Features /Format:table