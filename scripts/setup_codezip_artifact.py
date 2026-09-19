# setup_codezip_artifact.py
# Build the ZIP for direct code deployment (codezip) and upload it to S3. Use this to try V2
# without Docker.
#
# Dependencies must be vendored for ARM64 (aarch64): AgentCore Runtime executes on arm64
# Linux, so host wheels built for x86_64 or macOS will not run there.
#
# usage:
#   python scripts/setup_codezip_artifact.py <agent-dir>
#
# example:
#   python scripts/setup_codezip_artifact.py agent-basic
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import S3_BUCKET, S3_PREFIX, make_client, require_env, save_result

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO_ROOT / "build" / "codezip"
ZIP_PATH = REPO_ROOT / "build" / "agent-codezip.zip"

# Match the AgentCore Runtime execution environment. CODE_RUNTIME is PYTHON_3_11, so 3.11.
PLATFORM = "manylinux2014_aarch64"
PYTHON_VERSION = "3.11"
# Dependencies come from the target directory's requirements.txt, the same file the
# Dockerfile reads, so the container and the ZIP cannot drift apart. The fallback below only
# covers a directory without that file.
FALLBACK_DEPENDENCIES = ["bedrock-agentcore"]

USAGE = "usage: python scripts/setup_codezip_artifact.py <agent-dir>"


def resolve_dependencies(agent_dir):
    req = agent_dir / "requirements.txt"
    if not req.exists():
        print(f"{req} is missing; using the default dependencies: {FALLBACK_DEPENDENCIES}", flush=True)
        return FALLBACK_DEPENDENCIES
    deps = [
        line.strip()
        for line in req.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not deps:
        raise SystemExit(f"{req} lists no dependencies.")
    return deps


def vendor_dependencies(dependencies):
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True)

    # --only-binary=:all: keeps a source distribution from being built for the local
    # architecture. A dependency without a wheel fails here, where it is visible.
    #
    # --no-compile keeps locally compiled bytecode out of the archive: it is incompatible
    # when the development machine differs from the execution environment in architecture or
    # OS. The public documentation also recommends excluding __pycache__ from the deployment
    # package. build_zip() filters it again for good measure.
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--platform",
        PLATFORM,
        "--python-version",
        PYTHON_VERSION,
        "--only-binary=:all:",
        "--no-compile",
        "--target",
        str(BUILD_DIR),
        *dependencies,
    ]
    print(" ".join(command), flush=True)
    subprocess.run(command, check=True)


def build_zip(agent_dir):
    entry = agent_dir / "main.py"
    if not entry.exists():
        raise SystemExit(f"{entry} does not exist. {USAGE}")
    shutil.copy2(entry, BUILD_DIR / "main.py")

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(BUILD_DIR.rglob("*")):
            if not path.is_file():
                continue
            # Exclude __pycache__ and .pyc: pip install --target compiles them with the local
            # Python, and they are unusable when the execution environment differs in Python
            # version or architecture. They also inflate the archive.
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            # main.py and the dependencies sit at the root of the archive. entryPoint is
            # ["main.py"] and must match the path inside the ZIP or startup fails.
            zf.write(path, path.relative_to(BUILD_DIR))
    return ZIP_PATH.stat().st_size


def main():
    if len(sys.argv) < 2:
        raise SystemExit(USAGE)
    agent_dir = REPO_ROOT / sys.argv[1]
    if not agent_dir.is_dir():
        raise SystemExit(f"{agent_dir} is not a directory. {USAGE}")

    require_env(
        "AGENTCORE_S3_BUCKET",
        S3_BUCKET,
        "Set the name of an existing S3 bucket to upload the ZIP to.",
    )

    dependencies = resolve_dependencies(agent_dir)
    vendor_dependencies(dependencies)
    size = build_zip(agent_dir)
    print(f"built: {ZIP_PATH} ({size} bytes)", flush=True)

    s3 = make_client("s3")
    s3.upload_file(str(ZIP_PATH), S3_BUCKET, S3_PREFIX)
    print(f"uploaded: s3://{S3_BUCKET}/{S3_PREFIX}", flush=True)

    save_result(
        "setup_codezip_artifact.json",
        {
            "agent_dir": sys.argv[1],
            "zip_path": str(ZIP_PATH),
            "zip_bytes": size,
            "s3_bucket": S3_BUCKET,
            "s3_prefix": S3_PREFIX,
            "platform": PLATFORM,
            "python_version": PYTHON_VERSION,
            "dependencies": dependencies,
        },
    )


if __name__ == "__main__":
    main()
