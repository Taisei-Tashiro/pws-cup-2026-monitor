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
