# cleanup.py
# Delete what the samples created: the agent runtimes whose name starts with
# AGENTCORE_NAME_PREFIX (default "v2sample_"), and the prerequisite resources that
# scripts/setup_prerequisites.py created.
#
# Safeguards:
#   1. The minimum prefix length is checked at runtime. An empty prefix matches every runtime,
#      which would make the other checks meaningless.
#   2. The runtime prefix is matched both when listing and inside the loop.
#   3. A prerequisite resource is deleted only when it carries the ManagedBy tag that
#      setup_prerequisites.py applies, so a role, repository or bucket that already existed in
#      the account is left alone.
#   4. Without --yes the script prints the targets and exits without deleting.
#   5. Runtime deletion is complete on ResourceNotFoundException or DELETE_FAILED, not just the
#      former.
#
# usage:
#   python scripts/cleanup.py                       # print the targets only
#   python scripts/cleanup.py --yes                 # delete runtimes and prerequisites
#   python scripts/cleanup.py --yes --keep-resources # delete runtimes only
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from botocore.exceptions import ClientError

from common import (
    MANAGED_TAG_KEY,
    MANAGED_TAG_VALUE,
    MIN_NAME_PREFIX_LEN,
    NAME_PREFIX,
    SETUP_ECR_REPOSITORY,
    SETUP_ROLE_NAME,
    SETUP_ROLE_POLICY_NAME,
    list_sample_runtimes,
    make_client,
    save_result,
    setup_bucket_name,
    wait_until_deleted,
)

USAGE = "usage: python scripts/cleanup.py [--yes] [--keep-resources]"


def managed(tags):
    """tags is a list of {Key, Value} in any of the three services' shapes."""
    return any(t.get("Key") == MANAGED_TAG_KEY and t.get("Value") == MANAGED_TAG_VALUE for t in tags)


def role_state(iam):
    try:
        iam.get_role(RoleName=SETUP_ROLE_NAME)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            return None
        raise
    tags = iam.list_role_tags(RoleName=SETUP_ROLE_NAME)["Tags"]
    return {"name": SETUP_ROLE_NAME, "managed": managed(tags)}


def repository_state(ecr):
    try:
        repo = ecr.describe_repositories(repositoryNames=[SETUP_ECR_REPOSITORY])["repositories"][0]
    except ClientError as error:
        if error.response["Error"]["Code"] == "RepositoryNotFoundException":
            return None
        raise
    tags = ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"])["tags"]
    return {"name": SETUP_ECR_REPOSITORY, "managed": managed(tags)}


def bucket_state(s3, bucket):
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchBucket"):
            return None
        raise
    try:
        tags = s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchTagSet":
            raise
        tags = []
    return {"name": bucket, "managed": managed(tags)}


def delete_role(iam):
    iam.delete_role_policy(RoleName=SETUP_ROLE_NAME, PolicyName=SETUP_ROLE_POLICY_NAME)
    iam.delete_role(RoleName=SETUP_ROLE_NAME)


def delete_repository(ecr):
    # force=True also removes the images that were pushed into it.
    ecr.delete_repository(repositoryName=SETUP_ECR_REPOSITORY, force=True)


def delete_bucket(s3, bucket):
    """Empty the bucket, then delete it. Object versions are handled as well, so a bucket with
    versioning enabled does not leave the delete call failing on a non-empty bucket."""
    versions = s3.get_paginator("list_object_versions")
    for page in versions.paginate(Bucket=bucket):
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for key in ("Versions", "DeleteMarkers")
            for item in page.get(key, [])
        ]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
    s3.delete_bucket(Bucket=bucket)


