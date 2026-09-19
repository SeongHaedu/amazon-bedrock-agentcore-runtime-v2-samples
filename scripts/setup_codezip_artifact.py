# setup_codezip_artifact.py
# 直接コードデプロイ (codezip) 用の zip を作り、S3 にアップロードする。
# Docker を使わずに V2 を試したい場合はこちらを使う。
#
# 依存関係は ARM64 (aarch64) 向けにベンダリングする必要がある。AgentCore Runtime の実行環境は
# arm64 の Linux であり、ローカルが x86_64 や macOS でもホスト向けの wheel では動かないためである。
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

# AgentCore Runtime の実行環境に合わせる。CODE_RUNTIME が PYTHON_3_11 なので 3.11 を指定する。
PLATFORM = "manylinux2014_aarch64"
PYTHON_VERSION = "3.11"
# 依存は対象ディレクトリの requirements.txt から読む。Dockerfile も同じファイルを参照するため、
# コンテナと ZIP で依存がずれない。ファイルが無い場合のフォールバックだけここで持つ。
FALLBACK_DEPENDENCIES = ["bedrock-agentcore"]

USAGE = "usage: python scripts/setup_codezip_artifact.py <agent-dir>"


def resolve_dependencies(agent_dir):
    req = agent_dir / "requirements.txt"
    if not req.exists():
        print(f"{req} が無いため既定の依存を使う: {FALLBACK_DEPENDENCIES}", flush=True)
        return FALLBACK_DEPENDENCIES
    deps = [
        line.strip()
        for line in req.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not deps:
        raise SystemExit(f"{req} に依存が 1 件も書かれていない。")
    return deps


def vendor_dependencies(dependencies):
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True)

    # --only-binary=:all: を付けるのは、ソース配布からのビルドがローカルのアーキテクチャ向けに
    # なってしまうのを防ぐためである。wheel が無い依存があればここで失敗し、気付ける。
    #
    # --no-compile を付けるのは、ローカルでコンパイルしたバイトコードを作らせないためである。
    # 開発機と実行環境でアーキテクチャや OS が異なると互換性が無い。公開ドキュメントも
    # __pycache__ をデプロイパッケージに含めないことを推奨している。
    # 念のため build_zip() 側でも除外する。
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
        raise SystemExit(f"{entry} が存在しない。{USAGE}")
    shutil.copy2(entry, BUILD_DIR / "main.py")

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(BUILD_DIR.rglob("*")):
            if not path.is_file():
                continue
            # __pycache__ と .pyc は除外する。pip install --target がローカルの
            # Python でコンパイルしたバイトコードであり、実行環境と Python の
            # バージョンやアーキテクチャが異なると使えない。zip も無駄に膨らむ。
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            # zip のルート直下に main.py と依存が並ぶ形にする。entryPoint は
            # ["main.py"] であり、zip 内のパスと一致していなければ起動に失敗する。
            zf.write(path, path.relative_to(BUILD_DIR))
    return ZIP_PATH.stat().st_size


def main():
    if len(sys.argv) < 2:
        raise SystemExit(USAGE)
    agent_dir = REPO_ROOT / sys.argv[1]
    if not agent_dir.is_dir():
        raise SystemExit(f"{agent_dir} がディレクトリではない。{USAGE}")

    require_env(
        "AGENTCORE_S3_BUCKET",
        S3_BUCKET,
        "zip のアップロード先に使う既存の S3 バケット名を設定する。",
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
