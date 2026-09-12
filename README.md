# PWS Cup 2026 Leaderboard Monitor

[PWS Cup 2026](https://www.codabench.org/competitions/17698/) の現在のphaseのLeaderboardを5分ごとに取得し、得点・順位・掲載参加者の変更をDiscord Webhookで通知します。

Python標準ライブラリのみを使用。OpenAI API、Discord Bot、Dockerは使いません。Submissionファイルや詳細結果ファイルは取得しません。

## 動作

- 開始・終了日時（UTC）で現在phaseを毎回判定。期間外は状態を保持して待機します。
- 公式画面と同じ `/api/phases/{id}/get_leaderboard/?page_size=all` を使用し、全件取得を検証します。
- 初回は通知せず、空のLeaderboardも有効な基準値として保存します。
- 2回目以降はスコア（画面表示精度）、順位（公式APIの表示順）、掲載参加者、提出IDを比較。提出IDだけの変化でも通知します。提出日時だけの変化では通知しません。
- phaseが切り替わると新しいphaseへ自動移行。順位・スコア・参加者に差がある場合だけ、その変更とphase名を通知します。
- 状態と通知の送信進捗は `monitor-state` ブランチの `.monitor/state.json` に保存します。Webhook URLは状態・コードに保存しません。
- 通知失敗時は未送信のメッセージを次の実行で再試行します。Discordが受信した直後の通信断やGitHubへの状態保存失敗では重複する場合があります。通知の「更新ID」で同じ更新を識別できます。
- `@everyone` 等のメンションは無効化します。
- 通知にコホート番号・チーム名・CodaBench名・提出IDを表示します。対応表は `teams.json` です。表示名と実際のアカウント名が異なる場合は `aliases` でも照合します。
- 提出IDは毎回APIから取得し、変更前・変更後も通知します。旧保存データに提出IDがない場合は一度だけ静かに補完し、次回から比較します。未登録チームは未登録と明示し、通知を継続します。コホート21のbot035はCodaBench名未提供のため照合対象外です。

## 初期設定

1. Discordの通知先チャンネルに対する「ウェブフックの管理」権限を持つ人が、チャンネル設定 → 連携サービス → ウェブフックからWebhookを作成します。無料サーバーで利用できます。
2. GitHubの Settings → Secrets and variables → Actions → New repository secret に `DISCORD_WEBHOOK_URL` を登録します。ValueにはWebhook URLを貼り付けます。
3. Actions → **PWS Cup 2026 Monitor** → Run workflow を実行します。通常実行では「取得・検証のみ」をオフにします。
4. 初回ログの `Initial baseline saved. No Discord notification.` と `monitor-state` ブランチの作成を確認します。初回はDiscordに投稿されません。

Secret未登録の場合は警告を表示し、公開APIの取得確認だけを行います。**GitHub Actionsが成功しても、Secret未登録なら監視・Discord通知は稼働していません。** Secret登録後の通常実行で基準値を作成し、以後の定期実行で通知します。

## 手元で確認

```sh
python3 -m unittest discover -s tests -v
python3 main.py --dry-run
```

`--dry-run` は取得・構造検証のみで、状態を保存せず通知もしません。通常実行では環境変数 `DISCORD_WEBHOOK_URL` が必要です。

## 運用上の制約

- GitHub Actionsの実行間隔は5分に設定していますが、混雑で遅延・取りこぼしが生じ得ます。5分以内の検知を保証するものではありません。
- 公開リポジトリの標準GitHub-hosted runnerは無料です。公開リポジトリでは60日間アクティビティがないと定期実行が無効になる場合があります。長期運用時はActionsの有効状態を確認してください。
- CodaBenchのAPI構造が変わった場合は対応が必要です。API取得失敗や破損した保存状態は正常な空データとして扱いません。
- 状態ブランチには公開Leaderboardから得た表示名・得点・順位が保存されます。

## 参照

- [Discord Webhook](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)
- [Discordのチャンネル権限](https://support.discord.com/hc/en-us/articles/10543994968087-Channel-Permissions-Settings-101)
- [GitHub Actionsのschedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actionsの課金](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [CodaBench公式API実装](https://github.com/codalab/codabench/blob/develop/src/apps/api/views/competitions.py)
