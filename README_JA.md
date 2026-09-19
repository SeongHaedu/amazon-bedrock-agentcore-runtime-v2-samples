[English](README.md) | Japanese

# Amazon Bedrock AgentCore Runtime V2: platformVersion サンプル

Amazon Bedrock AgentCore Runtime のプラットフォームバージョン V2 を試すためのサンプルコードです。V2 はエージェントをスナップショットから復元して起動するため、同時実行数やイメージサイズに関係なくコールドスタートを速く一貫した状態に保ちます。エージェントランタイムごとに `platformVersion` フィールド (`V1` または `V2`) で選択し、既定値は V1 です。

本リポジトリのスクリプトで、V2 ランタイムの作成、既存の V1 ランタイムから V2 への移行、プラットフォームバージョンの確認、呼び出し、そして V2 が起動時のコードの実行タイミングをどう変えるかの観測ができます。

ブログ記事: 公開後にリンクを追加します。

## ディレクトリ構成

```
.
├── agent-basic/                 最小のエージェント。モジュールスコープとハンドラのマーカーを出力する。
│   ├── main.py
│   └── Dockerfile
├── agent-globalinit-probe/      プローブ用エージェント。グローバル初期化と遅延初期化を各 10 秒置く。
│   ├── main.py
│   └── Dockerfile
├── scripts/
│   ├── common.py                共通の設定とヘルパー。設定はすべて環境変数から読む。
│   ├── check_sdk_version.py     導入済み SDK の platformVersion 対応を確認する。AWS を呼ばない。
│   ├── setup_codezip_artifact.py ARM64 向けの ZIP を作り S3 にアップロードする (Docker 不要)。
│   ├── create_runtime.py        V1 / V2 / 省略でランタイムを作成し READY までを計測する。
│   ├── switch_platform_version.py 既存ランタイムを V1 と V2 の間で切り替える。
│   ├── get_runtime.py           get_agent_runtime で platformVersion を確認する。
│   ├── invoke_runtime.py        新規セッションで呼び出し、セッション再利用と比較する。
│   ├── probe_globalinit.py      プローブを同時呼び出しし、起動時の処理の所在を観測する。
│   └── cleanup_runtimes.py      名前プレフィックスに一致するランタイムのみを削除する。
└── requirements.txt             boto3>=1.43.95
```

## 前提条件

- Python 3.10 以降
- `boto3>=1.43.95`。`platformVersion` フィールドを含む最初の公開版です。これ未満のバージョンでは、リクエストが送信される前に `ParamValidationError` で拒否されます。
- AgentCore Runtime の実行ロール
- V2 が利用できるリージョン: `us-east-1`、`us-east-2`、`us-west-2`、`eu-west-1`、`ap-northeast-1`
- コンテナで試す場合: `docker buildx` が使える Docker (AgentCore Runtime の microVM は ARM64 Linux です) と ECR リポジトリ
- 直接コードデプロイで試す場合: 既存の S3 バケット

> 注意: AWS CloudFormation と AWS CDK は現時点で `platformVersion` の設定に対応していません。AWS SDK、CLI、またはコンソールを使用してください。

## セットアップ

```bash
git clone https://github.com/SeongHaedu/amazon-bedrock-agentcore-runtime-v2-samples.git
cd amazon-bedrock-agentcore-runtime-v2-samples

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

以下のコマンドはすべてリポジトリのルートから実行します。

## 環境変数

```bash
export AWS_REGION=ap-northeast-1
export AGENTCORE_ROLE_ARN=arn:aws:iam::<account-id>:role/<execution-role>

# コンテナで試す場合
export AGENTCORE_CONTAINER_URI=<account-id>.dkr.ecr.$AWS_REGION.amazonaws.com/<repository>:v2sample

# 直接コードデプロイで試す場合
export AGENTCORE_S3_BUCKET=<bucket-name>
export AGENTCORE_S3_PREFIX=agentcore/codezip/agent.zip   # 省略可。これが既定値である。

