"""Build and inspect release archives and exercise the installed console command."""

import argparse
import ast
import configparser
import email.parser
import importlib.util
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from antigravity_local_mcp import __version__

PACKAGE = "antigravity_local_mcp"
PROJECT = "antigravity-local-mcp"
INIT = PACKAGE + "/__init__.py"
EXCLUDED = {".handoff", ".tmp", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            ".cache", ".tox", ".nox", ".venv", ".git", ".aws", ".gemini", ".ssh"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_member(name):
    """Check the original components before a path library can normalize them."""
    name = name.removesuffix("/")
    parts = name.split("/")
    require(not any(p in ("", ".", "..") or ":" in p for p in parts) and "\\" not in name
            and not PureWindowsPath(name).drive, f"Unsafe archive path {name!r}")
    path = PurePosixPath(name)
    require(not EXCLUDED.intersection(p.lower() for p in parts)
            and path.suffix.lower() not in {".pyc", ".pyo"}, f"Excluded archive member {name!r}")
    return path


def module_version(source):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            version = ast.literal_eval(node.value)
            require(isinstance(version, str), "Invalid package version")
            return version
    raise ValueError("Missing package __version__")


def check_project(data, version):
    project = tomllib.loads(data.decode("utf-8"))["project"]
    require(project["name"] == PROJECT and project["version"] == version, "Project name/version mismatch")
    require(not project.get("dependencies"), "Unexpected runtime dependencies")
    require(project["scripts"].get(PROJECT) == PACKAGE + ".cli:main", "Invalid console entry point")


def check_metadata(data, version):
    metadata = email.parser.BytesParser().parsebytes(data, headersonly=True)
    require(metadata.get_all("Name") == [PROJECT] and metadata.get_all("Version") == [version],
            "Distribution metadata name/version mismatch")
    require(not metadata.get_all("Requires-Dist"), "Unexpected runtime dependencies in metadata")


def expected_modules(root=ROOT):
    return {p.relative_to(root).as_posix() for p in (root / PACKAGE).rglob("*.py")}


def release_version(root=ROOT):
    check_project((root / "pyproject.toml").read_bytes(), __version__)
    require(module_version((root / INIT).read_bytes()) == __version__, "Source package version mismatch")
    return __version__


def _small(stream):
    with stream:
        data = stream.read((1 << 20) + 1)
    require(len(data) <= 1 << 20, "Oversized package metadata")
    return data


def inspect_wheel(path, version, modules=None):
    modules = expected_modules() if modules is None else modules
    dist = f"{PACKAGE}-{version}.dist-info"
    metadata_files = {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt",
                      "licenses/LICENSE", "LICENSE", "RECORD.jws", "RECORD.p7s"}
    allowed = set(modules) | {f"{dist}/{name}" for name in metadata_files} | {"LICENSE"}
    directories = {str(p) for name in allowed for p in PurePosixPath(name).parents}
    files, seen = set(), set()
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            member = safe_member(item.filename)
            name = str(member)
            require(name.casefold() not in seen, "Duplicate wheel member " + name)
            seen.add(name.casefold())
            kind = stat.S_IFMT(item.external_attr >> 16)
            require(kind in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG), "Special wheel member " + name)
            require(name in (directories if item.is_dir() else allowed) and "tests" not in member.parts,
                    "Unexpected wheel member " + name)
            if not item.is_dir():
                files.add(name)
        required = set(modules) | {f"{dist}/{name}" for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt")}
        require(required <= files, "Missing wheel files: " + ", ".join(sorted(required - files)))
        require(any(PurePosixPath(name).name == "LICENSE" for name in files), "Wheel LICENSE missing")
        check_metadata(_small(archive.open(f"{dist}/METADATA")), version)
        require(module_version(_small(archive.open(INIT))) == version, "Wheel package version mismatch")
        entries = configparser.ConfigParser(interpolation=None)
        entries.read_string(_small(archive.open(f"{dist}/entry_points.txt")).decode())
        require(entries.get("console_scripts", PROJECT, fallback=None) == PACKAGE + ".cli:main",
                "Wheel console entry point mismatch")


def inspect_sdist(path, version, modules=None):
    modules = expected_modules() if modules is None else modules
    seen, files, roots = set(), set(), set()
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            member = safe_member(item.name)
            require(item.isfile() or item.isdir(), "Special sdist member " + item.name)
            require(str(member).casefold() not in seen, "Duplicate sdist path " + item.name)
            seen.add(str(member).casefold())
            roots.add(member.parts[0])
            if item.isfile():
                files.add("/".join(member.parts[1:]))
        require(len(roots) == 1, "Expected one sdist root")
        root = roots.pop()
        require(root in {f"{PACKAGE}-{version}", f"{PROJECT}-{version}"}, "Sdist root name/version mismatch")
        prefix = root + "/"
        required = set(modules) | {"pyproject.toml", "PKG-INFO", "LICENSE", "README.md"}
        require(required <= files, "Missing sdist files: " + ", ".join(sorted(required - files)))
        check_metadata(_small(archive.extractfile(prefix + "PKG-INFO")), version)
        check_project(_small(archive.extractfile(prefix + "pyproject.toml")), version)
        require(module_version(_small(archive.extractfile(prefix + INIT))) == version, "Sdist package version mismatch")


def check_install(wheel, directory, env, version):
    target = directory / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(target)], env=env, cwd=directory, check=True, timeout=120)
    if sys.platform == "win32":
        python, command = target / "Scripts/python.exe", target / ("Scripts/" + PROJECT + ".exe")
    else:
        python, command = target / "bin/python", target / ("bin/" + PROJECT)
    subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
                   cwd=directory, env=env, check=True, timeout=120)
    result = subprocess.run([str(command), "--version"], cwd=directory, env=env, check=True,
                            capture_output=True, text=True, timeout=15)
    require(result.stdout.strip() == version, "Installed console reported a different version")


def check(*, require_build=False, require_install=False):
    release_version()
    if importlib.util.find_spec("build") is None:
        print(("FAIL" if require_build or require_install else "SKIP") + " build: install the build frontend")
        return int(require_build or require_install)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}
    env.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    with tempfile.TemporaryDirectory(prefix="agy-release-", dir=os.environ.get("AGY_MCP_TEST_TMP")) as tmp:
        directory = Path(tmp).resolve()
        dist = directory / "dist"
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist), str(ROOT)],
                       cwd=directory, env=env, check=True, timeout=180)
        wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
        require(len(wheels) == len(sdists) == 1 and len(list(dist.iterdir())) == 2, "Expected one wheel and one sdist")
        require(wheels[0].name.startswith(f"{PACKAGE}-{__version__}-") and
                sdists[0].name in {f"{n}-{__version__}.tar.gz" for n in (PACKAGE, PROJECT)},
                "Distribution filename name/version mismatch")
        modules = expected_modules(ROOT)
        inspect_wheel(wheels[0], __version__, modules)
        print("PASS wheel", wheels[0].name)
        inspect_sdist(sdists[0], __version__, modules)
        print("PASS sdist", sdists[0].name)
        if require_install:
            check_install(wheels[0], directory, env, __version__)
            print("PASS installed console", __version__)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-build", action="store_true")
    parser.add_argument("--require-install", action="store_true")
    args = parser.parse_args(argv)
    try:
        return check(require_build=args.require_build, require_install=args.require_install)
    except (OSError, ValueError, SyntaxError, KeyError, configparser.Error, tarfile.TarError,
            zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        print("FAIL release check:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
