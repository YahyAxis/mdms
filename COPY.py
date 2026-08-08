from pathlib import Path
import pyperclip

ROOT = Path.cwd()

INCLUDE_EXTENSIONS = {
    ".py",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".env",
    ".sql",
    ".txt",
    ".rst",
}

INCLUDE_FILENAMES = {
    ".env",
    "Dockerfile",
    "docker-compose.yml",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "alembic.ini",
    "README.md",
    "CHANGELOG.md",
}

EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    ".venv",
    ".bin",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "node_modules",
    "dist",
    "build",
    ".egg-info",
}

EXCLUDED_FILES = {
    "__init__.py",
    "uv.lock"
}

parts = []
file_count = 0

for path in sorted(ROOT.rglob("*")):

    if not path.is_file():
        continue

    if any(part in EXCLUDED_DIRS for part in path.parts):
        continue

    if path.name in EXCLUDED_FILES:
        continue

    if (
        path.suffix not in INCLUDE_EXTENSIONS
        and path.name not in INCLUDE_FILENAMES
    ):
        continue

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="latin-1")
        except Exception:
            continue
    except Exception:
        continue

    rel = path.relative_to(ROOT)

    parts.append("=" * 90)
    parts.append(f"FILE: {rel}")
    parts.append("=" * 90)
    parts.append("")
    parts.append(text.rstrip())
    parts.append("\n")

    file_count += 1

output = "\n".join(parts)

pyperclip.copy(output)

print(f"Copied {file_count} files.")
print(f"{len(output):,} characters copied to clipboard.")