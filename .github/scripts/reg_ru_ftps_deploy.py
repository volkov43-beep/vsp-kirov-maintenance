#!/usr/bin/env python3
"""Deploy an explicit static-file allowlist to a chrooted REG.RU FTPS home.

Never deletes remote files; no writes occur in dry-run mode.
"""
from __future__ import annotations

import ftplib
import os
from pathlib import Path
import ssl
import sys

ALLOWED_MODES = {"off", "dry-run", "live"}
ALLOWED_ROOT_FILES = {"index.html", "styles.css"}


def fail(message: str) -> None:
    print(f"::error::{message}")
    raise SystemExit(1)


def release_files(root: Path) -> list[tuple[Path, str]]:
    if not (root / "index.html").is_file() or not (root / "styles.css").is_file():
        fail("Required static entrypoints index.html and styles.css are missing.")
    selected: list[tuple[Path, str]] = []
    for item in root.rglob("*"):
        if item.is_symlink():
            fail("Symlinks are not allowed in the publish package.")
        if not item.is_file():
            continue
        relative = item.relative_to(root).as_posix()
        if relative not in ALLOWED_ROOT_FILES and not relative.startswith("assets/"):
            fail("Unexpected file in publish package: " + relative)
        selected.append((item, relative))
    if not selected:
        fail("Publish package is empty.")
    # Publish the HTML entrypoint last, after the CSS and assets.
    return sorted(selected, key=lambda entry: (entry[1] == "index.html", entry[1]))


def verify_ftp_home(ftp: ftplib.FTP_TLS) -> None:
    home = ftp.pwd()
    names = {Path(name.rstrip("/")).name for name in ftp.nlst()}
    if "index.html" not in names:
        fail("FTP home lacks REG.RU index.html; refusing to touch an unknown directory.")
    if "sf676.ru" in names:
        fail("FTP home appears to expose another site; refusing deployment.")
    try:
        ftp.cwd("..")
    except ftplib.error_perm:
        return
    if ftp.pwd() != home:
        fail("FTP login can traverse above its home directory; refusing deployment.")
    ftp.cwd(home)


def ensure_directory(ftp: ftplib.FTP_TLS, relative: str) -> None:
    try:
        ftp.mkd(relative)
    except ftplib.error_perm:
        # Existing directory is acceptable; other failures are not.
        original = ftp.pwd()
        ftp.cwd(relative)
        ftp.cwd(original)


def main() -> None:
    mode = (os.environ.get("REG_DEPLOY_MODE") or "off").strip().lower()
    if mode not in ALLOWED_MODES:
        fail("REG_DEPLOY_MODE must be off, dry-run, or live.")
    root_value = os.environ.get("RELEASE_DIR", "")
    if not root_value:
        fail("RELEASE_DIR is missing.")
    root = Path(root_value).resolve()
    files = release_files(root)
    total_bytes = sum(path.stat().st_size for path, _ in files)
    print(f"Prepared {len(files)} static files ({total_bytes} bytes).")
    if mode == "off":
        print("Deploy is OFF. No network connection was made.")
        return

    host = os.environ.get("FTP_HOST", "")
    user = os.environ.get("FTP_USER", "")
    password = os.environ.get("FTP_PASS", "")
    if not host or not user or not password:
        fail("One or more required REG_FTP_* secrets are missing.")

    ftp = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=20)
    try:
        ftp.connect(host, 21)
        ftp.login(user, password)
        ftp.prot_p()
        verify_ftp_home(ftp)
        print("FTPS TLS, credentials, encrypted listing and home-directory guard: PASS.")

        if mode == "dry-run":
            print("DRY RUN: files that would be uploaded (no writes, no deletions):")
            for _, relative in files:
                print("  " + relative)
            print("DRY RUN: PASS.")
            return

        # Live mode requires REG_DEPLOY_MODE=live on the main branch.
        directories = {
            "/".join(relative.split("/")[:i])
            for _, relative in files
            for i in range(1, len(relative.split("/")))
        }
        for relative in sorted(directories, key=lambda d: (d.count("/"), d)):
            ensure_directory(ftp, relative)
        for path, relative in files:
            with path.open("rb") as source:
                ftp.storbinary("STOR " + relative, source)
            print("Uploaded: " + relative)
        print(f"LIVE: uploaded {len(files)} files. No remote files were deleted.")
    except ssl.SSLError:
        fail("TLS certificate or handshake failed. Never disable certificate verification.")
    except (ftplib.all_errors, OSError) as error:
        fail("FTPS connection or transfer failed (" + type(error).__name__ + ").")
    finally:
        ftp.close()


if __name__ == "__main__":
    main()
