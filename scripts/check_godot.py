#!/usr/bin/env python3
"""Check client files; use a real Godot executable when available.

Source checks deliberately do not claim GDScript parsing, rendering, IME or export.
Use --require-runtime in CI, and --api-smoke with a disposable running service.
"""

import argparse
import configparser
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "godot"


def check_delimiters(path):
    """A limited lexical sanity check, not a GDScript parser."""
    source = path.read_text(encoding="utf-8")
    stack = []
    quote = None
    escaped = False
    comment = False
    line = 1
    for character in source:
        if character == "\n":
            line += 1
            comment = False
        if comment:
            continue
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character == "#":
            comment = True
        elif character in "\"'":
            quote = character
        elif character in "([{":
            stack.append((character, line))
        elif character in ")]}":
            expected = {")": "(", "]": "[", "}": "{"}[character]
            if not stack or stack.pop()[0] != expected:
                raise ValueError(f"{path.name}:{line}: delimiter mismatch")
    if quote or stack:
        raise ValueError(f"{path.name}: incomplete quoted string or delimiter")


def source_checks():
    required = ("project.godot", "Main.tscn", "main.gd", "api_client.gd", "test.gd", "export_presets.cfg")
    for filename in required:
        if not (PROJECT / filename).is_file():
            raise ValueError(f"Missing Godot source: {filename}")
    settings = configparser.ConfigParser(interpolation=None)
    settings.read_string("[root]\n" + (PROJECT / "project.godot").read_text(encoding="utf-8"))
    if settings.get("root", "config_version") != "5":
        raise ValueError("Expected Godot 4 project format")
    if settings.get("application", "run/main_scene") != '"res://Main.tscn"':
        raise ValueError("Unexpected main scene")
    if settings.get("rendering", "renderer/rendering_method") != '"gl_compatibility"':
        raise ValueError("Web candidate must use compatibility renderer")
    exports = configparser.ConfigParser(interpolation=None)
    exports.read(PROJECT / "export_presets.cfg", encoding="utf-8")
    for section, platform in (("preset.0", '"Windows Desktop"'), ("preset.1", '"Web"')):
        if exports.get(section, "platform") != platform:
            raise ValueError("Required export preset missing")
    if exports.get("preset.1.options", "variant/thread_support") != "false":
        raise ValueError("Prototype web preset should be single-threaded")
    for path in PROJECT.glob("*.gd"):
        check_delimiters(path)
        source = path.read_text(encoding="utf-8")
        if not source.startswith("extends "):
            raise ValueError(f"{path.name}: missing base class")
        names = re.findall(r"^func (\w+)\(", source, re.MULTILINE)
        if len(names) != len(set(names)):
            raise ValueError(f"{path.name}: duplicate method")
        if re.search(r"\b(FileAccess|DirAccess|SQLite)\b", source):
            raise ValueError(f"{path.name}: UI must not own persisted state")
        for resource in re.findall(r'"res://([^"\n]+\.(?:gd|tscn))"', source):
            if not (PROJECT / resource).is_file():
                raise ValueError(f"{path.name}: missing resource {resource}")
    ui = (PROJECT / "main.gd").read_text(encoding="utf-8")
    for token in ("Command Center", "Calendar", "Funds", "Work Queue", "Evidence & Audit", "expectedVersion", "completionConditions", "/audit", "/api/evidence/"):
        if token not in ui:
            raise ValueError(f"UI contract missing: {token}")
    print("Godot source sanity: PASS (not an engine parser/runtime test)")


def run(command, timeout):
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    output = completed.stdout + completed.stderr
    print(output, end="")
    if completed.returncode or re.search(r"(?:SCRIPT ERROR|Parse Error|Failed to load script)", output):
        raise ValueError(f"Godot command failed: {' '.join(command[:3])}")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", help="Godot 4.x executable; also accepts GODOT_BIN")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--require-runtime", action="store_true")
    parser.add_argument("--api-smoke", action="store_true", help="Requires running disposable API service")
    parser.add_argument("--verify-id", help="Verify saved Korean task after API restart")
    args = parser.parse_args()
    if args.source_only and (args.require_runtime or args.api_smoke or args.verify_id):
        parser.error("--source-only cannot be combined with runtime options")
    source_checks()
    executable = args.godot or os.environ.get("GODOT_BIN") or shutil.which("godot4") or shutil.which("godot")
    if args.source_only or not executable:
        print("Godot engine runtime: NOT RUN; IME/Windows/Web export: NOT VERIFIED")
        if args.require_runtime or args.api_smoke or args.verify_id:
            raise ValueError("Godot executable required; use --godot or GODOT_BIN")
        return
    version = run([executable, "--version"], 15)
    if not version.lstrip().startswith("4."):
        raise ValueError("Godot 4.x is required")
    run([executable, "--headless", "--path", str(PROJECT), "--editor", "--import", "--quit"], 60)
    print("Godot engine import/parse: PASS")
    if args.api_smoke or args.verify_id:
        command = [executable, "--headless", "--path", str(PROJECT), "--script", "res://test.gd"]
        if args.verify_id:
            command += ["--", "--verify-id=" + args.verify_id]
        output = run(command, 90)
        marker = "GODOT_RESTART_PERSISTENCE_OK" if args.verify_id else "GODOT_SMOKE_OK"
        if marker not in output:
            raise ValueError("Godot smoke success marker missing")
        print("Godot headless API/UI flow: PASS; IME and platform export remain manual checks")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, configparser.Error, subprocess.TimeoutExpired) as error:
        print(f"Godot check FAILED: {error}", file=sys.stderr)
        sys.exit(1)
