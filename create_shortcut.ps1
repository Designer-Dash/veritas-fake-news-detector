$WshShell = New-Object -ComObject WScript.Shell
$Desktop = [System.Environment]::GetFolderPath('Desktop')

# Remove obsolete bot shortcuts if present
Remove-Item "$Desktop\Veritas WhatsApp Bot.lnk" -ErrorAction SilentlyContinue
Remove-Item "$Desktop\Test WhatsApp Bot.lnk" -ErrorAction SilentlyContinue

# Shortcut 1: Master Control Menu
$Shortcut1 = $WshShell.CreateShortcut("$Desktop\Veritas Master Control.lnk")
$Shortcut1.TargetPath = 'c:\Users\Lenovo\OneDrive\New folder\fake news detection\00_QUICK_START.bat'
$Shortcut1.WorkingDirectory = 'c:\Users\Lenovo\OneDrive\New folder\fake news detection'
$Shortcut1.Description = 'Launch Veritas Master Control Menu'
$Shortcut1.Save()

# Shortcut 2: Start Server
$Shortcut2 = $WshShell.CreateShortcut("$Desktop\Veritas Backend Server.lnk")
$Shortcut2.TargetPath = 'c:\Users\Lenovo\OneDrive\New folder\fake news detection\01_START_SERVER.bat'
$Shortcut2.WorkingDirectory = 'c:\Users\Lenovo\OneDrive\New folder\fake news detection'
$Shortcut2.Description = 'Start Veritas Backend Detection Server'
$Shortcut2.Save()

Write-Host "Desktop shortcuts updated successfully!"
