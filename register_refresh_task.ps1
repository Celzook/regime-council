# 자동 갱신 작업 등록 — 평일 16:30(KRX 마감 뒤)에 refresh_all.py 를 실행한다. 관리자 권한 불필요(현재 사용자 작업).
# 실행:  powershell -ExecutionPolicy Bypass -File register_refresh_task.ps1
# 파일은 UTF-8 BOM 으로 둔다 — Windows PowerShell 5 는 BOM 이 없으면 한글을 ANSI 로 읽어 깨뜨린다. 작업 이름은 영문(조회·해제 명령에서 깨지지 않게).
# 해제:  Unregister-ScheduledTask -TaskName "RegimeDash Market Refresh" -Confirm:$false
# Refinitiv 데스크톱 세션은 로그인한 사용자 화면에서만 붙으므로 '로그온 상태일 때만 실행' 으로 둔다.
$py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$action = New-ScheduledTaskAction -Execute $py -Argument "refresh_all.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 16:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "RegimeDash Market Refresh" -Action $action -Trigger $trigger -Settings $settings `
  -Description "regime-council: Refinitiv 시세 수집 → 대시보드·백테스트 갱신 (refresh_all.py)" -Force | Out-Null
Get-ScheduledTask -TaskName "RegimeDash Market Refresh" | Select-Object TaskName, State
