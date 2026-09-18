@echo off
rem Developing the app: runs it on a separate data-dev\ vault that opens with no
rem passphrase, on port 8767. That vault is not protected, so use made-up data only.
call "%~dp0Budget.cmd" --dev %*
