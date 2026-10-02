"""Run the actual Godot scene against a disposable service and restart it.

Requires an already installed Godot executable. Does not download dependencies.
Headless Unicode checks do not verify physical Korean IME or platform exports.
"""

import argparse
import json
import os
from pathlib import Path
import re
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def engine_run(engine, *arguments):
    result = subprocess.run(
        [engine, "--headless", "--path", str(ROOT / "godot"), *arguments],
        capture_output=True, encoding="utf-8", errors="replace", timeout=45, cwd=ROOT,
    )
    output = result.stdout + result.stderr
    if result.returncode or re.search(r"SCRIPT ERROR:|Parse Error:|^ERROR:", output, re.MULTILINE):
        raise RuntimeError("Godot execution failed:\n" + output)
    return output


def service_runtime():
    executable = sys.executable
    environment = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
    if os.name == 'nt' and sys.prefix != sys.base_prefix:
        # Windows venv python.exe redirects to a second process. Start that
        # interpreter directly so the ready PID and the waited PID agree,
        # retaining the venv through the redirector's own path mechanism.
        executable = sys._base_executable
        environment['__PYVENV_LAUNCHER__'] = sys.executable
    return executable, environment


def start_service(database):
    executable, environment = service_runtime()
    process = subprocess.Popen(
        [executable, "-m", "ariadne", "--db", str(database), "serve", "--demo", "--port", "0", "--ready-json"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding="utf-8", env={**environment, "ARIADNE_DATA_DIR": str(database.parent)},
    )
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        information = json.loads(ready.get(timeout=10))
        url = information["url"]
        if information["pid"] != process.pid or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", url):
            raise ValueError("Invalid ready handshake")
        with urlopen(url + "/api/health", timeout=5) as response:
            if response.status != 200 or process.poll() is not None:
                raise ValueError("Service not ready")
        os.environ["ARIADNE_API_BASE"] = url
        return process
    except (queue.Empty, ValueError, KeyError, OSError) as error:
        stop_service(process)
        raise RuntimeError("Disposable service did not start") from error


def stop_service(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process is not None and process.stdout is not None:
        process.stdout.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--godot", default=os.environ.get("GODOT_BIN", "godot"))
    args = parser.parse_args()
    engine = shutil.which(args.godot)
    if engine is None:
        print("Godot executable unavailable; engine validation NOT RUN.", file=sys.stderr)
        return 2
    process = None
    try:
        engine_run(engine, "--editor", "--quit")
        with tempfile.TemporaryDirectory(prefix="ariadne-godot-smoke-") as directory:
            try:
                database = Path(directory) / "synthetic.sqlite3"
                process = start_service(database)
                output = engine_run(engine, "--script", "res://test.gd")
                match = re.search(r"GODOT_SMOKE_OK id=([^\s]+)", output)
                if match is None:
                    raise RuntimeError("Godot smoke completion marker missing:\n" + output)
                stop_service(process)
                process = start_service(database)
                restarted = engine_run(engine, "--script", "res://test.gd", "--", "--verify-id=" + match[1])
                if "GODOT_RESTART_PERSISTENCE_OK" not in restarted:
                    raise RuntimeError("Godot restart verification marker missing:\n" + restarted)
            finally:
                # Windows cannot remove an open SQLite DB. Stop before temp cleanup.
                stop_service(process)
                process = None
        print("Godot scene/API create/edit/audit/completion guard/restart PASS")
        print("Physical IME, visual layout, clipboard, Windows/Web exports NOT verified by this test.")
        return 0
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        stop_service(process)


if __name__ == "__main__":
    raise SystemExit(main())
