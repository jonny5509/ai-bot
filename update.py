from __future__ import annotations

import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPOSITORY = "jonny5509/ai-bot"
BRANCH = "main"
ARCHIVE_URL = f"https://github.com/{REPOSITORY}/archive/refs/heads/{BRANCH}.zip"

PRESERVE_NAMES = {
    ".env", ".git", ".venv", "venv", "__pycache__",
    "data", "logs", "backups",
}
PRESERVE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
PRESERVE_FILES = {"credentials.key", "encryption.key"}


def should_preserve(path: Path) -> bool:
    return (
        path.name in PRESERVE_NAMES
        or path.name in PRESERVE_FILES
        or path.suffix.lower() in PRESERVE_SUFFIXES
    )


def main() -> int:
    root = Path(__file__).resolve().parent
    if not (root / "main.py").is_file():
        print("ERROR: Run this updater from the bot's main folder (the folder containing main.py).")
        return 1

    print(f"Checking GitHub for updates: {REPOSITORY} ({BRANCH})")
    request = urllib.request.Request(
        ARCHIVE_URL,
        headers={"User-Agent": "EmlaLock-AI-Bot-Updater"},
    )

    try:
        with tempfile.TemporaryDirectory(prefix="emlalock-update-") as temp_dir:
            temp = Path(temp_dir)
            archive = temp / "latest.zip"
            with urllib.request.urlopen(request, timeout=60) as response:
                archive.write_bytes(response.read())

            extracted = temp / "extracted"
            extracted.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    target = (extracted / member.filename).resolve()
                    if extracted.resolve() not in target.parents and target != extracted.resolve():
                        raise RuntimeError("The GitHub archive contains an unsafe file path.")
                bundle.extractall(extracted)

            source_dirs = [p for p in extracted.iterdir() if p.is_dir()]
            if len(source_dirs) != 1 or not (source_dirs[0] / "main.py").is_file():
                raise RuntimeError("Could not find the bot files in the downloaded GitHub archive.")

            source = source_dirs[0]
            added: list[str] = []
            updated: list[str] = []
            unchanged: list[str] = []
            ignored_parts = {"__pycache__", ".pytest_cache"}

            # Compare individual files before copying so the updater can show
            # exactly what changed, rather than only counting top-level items.
            for item in source.rglob("*"):
                if not item.is_file():
                    continue
                relative = item.relative_to(source)
                if should_preserve(root / relative):
                    continue
                if ignored_parts.intersection(relative.parts) or item.suffix == ".pyc":
                    continue

                destination = root / relative
                if not destination.exists():
                    added.append(relative.as_posix())
                elif destination.read_bytes() != item.read_bytes():
                    updated.append(relative.as_posix())
                else:
                    unchanged.append(relative.as_posix())

            for item in source.iterdir():
                destination = root / item.name
                if should_preserve(destination):
                    continue
                if item.is_dir():
                    shutil.copytree(
                        item,
                        destination,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(
                            "__pycache__", "*.pyc", ".pytest_cache"
                        ),
                    )
                else:
                    shutil.copy2(item, destination)

        print("\\nUpdate completed successfully.")
        print(f"Added: {len(added)} file(s)")
        for name in added:
            print(f"  + {name}")
        print(f"Updated: {len(updated)} file(s)")
        for name in updated:
            print(f"  * {name}")
        if not added and not updated:
            print("  No new or changed files were found.")
        print(f"Unchanged: {len(unchanged)} file(s)")
        print("Preserved: .env, local databases, credentials, logs, and virtual environments.")
        print("Note: Files removed from GitHub are not automatically deleted locally.")
        if "requirements.txt" in added or "requirements.txt" in updated:
            print("Dependencies changed. Run: python -m pip install -r requirements.txt")
        print("Restart the bot with: python main.py")
        return 0

    except Exception as exc:
        print(f"Update failed: {exc}")
        print("Check your internet connection and try again.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