# 任意
export AWS_PROFILE=<profile>
export AGENTCORE_NAME_PREFIX=v2sample_                   # cleanup_runtimes.py はこのプレフィックスのみを削除する
```

## 手順 1: SDK を確認する

```bash
python scripts/check_sdk_version.py
```

AWS を一切呼びません。導入済みの botocore のサービスモデルを読み、`CreateAgentRuntime` と `UpdateAgentRuntime` のリクエスト側、および `GetAgentRuntime` のレスポンス側に `platformVersion` が存在するかを報告します。`GetAgentRuntime` のリクエスト側に存在しないのは仕様どおりです。`platformVersion` はレスポンス専用のフィールドです。

## 手順 2: アーティファクトを用意する

### 方法 A: コンテナ

```bash
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin \
    $(echo $AGENTCORE_CONTAINER_URI | cut -d/ -f1)

docker buildx build --platform linux/arm64 \
  -t $AGENTCORE_CONTAINER_URI \
  --push agent-basic/
```

### 方法 B: 直接コードデプロイ (Docker 不要)

```bash
python scripts/setup_codezip_artifact.py agent-basic
```

依存関係を `manylinux2014_aarch64` 向けにベンダリングし、`main.py` とともに ZIP にまとめて `s3://$AGENTCORE_S3_BUCKET/$AGENTCORE_S3_PREFIX` にアップロードします。

## 手順 3: V2 ランタイムを作成する

```bash
python scripts/create_runtime.py basic_v2 container V2
```

ZIP を作った場合は `container` を `codezip` に置き換えます。第 3 引数がプラットフォームバージョンで、`V2`、`V1`、`omit` のいずれかです。

V2 の作成は環境の準備とスナップショットの取得を行うため、秒ではなく分単位の時間がかかります。スクリプトは `get_agent_runtime` をポーリングし、`READY` または `FAILED` で終わる状態になるまで待ち、各ステータス遷移を経過時間とともに表示します。

差を自分で確認する場合は、同じアーティファクトから V1 のランタイムも作成します。

```bash
python scripts/create_runtime.py basic_v1 container omit
```

## 手順 4: プラットフォームバージョンを確認する

```bash
python scripts/get_runtime.py
```

`create_agent_runtime` と `update_agent_runtime` のレスポンスには `platformVersion` が含まれません。`get_agent_runtime` には含まれます。`list_agent_runtimes` にも含まれないため、このスクリプトはランタイムごとに `get_agent_runtime` を呼びます。

ご自身のコードで判定する場合は `resp.get("platformVersion", "V1")` の形で書いてください。V1 が既定値です。

## 手順 5: V1 から V2 へ移行する

```bash
python scripts/switch_platform_version.py <agentRuntimeId> V2
```

`update_agent_runtime` は `roleArn` と `agentRuntimeArtifact` が必須であるため、スクリプトは `get_agent_runtime` で現行値を読み、そのまま引き継ぎます。`environmentVariables` も明示的に引き継ぎます。省略した場合に既存の環境変数がクリアされるかどうかがドキュメントに明記されていないためです。

直接確認する価値のある挙動が 2 つあります。

- `V2` から `V1` への切り戻しは数秒で完了します。スナップショットの準備が不要なためです。
- 既に V2 のランタイムに対して `platformVersion` を省略した更新を行うと、V2 が維持されたうえでスナップショットの準備も実行されます。したがってアーティファクトの差し替えのような通常の更新も、V2 では分単位の時間がかかります。

```bash
python scripts/switch_platform_version.py <agentRuntimeId> V1     # 数秒
python scripts/switch_platform_version.py <agentRuntimeId> omit   # 数分。V2 のまま維持される。
```

update を呼ぶ前に、ランタイムが終端状態 (`READY` または `*_FAILED`) になっている必要があります。`CREATING` / `UPDATING` / `DELETING` の間に呼ぶと `ConflictException` になります。

## 手順 6: 呼び出す

```bash
python scripts/invoke_runtime.py <agentRuntimeId> 3 2
```

引数はセッション数とセッションあたりの呼び出し回数です。各セッションは新しい `runtimeSessionId` を使うため、初回の呼び出しは必ず起動の経路を通ります。同一セッションの 2 回目は既存の実行環境に着地するため、起動を含まないリクエストの下限が得られます。

`agent-basic` はモジュールスコープで構築した `snapshot_identity` を返します。スクリプトが最後に表示する `distinct identity uuid` の行に注目してください。

