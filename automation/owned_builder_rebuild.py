#!/usr/bin/env python3
"""Rebuild the Windows BlueShell Builder with release/update URLs owned by this repository.

This script runs on a Windows GitHub Actions runner. It checks out an upstream-mirrored
tag supplied by the caller, patches only a temporary source tree, builds the bootstrapper
and GUI, reuses verified tool parts from this repository's builder-parts release, then
publishes new content-addressed parts and the three versioned release assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

REPO = "ZuanCrisp/MarioKartWorld-Port"
PARTS_TAG = "builder-parts"
TOOLS_TO_REUSE = ("cpm", "emsdk", "python", "wgpu-native")
PREBUILT_EXTENSIONS = (".dll", ".lib", ".exe", ".sha1")
USER_AGENT = "BlueShell-owned-builder-rebuild/1.0"


def fail(message: str) -> "NoReturn":
    raise RuntimeError(message)


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    print("+", subprocess.list2cmdline([str(x) for x in command]), flush=True)
    result = subprocess.run(
        [str(x) for x in command],
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if result.returncode:
        fail(f"Command failed ({result.returncode}): {command[0]}")
    return result.stdout


def request_bytes(url: str, *, api: bool = False, timeout: int = 900) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if api:
        headers["Accept"] = "application/vnd.github+json"
        if token:
            headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    last: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 4:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 4:
                raise
            last = exc
        delay = min(30, 2 ** attempt)
        print(f"Download/API retry in {delay}s: {url} ({last})", flush=True)
        time.sleep(delay)
    raise RuntimeError(f"Could not fetch {url}: {last}")


def api_json(url: str) -> dict:
    return json.loads(request_bytes(url, api=True, timeout=60).decode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_asset(asset: dict, cache: Path) -> Path:
    name = asset.get("name") or ""
    url = asset.get("browser_download_url") or ""
    expected_size = int(asset.get("size") or 0)
    digest = (asset.get("digest") or "").removeprefix("sha256:")
    if not name or not url or expected_size <= 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        fail(f"builder-parts asset has incomplete integrity metadata: {name!r}")
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com" or not parsed.path.startswith(f"/{REPO}/releases/download/"):
        fail(f"Refusing dependency asset outside {REPO}: {url}")
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name
    if target.exists() and target.stat().st_size == expected_size and sha256_file(target) == digest.lower():
        print(f"Verified cached dependency: {name}", flush=True)
        return target
    print(f"Downloading verified dependency: {name} ({expected_size >> 20} MiB)", flush=True)
    payload = request_bytes(url, timeout=900)
    if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != digest.lower():
        fail(f"Dependency integrity check failed: {name}")
    temporary = target.with_suffix(target.suffix + ".new")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return target


def pick_asset(assets: list[dict], prefix: str) -> dict:
    matches = [
        asset for asset in assets
        if (asset.get("name") or "").startswith(prefix) and (asset.get("name") or "").endswith(".zip")
    ]
    if not matches:
        fail(f"Owned builder-parts release has no ZIP asset matching {prefix!r}")
    return sorted(matches, key=lambda item: item.get("created_at") or "", reverse=True)[0]


def patch_text(path: Path, replacements: list[tuple[str, str]], *, required: bool = True) -> str:
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        count = text.count(old)
        if count == 0 and required:
            fail(f"Expected endpoint/config text was not found in {path}: {old}")
        if count:
            text = text.replace(old, new)
    path.write_text(text, encoding="utf-8", newline="\n")
    return text


def patch_source(source: Path, tag: str) -> None:
    owner_repo = REPO
    public_repo = f"https://github.com/{owner_repo}"
    api_repo = f"https://api.github.com/repos/{owner_repo}"

    updater = source / "builder" / "BlueShellBuilder" / "Updater.cs"
    patch_text(updater, [
        (
            "https://git.notrexed.dev/api/v1/repos/BlueShell/BlueShell/releases/latest",
            api_repo + "/releases/latest",
        ),
        (
            "https://git.notrexed.dev/api/v1/repos/BlueShell/BlueShell/releases",
            api_repo + "/releases",
        ),
        (
            "https://git.notrexed.dev/BlueShell/BlueShell",
            public_repo,
        ),
        ("?limit=20", "?per_page=20"),
        ("git.notrexed.dev", "GitHub"),
    ])

    window = source / "builder" / "BlueShellBuilder" / "MainWindow.xaml.cs"
    patch_text(window, [
        (
            "https://git.notrexed.dev/BlueShell/BlueShell/releases/latest",
            public_repo + "/releases/latest",
        ),
        ("git.notrexed.dev", "GitHub"),
    ])

    setup_c = source / "builder" / "setup" / "setup.c"
    patch_text(setup_c, [
        (
            "https://git.notrexed.dev/api/v1/repos/%s/releases/tags/%s",
            "https://api.github.com/repos/%s/releases/tags/%s",
        ),
        (
            "https://git.notrexed.dev/api/v1/repos/%s/releases/latest",
            "https://api.github.com/repos/%s/releases/latest",
        ),
        (
            '!strncmp(out, "https://git.notrexed.dev/", 25)',
            '!strncmp(out, "https://github.com/ZuanCrisp/MarioKartWorld-Port/", sizeof("https://github.com/ZuanCrisp/MarioKartWorld-Port/") - 1)',
        ),
        (
            'const char *close = strchr(at, \'}\'), *url = strstr(at, "\\"browser_download_url\\"");\\n    return url && close && url < close && json_string_after(url + 22, end, out, n) &&\\n           !strncmp(out, "https://github.com/ZuanCrisp/MarioKartWorld-Port/", sizeof("https://github.com/ZuanCrisp/MarioKartWorld-Port/") - 1);',
            'const char *close = strchr(at, \'}\'), *url = strstr(at, "\\"url\\"");\\n    return url && close && url < close && json_string_after(url + 5, end, out, n) &&\\n           !strncmp(out, "https://api.github.com/repos/ZuanCrisp/MarioKartWorld-Port/releases/assets/", sizeof("https://api.github.com/repos/ZuanCrisp/MarioKartWorld-Port/releases/assets/") - 1);',
        ),
        (
            'static const char pre[] = "https://git.notrexed.dev/";',
            'static const char pre[] = "https://github.com/";',
        ),
        (
            '    char api[1024];\n    if (tag) snprintf(api, sizeof api, "https://api.github.com/repos/%s/releases/tags/%s", repo, tag);',
            '    if (strcmp(repo, "ZuanCrisp/MarioKartWorld-Port") != 0) return 0;\n    char api[1024];\n    if (tag) snprintf(api, sizeof api, "https://api.github.com/repos/%s/releases/tags/%s", repo, tag);',
        ),
        (
            "https://git.notrexed.dev/BlueShell/BlueShell/releases/download/%s/hookshot-setup.txt",
            public_repo + "/releases/download/%s/hookshot-setup.txt",
        ),
        (
            "https://git.notrexed.dev/BlueShell/BlueShell/releases/latest/download/hookshot-setup.txt",
            public_repo + "/releases/latest/download/hookshot-setup.txt",
        ),
        (
            "https://git.notrexed.dev/BlueShell/BlueShell/releases/latest",
            public_repo + "/releases/latest",
        ),
        ("git.notrexed.dev", "GitHub"),
    ])

    parts_script = source / "scripts" / "make_setup_parts.py"
    patch_text(parts_script, [
        ('REPO = "BlueShell/BlueShell"', f'REPO = "{owner_repo}"'),
        (
            'https://git.notrexed.dev/%s/releases/download/builder-parts',
            'https://github.com/%s/releases/download/builder-parts',
        ),
    ])

    # Fail closed if a runtime updater/setup path still contains the old host or old project.
    for path in (updater, window, setup_c, parts_script):
        text = path.read_text(encoding="utf-8")
        if "git.notrexed.dev" in text or "NotRexed/BlueShell" in text or "BlueShell/BlueShell" in text:
            fail(f"An upstream distribution endpoint remains in {path}")
    print(f"Patched temporary build source for {tag}; repository source/tag is untouched.", flush=True)


def tracked_files(source: Path, *paths: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *paths],
        cwd=str(source),
        check=True,
        stdout=subprocess.PIPE,
    )
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def copy_tracked(source: Path, destination: Path, *roots: str) -> list[str]:
    copied: list[str] = []
    for relative in tracked_files(source, *roots):
        src = source / Path(relative)
        dst = destination / Path(relative)
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(relative.replace("\\", "/"))
    return copied


def populate_package(
    source: Path,
    package: Path,
    publish: Path,
    setup_exe: Path,
    assets: list[dict],
    cache: Path,
) -> None:
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(publish, package)
    shutil.copy2(setup_exe, package / "BlueShellBuilder-Setup.exe")
    shutil.copy2(source / "LICENSE", package / "LICENSE")

    tools = package / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    tracked = copy_tracked(source, tools, "gpu", "host", "nx2", "scripts", "third_party", "assets/pipeline_states")
    if not tracked:
        fail("No tracked Builder/game runtime source files were copied into the package.")

    # Reuse the known-good play profile from this repository's most recent app part.
    app_asset = pick_asset(assets, "BlueShellBuilder-app-")
    app_zip = download_asset(app_asset, cache)
    work_dest = tools / "work"
    with zipfile.ZipFile(app_zip) as archive:
        prefix = "BlueShellBuilder/tools/work/"
        for member in archive.infolist():
            if member.is_dir() or not member.filename.startswith(prefix):
                continue
            relative = member.filename[len("BlueShellBuilder/tools/"):]
            if not relative or ".." in Path(relative).parts:
                continue
            target = tools / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source_stream, target.open("wb") as output_stream:
                shutil.copyfileobj(source_stream, output_stream)
    if work_dest.exists():
        count = sum(1 for item in work_dest.rglob("*") if item.is_file())
        print(f"Reused {count} profile files from {app_asset['name']}", flush=True)

    # Restore only untracked/prebuilt third_party binaries; tracked source comes from the selected tag.
    third_asset = pick_asset(assets, "BlueShellBuilder-third_party-")
    third_zip = download_asset(third_asset, cache)
    known_third_party = {
        name.removeprefix("third_party/") for name in tracked_files(source, "third_party")
    }
    extracted = 0
    with zipfile.ZipFile(third_zip) as archive:
        prefix = "BlueShellBuilder/tools/third_party/"
        for member in archive.infolist():
            if member.is_dir() or not member.filename.startswith(prefix):
                continue
            relative = member.filename[len(prefix):]
            if not relative or ".." in Path(relative).parts:
                continue
            if relative in known_third_party:
                continue
            if Path(relative).suffix.lower() not in PREBUILT_EXTENSIONS:
                continue
            target = tools / "third_party" / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source_stream, target.open("wb") as output_stream:
                shutil.copyfileobj(source_stream, output_stream)
            extracted += 1
    print(f"Restored {extracted} prebuilt third_party files from {third_asset['name']}", flush=True)


def prepare_reuse_manifest(assets: list[dict], cache: Path) -> Path:
    lines = ["tag builder-parts"]
    for part in TOOLS_TO_REUSE:
        asset = pick_asset(assets, f"BlueShellBuilder-{part}-")
        # Validate the file before carrying its URL/checksum into a new release manifest.
        download_asset(asset, cache)
        digest = asset["digest"].removeprefix("sha256:")
        lines.append(
            f"part {digest} {int(asset['size'])} . - - {asset['browser_download_url']}"
        )
    path = cache / "owned-builder-parts-reuse.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path, help="checkout of the selected source tag")
    parser.add_argument("--tag", required=True, help="upstream-mirrored release tag, e.g. v1.0.1")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    args = parser.parse_args()

    tag = args.tag.strip()
    match = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)", tag)
    if not match:
        fail(f"Only stable semantic version tags are accepted: {tag}")
    version = ".".join(match.groups())
    source = args.source.resolve()
    if not (source / "builder" / "BlueShellBuilder" / "BlueShellBuilder.csproj").is_file():
        fail(f"{tag} does not contain the expected BlueShellBuilder project.")
    if not (source / "builder" / "setup" / "setup.c").is_file():
        fail(f"{tag} does not contain the Windows bootstrapper source.")

    workspace = args.workspace.resolve()
    cache = args.cache.resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    cache.mkdir(parents=True, exist_ok=True)

    # The builder-parts release is our only source for reusable Builder dependencies.
    parts_release = api_json(f"https://api.github.com/repos/{REPO}/releases/tags/{PARTS_TAG}")
    if parts_release.get("draft") or not parts_release.get("prerelease"):
        fail("builder-parts must remain a published prerelease; refusing to use a stable release as a dependency store.")
    assets = parts_release.get("assets") or []
    reuse_manifest = prepare_reuse_manifest(assets, cache)

    patch_source(source, tag)

    # Download and verify the pinned LLVM-MinGW package using the upstream's pinned SHA-256.
    llvm_dir = cache / "llvm-mingw"
    llvm_script = source / "scripts" / "release_parts.py"
    llvm_output = run(
        [sys.executable, "-I", str(llvm_script), "llvm-mingw", str(llvm_dir)],
        cwd=source,
    ).strip().splitlines()
    if not llvm_output:
        fail("The pinned LLVM-MinGW downloader did not return its installation directory.")
    llvm_root = Path(llvm_output[-1].strip())
    windres = llvm_root / "bin" / "x86_64-w64-mingw32-windres.exe"
    clang = llvm_root / "bin" / "x86_64-w64-mingw32-clang.exe"
    if not windres.is_file() or not clang.is_file():
        fail(f"LLVM-MinGW tools are missing under {llvm_root}")

    # The resource version follows the release tag, not the stale 1.0.0 value in the upstream rc file.
    setup_dir = source / "builder" / "setup"
    rc = setup_dir / "setup.rc"
    rc_text = rc.read_text(encoding="utf-8")
    version_commas = ",".join(match.groups()) + ",0"
    rc_text = re.sub(r"(?m)^FILEVERSION\s+[0-9,]+$", f"FILEVERSION {version_commas}", rc_text)
    rc_text = re.sub(r"(?m)^PRODUCTVERSION\s+[0-9,]+$", f"PRODUCTVERSION {version_commas}", rc_text)
    rc_text = re.sub(
        r'(?m)^(\s*VALUE "FileVersion", )"[^"]+"$',
        rf'\g<1>"{version}"',
        rc_text,
    )
    rc.write_text(rc_text, encoding="utf-8", newline="\n")
    resource_obj = workspace / "setup_rc.o"
    setup_exe = workspace / "BlueShellBuilder-Setup.exe"
    run(
        [str(windres), "setup.rc", "-O", "coff", "-o", str(resource_obj)],
        cwd=setup_dir,
    )
    run(
        [
            str(clang), "-O2", "-mwindows", "-municode",
            "setup.c", str(resource_obj),
            "-lwinhttp", "-lbcrypt", "-lcomctl32", "-lshell32", "-lole32",
            "-Wl,--dependent-load-flag=0x800",
            "-o", str(setup_exe),
        ],
        cwd=setup_dir,
    )
    if not setup_exe.is_file() or setup_exe.stat().st_size < 100_000:
        fail("The Windows bootstrapper output is missing or unexpectedly small.")

    publish = workspace / "publish"
    run(
        [
            "dotnet", "publish",
            str(source / "builder" / "BlueShellBuilder" / "BlueShellBuilder.csproj"),
            "--configuration", "Release",
            f"-p:Version={version}",
            "-p:NuGetAudit=false",
            "--output", str(publish),
        ],
        cwd=source,
    )
    if not (publish / "BlueShellBuilder.exe").is_file():
        fail("dotnet publish did not produce BlueShellBuilder.exe.")

    package = workspace / "package"
    populate_package(source, package, publish, setup_exe, assets, cache)

    dist = workspace / "dist"
    dist.mkdir()
    make_parts = source / "scripts" / "make_setup_parts.py"
    base_url = f"https://github.com/{REPO}/releases/download/{PARTS_TAG}"
    run(
        [
            sys.executable, "-I", str(make_parts),
            str(package), tag, str(dist),
            "--base", base_url,
            "--reuse", str(reuse_manifest),
        ],
        cwd=source,
    )

    # The small bootstrapper is the stable-release download; its setup copy is also inside the app part for Repair.
    release_exe = dist / "BlueShellBuilder.exe"
    shutil.copy2(setup_exe, release_exe)
    digest = sha256_file(release_exe)
    (dist / "BlueShellBuilder.exe.sha256").write_text(
        f"{digest}  BlueShellBuilder.exe\n", encoding="ascii", newline="\n"
    )

    manifest = dist / "hookshot-setup.txt"
    if not manifest.is_file():
        fail("make_setup_parts.py did not create hookshot-setup.txt.")
    manifest_text = manifest.read_text(encoding="utf-8")
    if not manifest_text.startswith(f"tag {tag}\n"):
        fail("Generated setup manifest does not name the requested release tag.")
    if "git.notrexed.dev" in manifest_text or "NotRexed/BlueShell" in manifest_text or "BlueShell/BlueShell" in manifest_text:
        fail("Generated setup manifest still contains an upstream dependency URL.")
    for line in manifest_text.splitlines():
        if not line.startswith("part "):
            continue
        fields = line.split()
        if len(fields) != 7:
            fail(f"Malformed manifest part line: {line}")
        url = fields[6]
        if not (
            url.startswith(f"https://github.com/{REPO}/releases/download/{PARTS_TAG}/")
            or url.startswith("https://github.com/mstorsjo/llvm-mingw/")
        ):
            fail(f"Manifest points outside the owned dependency store or pinned LLVM source: {url}")

    # Ensure every newly generated content-addressed ZIP is nonempty and has a SHA-256.
    new_parts = sorted(dist.glob("BlueShellBuilder-*.zip"))
    if not any("-app-" in item.name for item in new_parts):
        fail("The rebuilt app part is missing.")
    if not any("-dotnet-" in item.name for item in new_parts):
        fail("The rebuilt .NET runtime part is missing.")
    if not any("-third_party-" in item.name for item in new_parts):
        fail("The rebuilt third_party part is missing.")
    for item in new_parts:
        if not item.is_file() or item.stat().st_size == 0:
            fail(f"Generated part is empty: {item.name}")
        print(f"Prepared part: {item.name} ({item.stat().st_size} bytes; SHA-256 {sha256_file(item)})", flush=True)

    # Publish dependency parts append-only: never clobber an existing builder-parts asset.
    gh_env = dict(os.environ)
    gh_env["GH_TOKEN"] = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    gh = shutil.which("gh")
    if not gh:
        fail("GitHub CLI (gh) is not available on this runner.")

    current_parts = api_json(f"https://api.github.com/repos/{REPO}/releases/tags/{PARTS_TAG}")
    existing_parts = {asset["name"]: asset for asset in current_parts.get("assets", [])}
    upload_parts: list[Path] = []
    for item in new_parts:
        current = existing_parts.get(item.name)
        if current is None:
            upload_parts.append(item)
            continue
        local_digest = sha256_file(item)
        remote_digest = (current.get("digest") or "").removeprefix("sha256:").lower()
        if int(current.get("size") or -1) != item.stat().st_size or remote_digest != local_digest:
            fail(f"Refusing to overwrite existing builder-parts asset with different bytes: {item.name}")
        print(f"Already present with identical SHA-256; keeping existing part asset: {item.name}", flush=True)

    if upload_parts:
        run(
            [gh, "release", "upload", PARTS_TAG, *[str(item) for item in upload_parts], "--repo", REPO],
            env=gh_env,
        )
    else:
        print("All generated dependency parts already exist unchanged; builder-parts was not modified.", flush=True)

    # Confirm each manifest dependency now exists on our own builder-parts release.
    published_parts = api_json(f"https://api.github.com/repos/{REPO}/releases/tags/{PARTS_TAG}")
    published_by_name = {asset["name"]: asset for asset in published_parts.get("assets", [])}
    for line in manifest_text.splitlines():
        if not line.startswith("part "):
            continue
        asset_name = line.split()[6].rsplit("/", 1)[-1]
        if asset_name.startswith("BlueShellBuilder-") and asset_name.endswith(".zip"):
            asset = published_by_name.get(asset_name)
            if asset is None:
                fail(f"Manifest dependency is not published on builder-parts: {asset_name}")
            expected = line.split()[1]
            actual = (asset.get("digest") or "").removeprefix("sha256:").lower()
            if actual != expected.lower():
                fail(f"Published dependency checksum differs from manifest: {asset_name}")

    # Verify the target stable release exists before replacing exactly the agreed versioned assets.
    target_release = api_json(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
    if target_release.get("draft") or target_release.get("prerelease") or target_release.get("tag_name") != tag:
        fail(f"Refusing to upload Builder assets to a non-stable or mismatched release: {tag}")
    run(
        [
            gh, "release", "upload", tag,
            str(release_exe),
            str(dist / "BlueShellBuilder.exe.sha256"),
            str(manifest),
            "--repo", REPO, "--clobber",
        ],
        env=gh_env,
    )

    print(f"SUCCESS: {tag} rebuilt and published to {REPO}.", flush=True)
    print(f"  Bootstrapper SHA-256: {digest}", flush=True)
    print(f"  Manifest: {manifest}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        raise
