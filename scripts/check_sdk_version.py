# check_sdk_version.py
# Makes no AWS calls. Decides from the local service model alone whether the installed
# boto3 / botocore supports platformVersion. Run this first.
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import boto3
import botocore

from common import MIN_BOTO3, platform_version_supported, save_result


def version_tuple(version_str):
    """Compare versions as tuples of ints. String comparison would read "1.43.9" as newer
    than "1.43.95".

    Digits are extracted rather than split on "." so that release candidates and dev builds
    ("1.44.0rc1", "1.43.96.dev0") do not make int() raise ValueError. This is the first
    script a reader runs; it must not fail on the shape of a version string.
    """
    return tuple(int(m) for m in re.findall(r"\d+", version_str))


def main():
    minimum = version_tuple(MIN_BOTO3)
    boto3_ok = version_tuple(boto3.__version__) >= minimum
    botocore_ok = version_tuple(botocore.__version__) >= minimum

    try:
        model = platform_version_supported()
        error = None
    except Exception as exc:  # noqa: BLE001
        model = {"create_input": False, "update_input": False, "get_output": False}
        error = repr(exc)

    supported = error is None and all(model.values())

    print(f"boto3    : {boto3.__version__} (>= {MIN_BOTO3}: {boto3_ok})", flush=True)
    print(f"botocore : {botocore.__version__} (>= {MIN_BOTO3}: {botocore_ok})", flush=True)
    print("service model:", flush=True)
    for key, value in model.items():
        print(f"    platformVersion in {key} = {value}", flush=True)
    if error:
        print(f"    error: {error}", flush=True)
    print(f"platform_version_supported = {supported}", flush=True)

    save_result(
        "sdk_version_check.json",
        {
            "boto3_version": boto3.__version__,
            "botocore_version": botocore.__version__,
            "min_required": MIN_BOTO3,
            "service_model": model,
            "error": error,
            "platform_version_supported": supported,
        },
    )

    if not supported:
        print(
            f"\nRun pip install --upgrade 'boto3>={MIN_BOTO3}' and check again.",
            flush=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
