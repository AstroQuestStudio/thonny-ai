"""Build a ready-to-use portable Thonny with thonny-ai preinstalled (Windows).

    python bundle/build_portable.py <portable-thonny-folder-or-zip> [output.zip]

Get the portable Thonny zip from https://github.com/thonny/thonny/releases
(asset named thonny-<version>-windows-portable.zip).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_thonny_root(folder):
    for current, _, files in os.walk(folder):
        if "thonny.exe" in files and "python.exe" in files:
            return current
    raise SystemExit(f"thonny.exe + python.exe not found under {folder}")


def main(source, output):
    work = tempfile.mkdtemp(prefix="thonny-ai-bundle-")
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as z:
            z.extractall(work)
        src_root = find_thonny_root(work)
    else:
        src_root = find_thonny_root(source)

    target = os.path.join(work, "Thonny-AI")
    shutil.copytree(
        src_root,
        target,
        ignore=shutil.ignore_patterns("user_data", "__pycache__", "*_dev.pth", "thonny_ai*"),
    )
    os.makedirs(os.path.join(target, "user_data"))
    marker = os.path.join(target, "portable_thonny.ini")
    if not os.path.exists(marker):
        with open(marker, "w") as f:
            f.write(";Existence of this file indicates that this instance of Thonny is portable\n")

    subprocess.run(
        [os.path.join(target, "python.exe"), "-m", "pip", "install", ROOT,
         "--no-warn-script-location", "--disable-pip-version-check"],
        check=True,
    )

    output = os.path.abspath(output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    base = output[:-4] if output.endswith(".zip") else output
    shutil.make_archive(base, "zip", work, "Thonny-AI")
    shutil.rmtree(work, ignore_errors=True)
    print(f"Built {base}.zip")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "bundle", "out", "Thonny-AI-portable.zip"))
