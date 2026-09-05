# 早明浦ダム 貯水率 X Bot

## v4: 表情つき投稿 + 貯水率連動の投稿頻度

前回観測より増えたら `🙂 / 😊 / 😄 / 🤩`、減ったら `🥲 / 😥 / 😰 / 😱` を自動で使い分けます。
ただし貯水率10%未満では、ふざけすぎないよう警戒寄りの表現に自動で切り替えます。

例：

```text
💧 早明浦ダム 定点観測
09/05 15:00　12.6% 😊💧
前回比 +0.7pt｜24時間 +2.1pt
貯水量 18,220千m³
流入 8.2 / 放流 4.1 m³/s
出典：国土交通省 川の防災情報
#早明浦ダム #吉野川
```

## 投稿頻度

通常投稿は現在の貯水率で自動的に間隔を変えます。

| 貯水率 | 通常投稿の目安 |
|---|---:|
| 80%以上 | 24時間ごと（1日1回） |
| 60%以上80%未満 | 12時間ごと |
| 40%以上60%未満 | 6時間ごと |
| 20%以上40%未満 | 3時間ごと |
| 10%以上20%未満 | 2時間ごと |
| 10%未満 | 1時間ごと |

ただし、5/10/15/20/25/30/40/50/60/70/80/90%の節目をまたいだ場合、または前回投稿から1.0ポイント以上急変した場合は通常間隔を待たず速報します。

 v2

早明浦ダムの公式公開データを確認し、Xへ自動投稿するbotです。
サーバーを借りず **GitHub Actions** で動かす構成です。

## v2の動き

- GitHub Actionsは **30分ごと** に確認
- 国土交通省「川の防災情報」の新しい観測値が出たら **毎回投稿**（初期設定）
- **7時・12時・19時** は朝/昼/夜レポート
- **5 / 10 / 15 / 20 / 25 / 30% ...** の節目をまたいだら速報風投稿
- 24時間前のデータが履歴にあれば「24時間 ±X.Xpt」も表示
- 貯水量・流入量・放流量・雨量も表示できる範囲で掲載
- 自動投稿には **URLを入れない**
- 1日30投稿で安全装置
- リアルタイム元が落ちた場合は水資源機構の公開値へフォールバック

現在の公式ページでも、早明浦ダムのリアルタイム表には `年月日 / 時刻 / 流域平均雨量 / 貯水量 / 流入量 / 放流量 / 貯水率` が公開されています。

## 投稿イメージ

```text
💧 早明浦ダム 定点観測
09/05 08:00　8.7% ↘️
前回比 -0.1pt｜24時間 -1.7pt
貯水量 12,789千m³
流入 4.8 / 放流 18.1 m³/s
出典：国土交通省 川の防災情報
#早明浦ダム #吉野川
```

節目なら：

```text
🚨 早明浦ダム 10%を下回りました
09/03 14:00　9.9% ⬇️
...
```

## X Developer側

X Developer ConsoleでProject / Appを作成します。
OAuth 1.0aのUser Contextで投稿する構成です。
Appは投稿可能な権限（Read and Write）が必要です。

GitHub Secretsに次の4つを登録します。

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

**キーやトークンは公開リポジトリ、X、ChatGPTなどへ貼らないでください。**

X公式のManage Posts Integration Guideでは、`POST /2/tweets` の作成例とOAuth 1.0a User Contextが案内されています。

## GitHubへの置き方

1. GitHubで新しいリポジトリを作る（Private推奨）
2. このフォルダの中身を全部アップロード
3. `Settings` → `Secrets and variables` → `Actions`
4. 上記4つのSecretを登録
5. `Settings` → `Actions` → `General` → `Workflow permissions` を **Read and write permissions** にする
6. `Actions` タブから `Sameura X Bot` → `Run workflow` で初回実行

最初はXに投稿せず確認したい場合、workflowの `Run bot` のenvに一時的に以下を追加します。

```yaml
DRY_RUN: "true"
```

ログに `POST PREVIEW` が出ます。

## 投稿頻度の変更

`.github/workflows/bot.yml` のenvで変更できます。

### 毎観測値を投稿したくない

```yaml
POST_EVERY_OBSERVATION: "false"
```

この場合でも定時レポートと節目速報は投稿します。

### 定時レポートを変更

```yaml
REPORT_HOURS: "6,12,18,21"
```

### 節目を増やす

```yaml
THRESHOLDS: "5,7.5,10,12.5,15,20,25,30,40,50"
```

## アフィリエイトについて

自動投稿本文にはURLを入れず、**固定ポストに楽天/Amazonの商品紹介を置く**設計をおすすめします。
テンプレートは `PINNED_POST_TEMPLATE.md` に入れています。

「リンクを踏んでbotを支援して」と直接お願いするより、渇水・断水時に実際に役立つ商品を広告表示付きで紹介する形にしています。

## データ元

- 国土交通省 川の防災情報：早明浦ダム（リアルタイム）
  - https://www1.river.go.jp/cgi-bin/DspDamData.exe?ID=1368080700010&KIND=3
- 水資源機構 関西・吉野川支社 吉野川本部（フォールバック）
  - https://www.water.go.jp/yoshino/yoshino/
- X API Create Posts
  - https://docs.x.com/x-api/posts/create-post
- X API Manage Posts Integration Guide
  - https://docs.x.com/x-api/posts/manage-tweets/integrate

ページ側のHTML構造が変われば取得処理の修正が必要になる場合があります。
