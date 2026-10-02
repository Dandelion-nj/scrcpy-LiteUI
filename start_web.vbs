' 源码态启动脚本：双击即可运行，不弹控制台窗口。
' 解释器优先用项目自带的虚拟环境（.build\venv），找不到才退回 PATH 里的 pythonw。
' 换电脑后如果还没建虚拟环境，请先按 docs/开发.md 的「从源码运行」重建，否则退回 PATH 可能装不全依赖。
Set fso = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")

strPath = fso.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = strPath

venvPy = fso.BuildPath(strPath, ".build\venv\Scripts\pythonw.exe")
If fso.FileExists(venvPy) Then
    py = """" & venvPy & """"
Else
    py = "pythonw.exe"
End If

objShell.Run py & " launcher_server.py", 1, False