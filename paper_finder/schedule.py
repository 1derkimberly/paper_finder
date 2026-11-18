"""Phase 5: weekly automatic update via Windows Task Scheduler (runs scripts/weekly_update.ps1)."""

from __future__ import annotations

import subprocess
import sys

from paper_finder.config import ROOT

TASK_NAME = "PaperFinderWeekly"
SCRIPT = ROOT / "scripts" / "weekly_update.ps1"
DAYS = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday", "FRI": "Friday",
        "SAT": "Saturday", "SUN": "Sunday"}


class ScheduleError(RuntimeError):
    pass


def _ps(script: str) -> subprocess.CompletedProcess:
    if sys.platform != "win32":
        raise ScheduleError("자동 업데이트 등록은 Windows 작업 스케줄러만 지원합니다 (macOS/Linux는 cron 사용)")
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60)


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def install_script(day: str = "MON", at: str = "09:00") -> str:
    if day.upper() not in DAYS:
        raise ScheduleError(f"요일은 {', '.join(DAYS)} 중 하나")
    arg = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{SCRIPT}"'
    return "\n".join([
        f"$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {_q(arg)} -WorkingDirectory {_q(str(ROOT))}",
        f"$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {DAYS[day.upper()]} -At {_q(at)}",
        # run a missed week (laptop off/asleep) as soon as the PC is available again
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)",
        f"Register-ScheduledTask -TaskName {_q(TASK_NAME)} -Action $a -Trigger $t -Settings $s "
        f"-Description 'Paper Finder weekly update (PubMed/OpenAlex/Claude) and new-paper report' -Force | Out-Null",
    ])


def install(day: str = "MON", at: str = "09:00") -> None:
    r = _ps(install_script(day, at))
    if r.returncode != 0:
        raise ScheduleError(r.stderr.strip() or r.stdout.strip())


def remove() -> bool:
    r = _ps(f"Unregister-ScheduledTask -TaskName {_q(TASK_NAME)} -Confirm:$false -ErrorAction Stop")
    return r.returncode == 0


def status() -> str | None:
    """'<state> · 다음 실행 <time> · 마지막 결과 <code>' or None when not registered."""
    r = _ps(f"$t = Get-ScheduledTask -TaskName {_q(TASK_NAME)} -ErrorAction Stop; "
            f"$i = $t | Get-ScheduledTaskInfo; "
            "\"$($t.State)|$($i.NextRunTime)|$($i.LastRunTime)|$($i.LastTaskResult)\"")
    if r.returncode != 0 or "|" not in r.stdout:
        return None
    state, nxt, last, code = r.stdout.strip().split("|")
    return f"{state} · 다음 실행 {nxt or '-'} · 마지막 실행 {last or '-'} (결과 {code})"