def main():
    for arg in sys.argv[1:]:
        if arg not in ("--yes", "--keep-resources"):
            raise SystemExit(f"unknown argument: {arg}\n{USAGE}")
    apply_delete = "--yes" in sys.argv[1:]
    keep_resources = "--keep-resources" in sys.argv[1:]

    # An empty or very short prefix would match runtimes that were never meant to be touched.
    # Fail before calling AWS.
    if len(NAME_PREFIX) < MIN_NAME_PREFIX_LEN:
        raise SystemExit(
            f"AGENTCORE_NAME_PREFIX is too short ({NAME_PREFIX!r})."
            f" At least {MIN_NAME_PREFIX_LEN} characters are required to prevent accidental deletion."
        )

    control = make_client("bedrock-agentcore-control")
    runtimes = list_sample_runtimes(control)

    iam, ecr, s3 = make_client("iam"), make_client("ecr"), make_client("s3")
    account_id = make_client("sts").get_caller_identity()["Account"]
    resources = []
    if not keep_resources:
        for state in (
            role_state(iam),
            repository_state(ecr),
            bucket_state(s3, setup_bucket_name(account_id)),
        ):
            if state:
                resources.append(state)

    print(f"Runtimes matching {NAME_PREFIX!r}:", flush=True)
    for runtime in runtimes:
        print(
            f"  - {runtime['agentRuntimeName']} ({runtime['agentRuntimeId']}) status={runtime['status']}",
            flush=True,
        )
    if not runtimes:
        print("  (none)", flush=True)

    if not keep_resources:
        print("\nPrerequisite resources:", flush=True)
        for state in resources:
            note = "" if state["managed"] else f"  [skipped: no {MANAGED_TAG_KEY}={MANAGED_TAG_VALUE} tag]"
            print(f"  - {state['name']}{note}", flush=True)
        if not resources:
            print("  (none)", flush=True)

    if not apply_delete:
        print("\nRan without --yes, so nothing was deleted. Re-run with --yes to delete.", flush=True)
        save_result(
            "cleanup.json",
            {
                "applied": False,
                "runtimes": [r["agentRuntimeName"] for r in runtimes],
                "resources": resources,
            },
        )
        return

    runtime_results = []
    for runtime in runtimes:
        name = runtime["agentRuntimeName"]
        runtime_id = runtime["agentRuntimeId"]
        if not name.startswith(NAME_PREFIX):
            print(f"[skip] {name} does not match the prefix", flush=True)
            continue

        print(f"[{name}] delete_agent_runtime id={runtime_id}", flush=True)
        try:
            control.delete_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as error:
            runtime_results.append(
                {
                    "name": name,
                    "agent_runtime_id": runtime_id,
                    "ok": False,
                    "error_type": error.response["Error"]["Code"],
                    "message": error.response["Error"]["Message"],
                }
            )
            print(f"[{name}] delete request failed: {error.response['Error']['Code']}", flush=True)
            continue

        final_status, elapsed_sec = wait_until_deleted(control, runtime_id)
        runtime_results.append(
            {
                "name": name,
                "agent_runtime_id": runtime_id,
                "ok": final_status == "DELETED",
                "final_status": final_status,
                "elapsed_sec": elapsed_sec,
            }
        )
        print(f"[{name}] {final_status} ({elapsed_sec}s)", flush=True)

    # The prerequisites go last. Deleting the role or the repository first would leave a
    # runtime that is still being deleted without the permissions it needs.
    resource_results = []
    deleters = {
        SETUP_ROLE_NAME: lambda: delete_role(iam),
        SETUP_ECR_REPOSITORY: lambda: delete_repository(ecr),
    }
    for state in resources:
        name = state["name"]
        if not state["managed"]:
            print(f"[skip] {name} has no {MANAGED_TAG_KEY}={MANAGED_TAG_VALUE} tag", flush=True)
            resource_results.append({**state, "ok": None, "skipped": True})
            continue
        delete = deleters.get(name, lambda n=name: delete_bucket(s3, n))
        try:
            delete()
        except ClientError as error:
            print(f"[{name}] delete failed: {error.response['Error']['Code']}", flush=True)
            resource_results.append(
                {**state, "ok": False, "error_type": error.response["Error"]["Code"]}
            )
            continue
        print(f"[{name}] DELETED", flush=True)
        resource_results.append({**state, "ok": True})

    save_result(
        "cleanup.json",
        {"applied": True, "runtimes": runtime_results, "resources": resource_results},
    )
    failed = [r for r in runtime_results + resource_results if r.get("ok") is False]
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
