import os
import sys
import zipfile
import tarfile
import urllib.request
import subprocess
import platform
from pathlib import Path
from contextlib import contextmanager
from typing import Tuple, Optional

from config.settings import settings

FPCALC_WINDOWS_URL = "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-windows-x86_64.zip"
FPCALC_MACOS_URL = "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-macos-universal.tar.gz"
FPCALC_LINUX_URL = "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-linux-x86_64.tar.gz"
FPCALC_LINUX_ARM_URL = "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-linux-arm64.tar.gz"

@contextmanager
def suppress_c_stderr():
    try:
        null_fd = os.open(os.devnull, os.O_RDWR)
        try:
            saved_stderr = os.dup(2)
            os.dup2(null_fd, 2)
            yield
        finally:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
            os.close(null_fd)
    except Exception:
        yield

def get_fpcalc_path() -> str:
    binary_name = "fpcalc.exe" if sys.platform == "win32" else "fpcalc"
    target_path = settings.BIN_DIR / binary_name
    if target_path.exists():
        if sys.platform != "win32" and not os.access(target_path, os.X_OK):
            try:
                os.chmod(target_path, 0o755)
            except Exception:
                pass
        return str(target_path)
    
    system_fpcalc = "fpcalc"
    try:
        res = subprocess.run([system_fpcalc, "-v"], capture_output=True, text=True, check=False, timeout=5.0)
        if res.returncode == 0:
            return system_fpcalc
    except Exception:
        pass

    return str(target_path)

def ensure_fpcalc() -> bool:
    bin_path = get_fpcalc_path()
    if os.path.exists(bin_path) or bin_path == "fpcalc":
        return True

    settings.BIN_DIR.mkdir(parents=True, exist_ok=True)
    binary_name = "fpcalc.exe" if sys.platform == "win32" else "fpcalc"
    target_exe = settings.BIN_DIR / binary_name

    url = None
    if sys.platform == "win32":
        url = FPCALC_WINDOWS_URL
    elif sys.platform == "darwin":
        url = FPCALC_MACOS_URL
    elif sys.platform.startswith("linux"):
        machine = platform.machine().lower()
        if "arm" in machine or "aarch64" in machine:
            url = FPCALC_LINUX_ARM_URL
        else:
            url = FPCALC_LINUX_URL

    if not url:
        print(f"[-] No auto-download URL mapped for platform: {sys.platform}")
        return False

    try:
        print(f"[*] Chromaprint fpcalc missing. Automatically downloading from {url}...")
        archive_dest = settings.BIN_DIR / ("fpcalc.zip" if url.endswith(".zip") else "fpcalc.tar.gz")
        
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as response, open(archive_dest, "wb") as out_file:
            out_file.write(response.read())

        if url.endswith(".zip"):
            with zipfile.ZipFile(archive_dest, "r") as zip_ref:
                for file_info in zip_ref.infolist():
                    if file_info.filename.endswith(binary_name):
                        file_info.filename = os.path.basename(file_info.filename)
                        zip_ref.extract(file_info, settings.BIN_DIR)
                        break
        else:
            with tarfile.open(archive_dest, "r:gz") as tar_ref:
                for member in tar_ref.getmembers():
                    if member.name.endswith(binary_name) or os.path.basename(member.name) == binary_name:
                        member.name = os.path.basename(member.name)
                        tar_ref.extract(member, settings.BIN_DIR)
                        break

        if archive_dest.exists():
            os.remove(archive_dest)

        if target_exe.exists():
            if sys.platform != "win32":
                os.chmod(target_exe, 0o755)
            print(f"[+] fpcalc successfully installed to {target_exe}")
            return True
    except Exception as ex:
        print(f"[-] Auto-download of fpcalc failed: {ex}")

    return False

def generate_chromaprint(filepath: str, duration_sec: int = 120, timeout_sec: float = 30.0) -> Tuple[Optional[int], Optional[str]]:
    ensure_fpcalc()
    bin_path = get_fpcalc_path()
    if not os.path.exists(filepath) or not os.path.exists(bin_path):
        return None, None

    try:
        with suppress_c_stderr():
            res = subprocess.run(
                [bin_path, "-length", str(duration_sec), filepath],
                capture_output=True, text=True, check=False, timeout=timeout_sec
            )
        if res.returncode != 0:
            return None, None

        duration, fingerprint = None, None
        for line in res.stdout.splitlines():
            if line.startswith("DURATION="):
                try:
                    duration = int(line.split("=")[1])
                except ValueError:
                    pass
            elif line.startswith("FINGERPRINT="):
                fingerprint = line.split("=")[1].strip()

        if fingerprint and len(fingerprint) >= 10:
            return duration, fingerprint
        return None, None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, Exception):
        return None, None