- V2 では、いくつセッションを作っても 1 です。すべてのインスタンスが同一のスナップショットから復元されています。
- V1 では、新しく起動した実行環境の数と一致します。それぞれが自分でモジュールスコープを実行しています。

## 手順 7: 起動時の処理がどこで走るかを確認する

ここが V2 でエージェントコードの書き方が変わる部分です。`agent-globalinit-probe` はモジュールスコープで 10 秒、各セッションの初回リクエストでさらに 10 秒スリープします。

```bash
docker buildx build --platform linux/arm64 \
  -t <account-id>.dkr.ecr.$AWS_REGION.amazonaws.com/<repository>:probe \
  --push agent-globalinit-probe/

AGENTCORE_CONTAINER_URI=<account-id>.dkr.ecr.$AWS_REGION.amazonaws.com/<repository>:probe \
  python scripts/create_runtime.py probe_v2 container V2

python scripts/probe_globalinit.py <agentRuntimeId> 20
```

V2 では、グローバル初期化の 10 秒はどの呼び出しにも現れません。スナップショットの準備時に 1 回だけ消費されています。遅延初期化の 10 秒は、すべてのセッションの初回呼び出しに現れます。スナップショットがこれを運べないためです。

同じイメージから作成した V1 のランタイムに対して同じことを行い、pre-warmed instance を枯渇させるだけの同時セッションを投げてください。その場合はグローバル初期化がリクエストの経路に現れます。

プローブが返す他の値にも注目してください。`baked.wall_clock` はモジュールスコープが実行された時刻であるため、V2 ではスナップショットが古くなるにつれて `now` との差が広がります。これが、V2 ではタイムスタンプ・認証情報・乱数シード・確立済みの接続をモジュールスコープで保持してはならない具体的な理由です。詳細は [Optimize your agent for Amazon Bedrock AgentCore Runtime V2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html) を参照してください。

## クリーンアップ

```bash
python scripts/cleanup_runtimes.py          # 対象を表示するだけ
python scripts/cleanup_runtimes.py --yes    # 実際に削除する
```

`AGENTCORE_NAME_PREFIX` (既定 `v2sample_`) で始まる名前のランタイムのみを削除します。`--yes` を付けない場合は一覧を表示して終了します。

ランタイム自体の削除は V2 でも数秒で終わります。裏側のスナップショットの消失には最大 8 時間かかります。そのスナップショット上で既に動いているセッションが終了まで継続するためであり、8 時間はセッションの最大寿命です。

## 注意事項と現時点の制限

- V2 はエージェントの環境変数の合計サイズを、直接コードデプロイで 1.5 KB、コンテナエージェントで 2.5 KB に制限しています。V1 は 4 KB です。超えると `ValidationException` で失敗します。ドキュメントにはこの上限を V1 と同じ値まで引き上げる予定と記載されています。
- コンテナは起動から 120 秒以内に `/ping` で healthy を報告する必要があります。間に合わない場合、ヘルスチェックのエラーで作成が失敗します。AgentCore SDK を使う場合はこれが自動的に満たされます。`app.run()` までサーバが listen しないため、スナップショットは構造上、完全に初期化されたエージェントを捉えます。
- AgentCore SDK ではなく独自の HTTP サーバを動かす場合は、初期化が完了してから healthy を返してください。
- コンテナで独自の暗号ライブラリを持ち込む場合は、復元後に再シードする snapshot-safe なビルドを使ってください。Amazon Linux 2023 では `openssl-snapsafe-libs` を使います。直接コードデプロイのサービス管理ベースイメージには snapshot-safe なビルドが既に含まれています。

## 参考

- [microVMs — Platform versions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html#runtime-platform-versions)
- [Optimize your agent for Amazon Bedrock AgentCore Runtime V2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html)
- [Host agent or tools with Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html)
- [The new AgentCore runtime: Elastic, optimized, and consistently fast starts](https://aws.amazon.com/jp/blogs/machine-learning/the-new-agentcore-runtime-elastic-optimized-and-consistently-fast-starts/)
- [create_agent_runtime](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_agent_runtime.html) / [update_agent_runtime](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/update_agent_runtime.html) / [get_agent_runtime](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/get_agent_runtime.html)
