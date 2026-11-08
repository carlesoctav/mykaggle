from pathlib import Path
import re
import subprocess
import tempfile
from typing import Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import typer
from rich.console import Console
from typing_extensions import Annotated

_console = Console()

uv_app = typer.Typer(
    help="UV and wheel building tools for Kaggle environments.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

CORE_GPU_PINS = {"torch", "torchaudio", "torchvision", "triton", "jax", "jaxlib"}


def load_kaggle_base_packages() -> set[str]:
    base_file = Path(__file__).resolve().parent.parent / "kaggle_base_packages.txt"
    if not base_file.is_file():
        return set()
    packages = set()
    for line in base_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pkg = line.split("==")[0].strip().lower().replace("_", "-")
            packages.add(pkg)
    return packages


def load_kaggle_constraints() -> dict[str, str]:
    base_file = Path(__file__).resolve().parent.parent / "kaggle_base_packages.txt"
    if not base_file.is_file():
        return {}
    constraints = {}
    for line in base_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pkg = line.split("==")[0].strip().lower().replace("_", "-")
            constraints[pkg] = line
    return constraints


def is_non_linux_platform_marker(line: str) -> bool:
    if ";" not in line:
        return False
    marker = line.split(";", 1)[1]
    non_linux_targets = [
        "'win32'",
        '"win32"',
        "'darwin'",
        '"darwin"',
        "'emscripten'",
        '"emscripten"',
    ]
    has_non_linux = any(
        f"sys_platform == {t}" in marker or f"os_name == {t}" in marker
        for t in non_linux_targets
    )
    has_linux = "'linux'" in marker or '"linux"' in marker
    return has_non_linux and not has_linux


def is_path_or_url(line: str) -> bool:
    if line == ".":
        return True
    if line.startswith((".", "/", "file://", "http://", "https://", "git+")):
        return True
    return False


def extract_package_name(line: str) -> str:
    spec = line.split(";", 1)[0].strip()
    tokens = re.split(r"[=><~\[;\s@]", spec)
    return tokens[0].strip().lower().replace("_", "-")


def resolve_project_and_wheel_dir(
    path: Optional[str], out_dir: Optional[str]
) -> tuple[Path, Path]:
    if path is None:
        project_dir = Path.cwd()
        wheel_dir = (
            Path(out_dir).resolve()
            if out_dir
            else (project_dir / "kaggle_wheelhouse")
        )
    else:
        p = Path(path).resolve()
        if p.is_file() and p.name == "pyproject.toml":
            project_dir = p.parent
            wheel_dir = (
                Path(out_dir).resolve()
                if out_dir
                else (project_dir / "kaggle_wheelhouse")
            )
        elif p.is_dir() and (p / "pyproject.toml").is_file():
            project_dir = p
            wheel_dir = (
                Path(out_dir).resolve()
                if out_dir
                else (project_dir / "kaggle_wheelhouse")
            )
        else:
            project_dir = Path.cwd()
            wheel_dir = Path(out_dir).resolve() if out_dir else p

    if not (project_dir / "pyproject.toml").is_file():
        typer.echo(
            f"Error: pyproject.toml not found in {project_dir}", err=True
        )
        raise typer.Exit(1)

    return project_dir, wheel_dir


def run_pip_wheel(
    lines: list[str],
    project_dir: Path,
    wheel_dir: Path,
    python_version: str,
) -> None:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    wheel_cmd = [
        "uv",
        "run",
        "--python",
        python_version,
        "--with",
        "pip",
        "pip",
        "wheel",
        "--no-deps",
        "-w",
        str(wheel_dir.resolve()),
        "-r",
        "/dev/stdin",
    ]
    input_text = "\n".join(lines) + ("\n" if lines else "")
    run_res = subprocess.run(
        wheel_cmd,
        cwd=str(project_dir),
        input=input_text,
        text=True,
    )
    if run_res.returncode != 0:
        raise typer.Exit(code=run_res.returncode)


def make_wheel_command(
    path: Annotated[
        Optional[str],
        typer.Argument(
            help="Project directory (containing pyproject.toml) or output wheelhouse directory. Defaults to '.'"
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Only print the resolved requirements list that would be built into wheels without downloading/building",
        ),
    ] = False,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Export and build all dependencies without filtering out Kaggle pre-installed packages",
        ),
    ] = False,
    out_dir: Annotated[
        Optional[str],
        typer.Option(
            "-o",
            "--out-dir",
            "--output",
            help="Output directory for built wheels (defaults to <project>/kaggle_wheelhouse)",
        ),
    ] = None,
    python_version: Annotated[
        str,
        typer.Option(
            "--python",
            help="Target Python version for pip wheel (defaults to 3.13 to match Kaggle VM)",
        ),
    ] = "3.13",
):
    """Build wheels for project dependencies for Kaggle offline environments using uv export."""
    project_dir, wheel_dir = resolve_project_and_wheel_dir(path, out_dir)

    export_cmd = [
        "uv",
        "export",
        "--no-hashes",
        "--no-editable",
        "--no-annotate",
        "--no-header",
    ]
    try:
        res = subprocess.run(
            export_cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        typer.echo(
            f"uv export failed (exit code {e.returncode}):\n{e.stderr}",
            err=True,
        )
        raise typer.Exit(e.returncode)
    except FileNotFoundError:
        typer.echo("Error: 'uv' executable not found in PATH.", err=True)
        raise typer.Exit(1)

    kaggle_base_packages = load_kaggle_base_packages()

    filtered_lines: list[str] = []
    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if is_non_linux_platform_marker(line):
            continue
        if is_path_or_url(line):
            filtered_lines.append(line)
            continue
        pkg_name = extract_package_name(line)
        if not full and pkg_name in kaggle_base_packages:
            continue
        filtered_lines.append(line)

    if dry_run:
        for item in filtered_lines:
            typer.echo(item)
        raise typer.Exit(0)

    run_pip_wheel(filtered_lines, project_dir, wheel_dir, python_version)


def make_wheel_strict_command(
    path: Annotated[
        Optional[str],
        typer.Argument(
            help="Project directory (containing pyproject.toml) or output wheelhouse directory. Defaults to '.'"
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Only print the resolved requirements list that would be built into wheels without downloading/building",
        ),
    ] = False,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Build all resolved dependencies without filtering out Kaggle pre-installed packages",
        ),
    ] = False,
    out_dir: Annotated[
        Optional[str],
        typer.Option(
            "-o",
            "--out-dir",
            "--output",
            help="Output directory for built wheels (defaults to <project>/kaggle_wheelhouse)",
        ),
    ] = None,
    python_version: Annotated[
        str,
        typer.Option(
            "--python",
            help="Target Python version for pip wheel (defaults to 3.13 to match Kaggle VM)",
        ),
    ] = "3.13",
):
    """Build wheels for project dependencies resolved strictly against Kaggle pre-installed version constraints."""
    project_dir, wheel_dir = resolve_project_and_wheel_dir(path, out_dir)

    pyproject_file = project_dir / "pyproject.toml"
    try:
        pyproject_data = tomllib.loads(pyproject_file.read_text(encoding="utf-8"))
    except Exception as e:
        typer.echo(f"Error reading {pyproject_file}: {e}", err=True)
        raise typer.Exit(1)

    has_dev_group = "dev" in pyproject_data.get("dependency-groups", {})

    local_sources: dict[str, str] = {}
    uv_sources = pyproject_data.get("tool", {}).get("uv", {}).get("sources", {})
    if isinstance(uv_sources, dict):
        for pkg, conf in uv_sources.items():
            if isinstance(conf, dict) and "path" in conf:
                p = Path(conf["path"])
                if not p.is_absolute():
                    p = (project_dir / p).resolve()
                local_sources[pkg.lower().replace("_", "-")] = str(p)

    compile_cmd = [
        "uv",
        "pip",
        "compile",
        "pyproject.toml",
    ]
    if has_dev_group:
        compile_cmd.extend(["--group", "dev"])
    compile_cmd.extend([
        "--no-annotate",
        "--no-header",
        "--python-version",
        python_version,
        "--python-platform",
        "x86_64-unknown-linux-gnu",
    ])

    constraints = load_kaggle_constraints()
    excluded: set[str] = set()

    resolved_stdout: Optional[str] = None
    for _ in range(25):
        active_constraints = [
            v for k, v in constraints.items() if k not in excluded
        ]
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write("\n".join(active_constraints))
            tmp_constraints_path = f.name
        try:
            cmd = compile_cmd + ["-c", tmp_constraints_path]
            res = subprocess.run(
                cmd,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            typer.echo("Error: 'uv' executable not found in PATH.", err=True)
            raise typer.Exit(1)
        finally:
            Path(tmp_constraints_path).unlink(missing_ok=True)

        if res.returncode == 0:
            resolved_stdout = res.stdout
            break

        stderr_norm = res.stderr.lower().replace("_", "-")
        found = set()
        for k, v in constraints.items():
            if k in CORE_GPU_PINS or k in excluded:
                continue
            v_norm = v.lower().replace("_", "-")
            if v_norm in stderr_norm:
                found.add(k)
            elif re.search(rf"(?<![a-z0-9\-]){re.escape(k)}(?![a-z0-9\-])", stderr_norm):
                found.add(k)

        if not found:
            break

        excluded.update(found)

    if resolved_stdout is None:
        active_constraints = [
            constraints[k] for k in CORE_GPU_PINS if k in constraints
        ]
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write("\n".join(active_constraints))
            tmp_constraints_path = f.name
        try:
            cmd = compile_cmd + ["-c", tmp_constraints_path]
            res = subprocess.run(
                cmd,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                resolved_stdout = res.stdout
                excluded.update(k for k in constraints if k not in CORE_GPU_PINS)
            else:
                typer.echo(
                    f"uv pip compile failed (exit code {res.returncode}):\n{res.stderr}",
                    err=True,
                )
                raise typer.Exit(res.returncode)
        finally:
            Path(tmp_constraints_path).unlink(missing_ok=True)

    filtered_lines: list[str] = ["."]
    seen: set[str] = {"."}

    for raw_line in resolved_stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if is_non_linux_platform_marker(line):
            continue

        if is_path_or_url(line):
            if line.startswith("file://"):
                line = line[len("file://"):]
            if line not in seen:
                seen.add(line)
                filtered_lines.append(line)
            continue

        line_no_marker = line.split(";", 1)[0].strip()
        pkg_name = extract_package_name(line_no_marker)
        pkg_norm = pkg_name.lower().replace("_", "-")

        if pkg_norm in local_sources:
            local_path = local_sources[pkg_norm]
            if local_path not in seen:
                seen.add(local_path)
                filtered_lines.append(local_path)
            continue

        if not full:
            if pkg_norm in constraints and pkg_norm not in excluded:
                req_ver = line_no_marker.split("==")[1].strip() if "==" in line_no_marker else None
                constraint_ver = constraints[pkg_norm].split("==")[1].strip() if "==" in constraints[pkg_norm] else None
                if req_ver and constraint_ver and req_ver == constraint_ver:
                    continue

        if line not in seen:
            seen.add(line)
            filtered_lines.append(line)

    if dry_run:
        for item in filtered_lines:
            typer.echo(item)
        raise typer.Exit(0)

    run_pip_wheel(filtered_lines, project_dir, wheel_dir, python_version)


uv_app.command(name="make-wheel")(make_wheel_command)
uv_app.command(name="make-wheel-strict")(make_wheel_strict_command)
