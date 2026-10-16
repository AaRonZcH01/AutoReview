Option Explicit
Dim shell, fso, folder, command, powershell
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = """" & powershell & """ -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & folder & "\Start-Current.ps1"""
If WScript.Arguments.Named.Exists("NoBrowser") Then command = command & " -NoBrowser"
shell.Run command, 0, False
