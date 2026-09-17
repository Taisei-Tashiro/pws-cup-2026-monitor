# PWS Cup 2026 Leaderboard Monitor

[PWS Cup 2026](https://www.codabench.org/competitions/17698/) の現在のphaseのLeaderboardを5分ごとに取得し、得点・順位・掲載参加者・提出IDの変更をDiscord Webhookで通知します。

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
- 本文には新規掲載・提出ID変更・スコア変更・取り下げをチームごとに表示します。順位だけの変化は件数のみをまとめ、個別チームの更新ブロックは作りません。加工フェーズのスコア名は総合U・U_gen・U_spec・U_rare・U_valid・保護と簡潔に表示します。
- 各通知の冒頭にLeaderboard取得直後の確認時刻を日本時間（JST）で表示します。再送時にも元の確認時刻を保持します。長い通知は原則チーム単位で分割し、分割した各メッセージにも同じ確認時刻を付けます。時刻の経過だけでは通知しません。
- 通知にコホート番号・チーム名・CodaBench名・提出IDを表示します。対応表は `teams.json` です。phase名に応じて予備戦・本戦の番号を切り替えます（例：zhiyanは予備戦5／本戦28）。本戦0も有効な番号として扱い、区分が不明なら番号を推測しません。表示名と実際のアカウント名が異なる場合は `aliases` でも照合します。
- 提出IDは毎回APIから取得し、変更前・変更後も通知します。旧保存データに提出IDがない場合は一度だけ静かに補完し、次回から比較します。未登録チームは未登録と明示し、通知を継続します。bot035（予備戦21／本戦17）とHiddenMetrics（本戦11）はCodaBench名未提供のため照合保留です。

- 同じphaseで掲載が消えた提出は「取り下げ・掲載終了」として、直前の提出ID・順位・スコアを通知します。消失理由はAPIでは判別できないため、取り下げと断定しません。再掲載は参加として通知します。同じ参加者の提出IDが差し替わった場合は更新として扱います。複数提出がある場合は残存する提出IDを先に照合します。phase移行による消失は別の表記にします。

- 変更通知ごとに、全掲載チームの現在順位・順位変動・コホート・チーム名・CodaBench名・提出ID・全スコアを「｜」区切りのUTF-8テキストファイルで添付します。長い名称は表中で省略し、末尾に全文を載せます。確認時刻は本文・添付で共通です。分割通知では最後のメッセージに1回だけ添付し、再送でも保存済みの同じファイルを使います。変更がない回は通知・添付を送りません。

## 初期設定

1. Discordの通知先チャンネルに対する「ウェブフックの管理」権限を持つ人が、チャンネル設定 → 連携サービス → ウェブフックからWebhookを作成します。無料サーバーで利用できます。
2. GitHubの Settings → Secrets and variables → Actions → New repository secret に `DISCORD_WEBHOOK_URL` を登録します。ValueにはWebhook URLを貼り付けます。
3. Actions → **PWS Cup 2026 Monitor** → Run workflow を実行します。通常実行では「取得・検証のみ」をオフにします。
4. 初回ログの `Initial baseline saved. No Discord notification.` と `monitor-state` ブランチの作成を確認します。初回はDiscordに投稿されません。

Secret未登録の場合は警告を表示し、公開APIの取得確認だけを行います。**GitHub Actionsが成功しても、Secret未登録なら監視・Discord通知は稼働していません。** Secret登録後の通常実行で基準値を作成し、以後の定期実行で通知します。

## 定期起動（cron-job.org）

cron-job.orgが5分ごと（Asia/Tokyo、毎時0・5・10…55分）にGitHub Actionsを起動します。Actionsの履歴には `PWS Cup 2026 Monitor · cron-job.org` と表示され、イベント種別は `workflow_dispatch` です。PCを閉じても動作します。

- HTTPメソッド: `POST`
- URL: `https://api.github.com/repos/Taisei-Tashiro/pws-cup-2026-monitor/actions/workflows/monitor-scheduled.yml/dispatches`
- ヘッダー: `Accept: application/vnd.github+json`、`Content-Type: application/json`、`X-GitHub-Api-Version: 2026-03-10`、`Authorization: Bearer <GitHubトークン>`
- 本文: `{"ref":"main","inputs":{"dry_run":false,"trigger_source":"cron-job.org"}}`
- GitHubトークンはこのリポジトリのみを対象とするfine-grained tokenで、Actions読み書きと必須のMetadata読み取りを付与します。トークンはcron-job.orgだけに保存し、コード・文書には記録しません。Discord WebhookはGitHub Secretに保持します。
- 現在のトークン有効期限は **2026年10月12日23:51 JST**。それ以降も監視する場合は、期限前にトークンを更新してcron-job.orgのAuthorizationヘッダーを差し替えます。
- cron-job.orgのHTTP 200は起動要求の受付成功です。実際の監視はGitHub Actionsの `Monitor current leaderboard` と状態保存ステップの成功も確認します。起動要求の失敗時はcron-job.orgのメール通知が届く設定です。
- 停止はcron-job.orgのジョブの `Enable job` をオフにします。GitHub標準の `schedule` は併用せず、手動実行用の `workflow_dispatch` は残します。

## 未開始の監視を自動復旧

同じ5分ごとの起動で、通常監視の `monitor`（ubuntu-latest）と復旧役の `watchdog`（ubuntu-24.04-arm）を並行実行します。排他制御はmonitorジョブだけに適用し、monitorが待機していても後続のwatchdogは動ける構成です。

- このリポジトリ・main・monitor-scheduled.ymlの通常dispatchだけを対象にします。
- monitorが10分以上未開始で、runner未割り当て・処理ステップ0件の場合だけ、状態を再確認して通常キャンセルを要求します。次の5分ごとの実行が引き継ぐため、通常は待機開始から10〜15分程度で解除を試みます。GitHubの遅延があればそれ以上かかります。
- 実行中、runner割り当て済み、再実行attempt、別ジョブが実行中、API情報が不完全な場合は解除しません。強制キャンセルは使いません。1回に最大3件まで解除します。
- 解除直前にmonitorが開始する競合に備え、通知・状態保存の各ステップには通常キャンセル時も継続する条件を設定しています。ただし割り当て確認とキャンセルの間の競合を完全に排除できる保証はありません。実行時間上限は5分です。
- watchdogにはこのリポジトリの一時的なGITHUB_TOKENでActions書き込み権限を付けます。Discord Secretはwatchdogに渡しません。追加のサービス登録や認証トークンは不要です。
- 実行一覧は取得中に変化するため、件数と一覧の一時的な不一致を失敗扱いにしません。解除直前の実行状態・ジョブ一覧の再確認は引き続き行います。
- GitHub APIの一時的な通信障害・HTTP 500/502/503/504は、読み取りだけ最大2回再試行します。解除要求は重複送信せず、権限エラーや継続する障害は失敗として記録します。ログには認証情報や応答本文を出しません。
- GitHub全体の障害・全runner不足・cron-job.org停止では復旧役も動けない場合があります。独立した外部障害通知は未導入です。
- Actionsログの `Release stale unassigned monitor jobs` と実行サマリーに解除要求を記録します。

動作試験はRun workflowで `dry_run` と `recovery_probe` を両方ONにします。通常監視とは別の排他グループ・専用の未割り当てラベルで待機させ、次の通常実行のwatchdogが解除します。この隔離試験に限り待機判定は1分で、Discord通知・状態変更は行いません。通常利用では両方OFFです。

## 手元で確認

```sh
python3 -m unittest discover -s tests -v
python3 main.py --dry-run
```

`--dry-run` は取得・構造検証のみで、状態を保存せず通知もしません。通常実行では環境変数 `DISCORD_WEBHOOK_URL` が必要です。

## 運用上の制約

- cron-job.orgの起動要求は5分間隔ですが、サービス障害やGitHub Actionsの待ち時間があるため5分以内の検知を保証するものではありません。
- cron-job.orgと公開リポジトリの標準GitHub-hosted runnerを使用します。長期運用時はトークンの期限、cron-job.orgのジョブ有効状態、Actionsの実行結果を確認してください。
- CodaBenchのAPI構造が変わった場合は対応が必要です。API取得失敗や破損した保存状態は正常な空データとして扱いません。
- 状態ブランチには公開Leaderboardから得た表示名・得点・順位が保存されます。

## 参照

- [Discord Webhook](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)
- [Discordのチャンネル権限](https://support.discord.com/hc/en-us/articles/10543994968087-Channel-Permissions-Settings-101)
- [GitHub Actionsのschedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [cron-job.org](https://cron-job.org/en/)
- [GitHub Actionsの外部起動API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
- [GitHub Actionsの課金](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [CodaBench公式API実装](https://github.com/codalab/codabench/blob/develop/src/apps/api/views/competitions.py)
