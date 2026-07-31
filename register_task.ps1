# 註冊 Windows 排程:每個平日 14:45(收盤價確定後)自動跑 snapshot.py
# 用法:在 PowerShell 執行  .\register_task.ps1
$proj = $PSScriptRoot
$python = (Get-Command python).Source
$action = New-ScheduledTaskAction -Execute $python -Argument "snapshot.py" -WorkingDirectory $proj
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 14:45
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName "TWnetvalue-daily" -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "已註冊排程 TWnetvalue-daily:平日 14:45 自動記錄淨值(當天開機較晚也會補跑)"
