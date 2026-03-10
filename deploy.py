#!/usr/bin/env python3
"""서버 배포 스크립트 - .env에서 접속 정보를 읽어 자동 배포"""
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

HOST: str = os.getenv("DEPLOY_HOST", "")
USER: str = os.getenv("DEPLOY_USER", "root")
DEPLOY_PATH: str = os.getenv("DEPLOY_PATH", "")

if not HOST or not DEPLOY_PATH:
    print("❌ .env에 DEPLOY_HOST, DEPLOY_PATH를 설정해주세요.")
    sys.exit(1)

DEPLOY_FILES: list[str] = ["bot.py", "app.py", "api.py", "requirements.txt"]
DEPLOY_STATIC: list[str] = ["static/index.html", "static/manifest.json", "static/sw.js"]
TARGET: str = f"{USER}@{HOST}"


def run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  → {cmd}")
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)


def deploy() -> None:
    print(f"\n🚀 배포 시작: {TARGET}:{DEPLOY_PATH}")

    print("\n📦 파일 업로드 중...")
    for f in DEPLOY_FILES:
        if Path(f).exists():
            run(f"scp -o ConnectTimeout=10 {f} {TARGET}:{DEPLOY_PATH}/{f}")
    for f in DEPLOY_STATIC:
        if Path(f).exists():
            remote_dir: str = f"{DEPLOY_PATH}/{str(Path(f).parent)}"
            run(f"ssh -o ConnectTimeout=10 {TARGET} 'mkdir -p {remote_dir}'")
            run(f"scp -o ConnectTimeout=10 {f} {TARGET}:{DEPLOY_PATH}/{f}")

    print("\n🔄 PM2 재시작 중...")
    result = run(f"ssh -o ConnectTimeout=10 {TARGET} 'cd {DEPLOY_PATH} && pm2 restart trade-bot'")
    print(result.stdout)

    print("\n✅ 배포 완료!")
    result = run(f"ssh -o ConnectTimeout=10 {TARGET} 'sleep 3 && pm2 status trade-bot'")
    print(result.stdout)


if __name__ == "__main__":
    deploy()
