#!/usr/bin/env python3
"""Organize one folder with a locally running Ollama/Gemma model."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


MANIFEST_NAME = ".slothsort-manifest.json"
CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


@dataclass(frozen=True)
class Move:
    source: str
    category: str
    reason: str


def scan_files(root: Path) -> list[dict[str, object]]:
    files = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.name.startswith(".") or path.name == MANIFEST_NAME:
            continue
        files.append(
            {
                "name": path.name,
                "extension": path.suffix.lower() or "(none)",
                "size_bytes": path.stat().st_size,
            }
        )
    return files


def build_prompt(files: list[dict[str, object]]) -> str:
    return (
        "You organize files into short, useful folder categories. Return JSON only with "
        'this shape: {"moves":[{"source":"exact filename","category":"Folder",'
        '"reason":"brief reason"}]}. Include every input file exactly once. Preserve each '
        "source filename exactly. Category names must be 1-40 characters and contain only "
        "letters, numbers, spaces, hyphens, or underscores. Do not use paths. Files:\n"
        + json.dumps(files, ensure_ascii=False, indent=2)
    )


def call_ollama(base_url: str, model: str, prompt: str) -> dict[str, object]:
    payload = json.dumps(
        {"model": model, "prompt": prompt, "format": "json", "stream": False}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            envelope = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Could not reach Ollama at {base_url}. Start Ollama and pull the model first."
        ) from error
    try:
        return json.loads(envelope["response"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Ollama returned an invalid JSON plan") from error


def _inside_root(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_plan(
    raw_plan: dict[str, object], root: Path, candidate_names: list[str]
) -> list[Move]:
    raw_moves = raw_plan.get("moves")
    if not isinstance(raw_moves, list):
        raise ValueError("plan must contain a moves list")

    candidates = set(candidate_names)
    seen: set[str] = set()
    moves: list[Move] = []
    for item in raw_moves:
        if not isinstance(item, dict):
            raise ValueError("every move must be an object")
        source = item.get("source")
        category = item.get("category")
        reason = item.get("reason", "")
        if not all(isinstance(value, str) for value in (source, category, reason)):
            raise ValueError("source, category, and reason must be strings")
        if source not in candidates or source in seen:
            raise ValueError("the plan must include every candidate exactly once")
        if not CATEGORY_RE.fullmatch(category) or category in {".", ".."}:
            raise ValueError(f"invalid category: {category!r}")
        source_path = root / source
        target_path = root / category / source
        if not _inside_root(root, source_path) or not _inside_root(root, target_path):
            raise ValueError("planned paths must stay inside the selected folder")
        seen.add(source)
        moves.append(Move(source, category, reason.strip()))

    if seen != candidates:
        raise ValueError("the plan must include every candidate exactly once")
    return moves


def print_plan(moves: list[Move]) -> None:
    print("\nSlothSort plan (dry-run):")
    for move in moves:
        suffix = f" — {move.reason}" if move.reason else ""
        print(f"  {move.source} -> {move.category}/{move.source}{suffix}")


def _write_manifest(root: Path, moves: list[Move]) -> Path:
    manifest = root / MANIFEST_NAME
    data = {"root": str(root.resolve()), "moves": [asdict(move) for move in moves]}
    handle, temp_name = tempfile.mkstemp(prefix=".slothsort-", suffix=".json", dir=root)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(temp_name, manifest)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return manifest


def apply_plan(root: Path, moves: list[Move]) -> None:
    manifest = root / MANIFEST_NAME
    if manifest.exists():
        raise FileExistsError(f"{manifest.name} already exists; undo it before applying a new plan")

    pairs: list[tuple[Path, Path]] = []
    for move in moves:
        source = root / move.source
        target = root / move.category / move.source
        if not source.is_file():
            raise FileNotFoundError(f"source file is missing: {source.name}")
        if target.exists():
            raise FileExistsError(f"destination already exists: {target}")
        if not _inside_root(root, target):
            raise ValueError("destination escaped the selected folder")
        pairs.append((source, target))

    _write_manifest(root, moves)
    completed: list[tuple[Path, Path]] = []
    try:
        for source, target in pairs:
            target.parent.mkdir(exist_ok=True)
            shutil.move(str(source), str(target))
            completed.append((source, target))
    except Exception:
        for source, target in reversed(completed):
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
        manifest.unlink(missing_ok=True)
        raise


def undo(root: Path) -> None:
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        raise FileNotFoundError(f"no {MANIFEST_NAME} found in {root}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("manifest is not valid JSON") from error
    if data.get("root") != str(root.resolve()):
        raise ValueError("manifest belongs to a different folder")
    raw_moves = data.get("moves")
    if not isinstance(raw_moves, list):
        raise ValueError("manifest must contain a moves list")
    candidate_names = [item.get("source") for item in raw_moves if isinstance(item, dict)]
    if len(candidate_names) != len(raw_moves) or not all(
        isinstance(name, str) for name in candidate_names
    ):
        raise ValueError("manifest contains an invalid move")
    moves = validate_plan({"moves": raw_moves}, root, candidate_names)

    pairs = [(root / move.source, root / move.category / move.source) for move in moves]
    for source, target in pairs:
        if source.exists() and target.exists():
            raise FileExistsError(f"cannot restore {source.name}; both source and target exist")
        if not source.exists() and not target.exists():
            raise FileNotFoundError(f"cannot restore {source.name}; target is missing")

    for source, target in reversed(pairs):
        if target.exists():
            shutil.move(str(target), str(source))
    for move in moves:
        category = root / move.category
        if category.is_dir() and not any(category.iterdir()):
            category.rmdir()
    manifest.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Organize one folder with a local Ollama/Gemma model."
    )
    parser.add_argument("folder", type=Path, help="folder whose immediate files should be organized")
    parser.add_argument("--model", default="gemma3:1b", help="Ollama model name")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--apply", action="store_true", help="apply the validated plan")
    parser.add_argument("--undo", action="store_true", help="undo the latest applied plan")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.folder.expanduser().resolve()
    if not root.is_dir():
        print(f"error: folder does not exist: {root}", file=sys.stderr)
        return 2
    try:
        if args.undo:
            undo(root)
            print("Restored files from the latest SlothSort manifest.")
            return 0

        files = scan_files(root)
        if not files:
            print("Nothing to organize.")
            return 0
        raw_plan = call_ollama(args.ollama_url, args.model, build_prompt(files))
        moves = validate_plan(raw_plan, root, [str(item["name"]) for item in files])
        print_plan(moves)
        if args.apply:
            apply_plan(root, moves)
            print(f"\nMoved {len(moves)} files. Run --undo to restore them.")
        else:
            print("\nNo files changed. Re-run with --apply after reviewing the plan.")
        return 0
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
