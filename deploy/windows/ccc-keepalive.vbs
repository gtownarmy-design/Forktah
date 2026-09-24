' Hidden launcher for ccc-keepalive.sh. Put a shortcut to this file in the Startup folder
' (shell:startup). It starts the WSL keepalive with no console window; that wsl.exe stays
' running, which keeps WSL - and so the Control Center dashboard - up.
'
' Paths are derived from where this file sits (<repo>\deploy\windows\), so the checkout can
' live anywhere. Backups go to %USERPROFILE%\ccc-backups and evidence is mirrored from
' %USERPROFILE%\ccc-evidence; set CCC_BACKUP_DIR / CCC_EVIDENCE_DIR (as /mnt/... paths) in
' the environment to change them. The WSL distribution is CCC_WSL_DISTRO, default Ubuntu.
Option Explicit
Dim shell, fso, here, repo, profile, distro, backups, evidence, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

Function ToWsl(winPath)
  ' C:\Users\me\x -> /mnt/c/Users/me/x
  ToWsl = "/mnt/" & LCase(Left(winPath, 1)) & Replace(Mid(winPath, 3), "\", "/")
End Function

Function EnvOr(name, fallback)
  Dim v
  v = shell.ExpandEnvironmentStrings("%" & name & "%")
  If v = "%" & name & "%" Or v = "" Then EnvOr = fallback Else EnvOr = v
End Function

here = fso.GetParentFolderName(WScript.ScriptFullName)
repo = ToWsl(fso.GetParentFolderName(fso.GetParentFolderName(here)))
profile = shell.ExpandEnvironmentStrings("%USERPROFILE%")
distro = EnvOr("CCC_WSL_DISTRO", "Ubuntu")
backups = EnvOr("CCC_BACKUP_DIR", ToWsl(profile & "\ccc-backups"))
evidence = EnvOr("CCC_EVIDENCE_DIR", ToWsl(profile & "\ccc-evidence"))

cmd = "wsl.exe -d " & distro & " --exec bash -lc ""CCC_REPO='" & repo & "' CCC_BACKUP_DIR='" & backups & _
      "' CCC_EVIDENCE_DIR='" & evidence & "' exec bash <(tr -d '\r' < '" & repo & "/deploy/windows/ccc-keepalive.sh')"""
shell.Run cmd, 0, False
