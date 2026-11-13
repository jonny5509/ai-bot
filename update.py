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
            copied = 0
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
                copied += 1

        print(f"Update files copied successfully ({copied} top-level items).")
        print("Your .env file and local database files were left untouched.")
        print("If requirements.txt changed, run: python -m pip install -r requirements.txt")
        print("Restart the bot with: python main.py")
        return 0

    except Exception as exc:
        print(f"Update failed: {exc}")
        print("Check your internet connection and try again.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
