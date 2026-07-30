' Запускает Discord-бота полностью в фоне, без видимого окна консоли.
' Двойной клик по этому файлу (или по ярлыку на него) — и бот просто начинает работать.

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Папка, где лежит этот самый файл (и bot.py рядом с ним)
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Путь к pythonw.exe строим через переменную окружения, чтобы не хранить
' кириллицу прямо в тексте .vbs-файла — из-за особенностей кодировки
' классический VBScript может искажать не-ASCII символы в самом коде.
userProfile = shell.ExpandEnvironmentStrings("%USERPROFILE%")
pythonwPath = userProfile & "\AppData\Local\Python\bin\pythonw.exe"

shell.CurrentDirectory = scriptDir
shell.Run """" & pythonwPath & """ tray_launcher.py", 0, False
