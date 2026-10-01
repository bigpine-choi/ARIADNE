"""Run with python -m ariadne. All commands are local and read-only externally."""

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .config import default_database


def main(argv=None):
    parser = argparse.ArgumentParser(description="ARIADNE v0.1 로컬 업무 기반")
    parser.add_argument("--db", type=Path, default=default_database(), help="운영 DB 경로 (기본: 소스 저장소 밖 사용자 데이터 폴더)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="환경 및 미검증 연결 확인")
    sub.add_parser("init", help="로컬 저장 구조 초기화")
    sub.add_parser("demo", help="가상 업무 데이터 생성")
    sub.add_parser("list", help="저장된 업무 조회")
    serve = sub.add_parser("serve", help="로컬 웹 화면과 JSON API 실행")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--demo", action="store_true", help="가상 데이터 추가")
    serve.add_argument("--automation-settings", type=Path, help="로컬 파일 수집 설정 경로")
    serve.add_argument("--ready-json", action="store_true", help=argparse.SUPPRESS)
    backup = sub.add_parser("backup", help="SQLite 일관 백업 생성")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        print(json.dumps({
            "version": __version__, "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
            "pythonSupported": sys.version_info >= (3, 12),
            "godotOnPath": bool(shutil.which("godot") or shutil.which("godot4")),
            "graph": "not-configured", "externalAi": "disabled",
            "companyStorageApproval": "not-verified", "companyAiScope": "not-verified",
            "windowsIme": "not-verified", "webExport": "not-verified",
        }, ensure_ascii=False, indent=2))
        return 0 if sys.version_info >= (3, 12) else 1
    from .demo import seed_demo
    from .repository import Repository
    repository = Repository(args.db)
    try:
        if args.command == "init":
            print("로컬 저장 구조 초기화 완료")
        elif args.command == "demo":
            seed_demo(repository)
            print("가상 업무 생성 완료")
        elif args.command == "list":
            print(json.dumps(repository.list_work_items(), ensure_ascii=False, indent=2))
        elif args.command == "backup":
            repository.backup(args.destination)
            print("백업 생성 완료")
        elif args.command == "serve":
            if not 0 <= args.port < 65536:
                parser.error("port must be between 0 and 65535 (0 selects a free port)")
            if args.demo:
                seed_demo(repository)
            from .server import create_server
            from .automation_store import AutomationStore
            from .local_settings import load_settings
            from .credentials import CredentialStore
            from .hiworks import DraftStore
            from .config import data_directory
            from .calendar import CalendarStore
            from .workspace import Workspace
            from .browser_bridge import BrowserBridge
            settings_path=args.automation_settings or data_directory()/'automation.json'
            automation_store=AutomationStore(data_directory()/'automation.sqlite3')
            calendar_store=CalendarStore(data_directory()/'calendar.sqlite3')
            bridge=BrowserBridge(automation_store,data_directory());bridge.start()
            workspace=Workspace(automation_store,settings_path,repository,calendar_store=calendar_store,browser_bridge=bridge)
            server=create_server(repository,port=args.port,automation_store=automation_store,
                automation_settings=workspace.settings(),credential_store=CredentialStore(),
                draft_store=DraftStore(data_directory()/'hiworks.sqlite3'),workspace=workspace,calendar_store=calendar_store)
            actual_port = server.server_address[1]
            if args.ready_json:
                print(json.dumps({"url": f"http://127.0.0.1:{actual_port}", "pid": __import__("os").getpid()}), flush=True)
            else:
                print(f"ARIADNE v{__version__}: http://127.0.0.1:{actual_port} (로컬 파일럿)", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
                workspace.close()
                bridge.close()
                automation_store.close()
    finally:
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

