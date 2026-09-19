# common.py
# 全スクリプトが共有する設定とヘルパー。
#
# 設定はすべて環境変数から読む。アカウント ID・ロール ARN・ECR URI・S3 バケット名を
# リポジトリにハードコードしないためである。未設定の必須項目は実行時に明示的に落とす。
import json
import os
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# platformVersion に対応した最初の公開版である。これ未満では送信前に ParamValidationError になる。
MIN_BOTO3 = "1.43.95"

REGION = os.environ.get("AWS_REGION", "us-west-2")
PROFILE = os.environ.get("AWS_PROFILE") or None

ROLE_ARN = os.environ.get("AGENTCORE_ROLE_ARN")
CONTAINER_URI = os.environ.get("AGENTCORE_CONTAINER_URI")
S3_BUCKET = os.environ.get("AGENTCORE_S3_BUCKET")
S3_PREFIX = os.environ.get("AGENTCORE_S3_PREFIX", "agentcore/codezip/agent.zip")
CODE_RUNTIME = os.environ.get("AGENTCORE_CODE_RUNTIME", "PYTHON_3_11")
ENTRY_POINT = [os.environ.get("AGENTCORE_ENTRY_POINT", "main.py")]

# 作成するランタイム名に付けるプレフィックス。cleanup_runtimes.py はこのプレフィックスを
# 持つランタイムのみを削除対象にする。既存リソースを誤って削除しないための安全策である。
NAME_PREFIX = os.environ.get("AGENTCORE_NAME_PREFIX", "v2sample_")

NETWORK = {"networkMode": "PUBLIC"}
ENV_VARS = {"PYTHONUNBUFFERED": "1"}

# V2 の create / update はスナップショット準備のため分単位かかる。V1 の所要時間を前提にした
# 短いタイムアウトでは完了を待たずに打ち切ってしまうため、十分な余裕を取る。
TIMEOUT_SEC = int(os.environ.get("AGENTCORE_WAIT_TIMEOUT_SEC", "1800"))
INTERVAL_SEC = 5

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def require_env(name, value, hint):
    if not value:
        raise SystemExit(f"環境変数 {name} が未設定である。{hint}")
    return value


def make_client(service):
    session = boto3.Session(profile_name=PROFILE) if PROFILE else boto3.Session()
    return session.client(service, region_name=REGION)


def artifact_for(kind):
    """container と codezip のどちらでも platformVersion の指定方法は同じである。
    アーティファクトの形だけが異なる。"""
    if kind == "container":
        require_env(
            "AGENTCORE_CONTAINER_URI",
            CONTAINER_URI,
            "ECR の <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag> を設定する。",
        )
        return {"containerConfiguration": {"containerUri": CONTAINER_URI}}
    if kind == "codezip":
        require_env(
            "AGENTCORE_S3_BUCKET",
            S3_BUCKET,
            "scripts/setup_codezip_artifact.py でアップロード先に使う S3 バケット名を設定する。",
        )
        return {
            "codeConfiguration": {
                "code": {"s3": {"bucket": S3_BUCKET, "prefix": S3_PREFIX}},
                "runtime": CODE_RUNTIME,
                "entryPoint": ENTRY_POINT,
            }
        }
    raise SystemExit(f"未知のアーティファクト種別である: {kind} (container か codezip を指定する)")


def platform_version_supported():
    """導入済み botocore のサービスモデルに platformVersion が含まれるかを調べる。
    バージョン文字列ではなくサービスモデルで判定するのは、バージョン番号が新しくても
    当該フィールドを含まないビルドが存在しうるためである。"""
    import botocore.session

    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    return {
        # create / update はリクエスト側、get はレスポンス側に platformVersion を持つ。
        # GetAgentRuntime のリクエストには存在しないのが正しい仕様である。
        "create_input": "platformVersion" in model.operation_model("CreateAgentRuntime").input_shape.members,
        "update_input": "platformVersion" in model.operation_model("UpdateAgentRuntime").input_shape.members,
        "get_output": "platformVersion" in model.operation_model("GetAgentRuntime").output_shape.members,
    }


def wait_until_ready(client, agent_runtime_id, timeout_sec=TIMEOUT_SEC, interval_sec=INTERVAL_SEC):
    """終端判定は status == "READY" または status.endswith("FAILED") で行う。
    失敗ステータスを網羅列挙しないのは、一覧に無い失敗が返った場合にループが止まらなくなる
    事態を避けるためである。AgentCore Runtime には waiter が用意されていないため、
    次の呼び出しに進む前にこのポーリングで終端状態を確認する必要がある。"""
    start = time.monotonic()
    transitions = []
    last = None
    while True:
        status = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)["status"]
        if status != last:
            elapsed = time.monotonic() - start
            transitions.append({"status": status, "at_sec": round(elapsed, 1)})
            print(f"    [{elapsed:7.1f}s] {status}", flush=True)
            last = status
        if status == "READY" or status.endswith("FAILED"):
            return status, transitions
        if time.monotonic() - start > timeout_sec:
            return f"TIMEOUT(last={status})", transitions
        time.sleep(interval_sec)


def wait_until_deleted(client, agent_runtime_id, timeout_sec=600, interval_sec=5):
    """削除の完了判定は ResourceNotFoundException と DELETE_FAILED の両方を見る。
    ResourceNotFoundException のみを条件にすると、DELETE_FAILED になった場合に
    ループが終わらない。wait_until_ready は削除の判定には再利用できない。"""
    start = time.monotonic()
    while True:
        try:
            status = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)["status"]
        except ClientError as error:
            if error.response["Error"]["Code"] == "ResourceNotFoundException":
                return "DELETED", round(time.monotonic() - start, 1)
            raise
        if status == "DELETE_FAILED":
            return "DELETE_FAILED", round(time.monotonic() - start, 1)
        if time.monotonic() - start > timeout_sec:
            return f"TIMEOUT(last={status})", round(time.monotonic() - start, 1)
        time.sleep(interval_sec)


def describe_platform_version(client, agent_runtime_id):
    """get_agent_runtime を呼び、platformVersion のキーの有無と値を分けて記録する。
    判定コードは resp.get("platformVersion", "V1") の形で書くのが安全である。"""
    resp = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)
    return {
        "platform_version": resp.get("platformVersion"),
        "key_present": "platformVersion" in resp,
        "status": resp.get("status"),
        "agent_runtime_version": resp.get("agentRuntimeVersion"),
        "raw_response": jsonable(resp),
    }


def list_sample_runtimes(client):
    """NAME_PREFIX を持つランタイムのみを列挙する。list_agent_runtimes のレスポンスには
    platformVersion が含まれないため、値が必要な場合は get_agent_runtime を個別に呼ぶ。"""
    paginator = client.get_paginator("list_agent_runtimes")
    found = []
    for page in paginator.paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime["agentRuntimeName"].startswith(NAME_PREFIX):
                found.append(runtime)
    return found


def jsonable(obj):
    """createdAt / lastUpdatedAt は datetime であり json.dumps がそのままでは扱えない。
    読み取り不能なオブジェクトは repr に落とし、記録が欠落しないようにする。"""
    from datetime import date, datetime

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


def save_result(filename, obj):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / filename
    path.write_text(json.dumps(jsonable(obj), indent=2, ensure_ascii=False, default=str))
    print(f"saved: {path}", flush=True)
    return path
