"""Build the site CSS with the Tailwind standalone CLI (no Node required).

    python scripts/tailwind.py build    one-off minified build
    python scripts/tailwind.py watch    rebuild on template changes

The CLI binary is downloaded once into .tools/. If that download is blocked and Node is
installed, the same Tailwind version is installed from npm instead. Uses only the standard
library so it also runs inside the Docker build stage.
"""

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

TAILWIND_VERSION = "4.3.5"

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "app" / "static" / "css" / "input.css"
OUTPUT = ROOT / "app" / "static" / "css" / "app.css"
TOOLS_DIR = ROOT / ".tools"


def asset_name() -> str:
    system = platform.system()
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    if system == "Windows":
        return "tailwindcss-windows-x64.exe"
    if system == "Darwin":
        return f"tailwindcss-macos-{arch}"
    if system == "Linux":
        return f"tailwindcss-linux-{arch}"
    raise SystemExit(f"Unsupported platform for the Tailwind CLI: {system}")


def ensure_binary() -> Path:
    name = asset_name()
    suffix = ".exe" if name.endswith(".exe") else ""
    binary = TOOLS_DIR / f"tailwindcss-{TAILWIND_VERSION}{suffix}"
    if binary.exists():
        return binary

    url = (
        f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{TAILWIND_VERSION}/{name}"
    )
    print(f"Downloading Tailwind CLI {TAILWIND_VERSION} ...")
    TOOLS_DIR.mkdir(exist_ok=True)
    partial = binary.with_name(binary.name + ".part")
    urllib.request.urlretrieve(url, partial)
    partial.chmod(partial.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    partial.replace(binary)
    return binary


def npm_cli() -> Path | None:
    """Install the same Tailwind version from npm into ./node_modules (git-ignored)."""
    npm = shutil.which("npm")
    if npm is None:
        return None
    modules = ROOT / "node_modules"
    executable = modules / ".bin" / ("tailwindcss.cmd" if os.name == "nt" else "tailwindcss")
    manifest = modules / "tailwindcss" / "package.json"
    installed = manifest.exists() and json.loads(manifest.read_text("utf-8")).get("version")
    if installed != TAILWIND_VERSION or not executable.exists():
        packages = [f"tailwindcss@{TAILWIND_VERSION}", f"@tailwindcss/cli@{TAILWIND_VERSION}"]
        flags = ["--no-save", "--no-package-lock", "--no-audit", "--no-fund"]
        subprocess.run(  # noqa: S603
            [npm, "install", "--prefix", str(ROOT), *flags, *packages], cwd=ROOT, check=True
        )
    return executable


def cli_command() -> list[str]:
    """The standalone binary, or the same version from npm if it cannot be fetched.

    Some networks block GitHub's release-asset host. Where Node is installed, the npm
    package produces identical output.
    """
    try:
        return [str(ensure_binary())]
    except OSError as error:
        print(f"Tailwind CLI download failed ({error}); falling back to npm.")
        executable = npm_cli()
        if executable is None:
            raise SystemExit("npm is not available either; cannot build the CSS.") from error
        return [str(executable)]


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "build"
    if mode not in ("build", "watch"):
        print(__doc__)
        return 2
    command = [*cli_command(), "-i", str(INPUT), "-o", str(OUTPUT)]
    command.append("--minify" if mode == "build" else "--watch")
    return subprocess.run(command, cwd=ROOT, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
