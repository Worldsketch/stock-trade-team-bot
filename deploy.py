#!/usr/bin/env python3
"""서버 배포 스크립트 - .env에서 접속 정보를 읽어 자동 배포"""
import argparse
from datetime import datetime, timedelta
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
import shlex
from dotenv import load_dotenv
import os

load_dotenv()

HOST: str = os.getenv("DEPLOY_HOST", "")
USER: str = os.getenv("DEPLOY_USER", "root")
DEPLOY_PATH: str = os.getenv("DEPLOY_PATH", "")
PM2_NAME: str = os.getenv("DEPLOY_PM2_NAME", "trade-team-bot").strip() or "trade-team-bot"

if not HOST or not DEPLOY_PATH:
    print("❌ .env에 DEPLOY_HOST, DEPLOY_PATH를 설정해주세요.")
    sys.exit(1)

DEPLOY_FILES: list[str] = ["bot.py", "app.py", "api.py", "requirements.txt"]
DEPLOY_STATIC: list[str] = ["static/index.html", "static/manifest.json", "static/sw.js"]
DEPLOY_DIRS: list[str] = ["routes", "services"]
TARGET: str = f"{USER}@{HOST}"
SSH_COMMON_OPTS: str = "-o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"


def run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  → {cmd}")
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)


def upload_files() -> None:
    print(f"\n📦 파일 업로드 시작: {TARGET}:{DEPLOY_PATH}")
    print("\n📦 파일 업로드 중...")
    run(f"ssh {SSH_COMMON_OPTS} {TARGET} 'mkdir -p {DEPLOY_PATH}'")
    for f in DEPLOY_FILES:
        if Path(f).exists():
            run(f"scp {SSH_COMMON_OPTS} {f} {TARGET}:{DEPLOY_PATH}/{f}")
    for f in DEPLOY_STATIC:
        if Path(f).exists():
            remote_dir: str = f"{DEPLOY_PATH}/{str(Path(f).parent)}"
            run(f"ssh {SSH_COMMON_OPTS} {TARGET} 'mkdir -p {remote_dir}'")
            run(f"scp {SSH_COMMON_OPTS} {f} {TARGET}:{DEPLOY_PATH}/{f}")
    for d in DEPLOY_DIRS:
        if Path(d).exists():
            run(f"ssh {SSH_COMMON_OPTS} {TARGET} 'mkdir -p {DEPLOY_PATH}/{d}'")
            run(f"scp {SSH_COMMON_OPTS} -r {d} {TARGET}:{DEPLOY_PATH}/")
    print("\n✅ 파일 업로드 완료")


def restart_now() -> None:
    print("\n🔄 PM2 재시작 중...")
    remote_cmd = (
        f"cd {shlex.quote(DEPLOY_PATH)} && "
        f"(pm2 describe {shlex.quote(PM2_NAME)} >/dev/null 2>&1 "
        f"&& pm2 restart {shlex.quote(PM2_NAME)} --update-env "
        f"|| pm2 start app.py --name {shlex.quote(PM2_NAME)} --interpreter python3 --time)"
    )
    result = run(f"ssh {SSH_COMMON_OPTS} {TARGET} {shlex.quote(remote_cmd)}")
    print(result.stdout)

    print("\n✅ 재시작 완료!")
    status_cmd = f"sleep 3 && pm2 status {shlex.quote(PM2_NAME)}"
    result = run(f"ssh {SSH_COMMON_OPTS} {TARGET} {shlex.quote(status_cmd)}")
    print(result.stdout)


def _parse_schedule_time(schedule: str, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    text = schedule.strip()

    # HH:MM 형식이면 오늘/내일로 자동 보정
    try:
        hm = datetime.strptime(text, "%H:%M")
        target = now.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return target
    except ValueError:
        pass

    # YYYY-MM-DD HH:MM 형식
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=tz, second=0, microsecond=0)
    except ValueError:
        pass

    # YYYY-MM-DD HH:MM:SS 형식
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=tz, microsecond=0)
    except ValueError as exc:
        raise ValueError("시간 형식 오류: 'HH:MM' 또는 'YYYY-MM-DD HH:MM[:SS]'를 사용하세요.") from exc


def schedule_restart(schedule: str, tz_name: str) -> None:
    target_dt = _parse_schedule_time(schedule, tz_name)
    now_dt = datetime.now(ZoneInfo(tz_name))
    delay_sec = int((target_dt - now_dt).total_seconds())
    if delay_sec <= 0:
        raise ValueError("예약 시간이 현재보다 이후여야 합니다.")

    remote_path = shlex.quote(DEPLOY_PATH)
    remote_log = shlex.quote(f"{DEPLOY_PATH}/deploy_scheduled_restart.log")
    remote_pid = shlex.quote(f"{DEPLOY_PATH}/deploy_scheduled_restart.pid")
    remote_script = (
        f"LOG={remote_log}; PID={remote_pid}; "
        "for P in $(pgrep -f \"deploy_scheduled_restart.log; \\(sleep\" 2>/dev/null || true); do "
        "pkill -P \"$P\" 2>/dev/null || true; kill \"$P\" 2>/dev/null || true; done; "
        f"if [ -f \"$PID\" ]; then OLD_PID=$(cat \"$PID\" 2>/dev/null || true); "
        f"if [ -n \"$OLD_PID\" ] && kill -0 \"$OLD_PID\" 2>/dev/null; then kill \"$OLD_PID\" 2>/dev/null || true; fi; fi; "
        f"(sleep {delay_sec}; cd {remote_path} && "
        f"pm2 restart {shlex.quote(PM2_NAME)} --update-env >> \"$LOG\" 2>&1 && "
        f"pm2 status {shlex.quote(PM2_NAME)} >> \"$LOG\" 2>&1) >/dev/null 2>&1 & "
        f"echo $! > \"$PID\""
    )
    run(f"ssh {SSH_COMMON_OPTS} {TARGET} {shlex.quote(remote_script)}")
    print(
        f"\n🕒 재시작 예약 완료: {target_dt.strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"(약 {delay_sec}초 후)"
    )
    print(f"📄 서버 로그: {DEPLOY_PATH}/deploy_scheduled_restart.log")
    print(f"🧭 예약 PID 파일: {DEPLOY_PATH}/deploy_scheduled_restart.pid")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="파일 업로드 + PM2 재시작 배포 스크립트")
    parser.add_argument("--upload-only", action="store_true", help="서버에 파일만 업로드하고 재시작하지 않음")
    parser.add_argument("--restart-only", action="store_true", help="파일 업로드 없이 PM2만 재시작")
    parser.add_argument(
        "--schedule-restart",
        type=str,
        default="",
        help="재시작 예약 시간 (예: '21:50' 또는 '2026-03-17 21:50')",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="Asia/Seoul",
        help="예약 시간 해석 타임존 (기본: Asia/Seoul)",
    )
    args = parser.parse_args()
    if args.upload_only and args.restart_only:
        parser.error("--upload-only 와 --restart-only 는 동시에 사용할 수 없습니다.")
    return args


if __name__ == "__main__":
    args = parse_args()
    print(f"\n🚀 배포 작업 시작: {TARGET}:{DEPLOY_PATH}")

    if not args.restart_only:
        upload_files()

    if args.schedule_restart:
        schedule_restart(args.schedule_restart, args.timezone)
    elif not args.upload_only:
        restart_now()
