# U-Back ユーザーマニュアル

> **対象読者:** U-Back を初めてセットアップする方、または各機能を詳しく知りたい方  
> **対応環境:** Windows 10/11、Python 3.11 以上、UGREEN NAS（または EXT4/Btrfs の SMB 共有）

---

## 目次

1. [セットアップ](#1-セットアップ)
2. [はじめてのバックアップ](#2-はじめてのバックアップ)
3. [自動スケジュールの設定](#3-自動スケジュールの設定)
4. [保持ポリシー（古いスナップショットの自動削除）](#4-保持ポリシー古いスナップショットの自動削除)
5. [ステータスと節約量の確認](#5-ステータスと節約量の確認)
6. [Windows 通知の設定](#6-windows-通知の設定)
7. [複数フォルダのバックアップ](#7-複数フォルダのバックアップ)
8. [トラブルシューティング](#8-トラブルシューティング)
9. [よくある質問](#9-よくある質問)

---

## 1. セットアップ

### 1-1. Python のインストール確認

PowerShell を開いて確認します:

```powershell
python --version
# Python 3.11.x 以上であること
```

Python が入っていない場合は [python.org](https://www.python.org/downloads/) からインストールしてください。

### 1-2. U-Back のダウンロード

```powershell
git clone https://github.com/airesearchagl-art/U-Back.git
cd U-Back
```

### 1-3. 通知機能のインストール（オプション）

Windows の通知センターに結果を表示したい場合:

```powershell
pip install win11toast
```

> `win11toast` がなくても U-Back のすべての機能は使えます。通知だけがスキップされます。

### 1-4. NAS 側の準備

UGREEN NAS の共有フォルダを Windows からアクセスできる状態にしてください。

**確認コマンド（PowerShell）:**

```powershell
# NAS への接続テスト
Test-Path "\\192.168.x.x\Backup"
# True が返れば OK
```

---

## 2. はじめてのバックアップ

### 2-1. 基本コマンド

```powershell
python -m uback.cli run `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\U-Back"
```

| 引数 | 説明 |
|---|---|
| `--source` | バックアップ**元**（ローカルの対象フォルダ） |
| `--base-dir` | バックアップ**先**（NAS 上のフォルダ。U-Back が管理するルート） |

### 2-2. 初回実行の出力例

```
Snapshot : 2026-04-19-153000       ← タイムスタンプ付きフォルダ名
Previous : (none — first backup)   ← 初回なので前回なし
Copied   : 1,622                   ← コピーしたファイル数
Linked   : 0                       ← ハードリンク数（初回は 0）
Errors   : 0
Size     : 8.3 MB                  ← 今回実際に書き込んだ容量
Elapsed  : 12.4s
Success  : True
```

### 2-3. 2 回目以降の実行

変更されていないファイルはハードリンクになり、コピー量が激減します:

```
Snapshot : 2026-04-19-163000
Previous : 2026-04-19-153000      ← 前回スナップショットを自動検出
Copied   : 3                      ← 変更があった 3 ファイルだけコピー
Linked   : 1,619                  ← 残り 1,619 ファイルはハードリンク
Errors   : 0
Size     : 0.1 MB                 ← 今回の実消費はわずか 0.1 MB
Elapsed  : 1.2s
```

### 2-4. NAS 上のフォルダ構造

U-Back が自動的に作成するフォルダ構造:

```
\\192.168.x.x\Backup\U-Back\
├── 2026-04-19-153000\        ← 初回スナップショット
│   ├── Documents\
│   │   ├── report.docx
│   │   └── photo.jpg
│   └── backup_info.json      ← 実行結果のメタデータ
│
├── 2026-04-19-163000\        ← 2 回目（差分のみ実体あり）
│   ├── Documents\
│   │   ├── report.docx       ← ハードリンク（容量ゼロ）
│   │   └── photo.jpg         ← ハードリンク（容量ゼロ）
│   └── backup_info.json
│
└── .uback.lock               ← 実行中のみ存在するロックファイル
```

---

## 3. 自動スケジュールの設定

### 3-1. タスクスケジューラへの登録

> **注意:** 管理者権限の PowerShell で実行してください（右クリック →「管理者として実行」）

```powershell
python -m uback.cli schedule `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\U-Back" `
  --interval 60
```

これで「1 時間ごとにバックアップを実行」するタスクが Windows タスクスケジューラに登録されます。

| オプション | 説明 | 例 |
|---|---|---|
| `--interval 60` | 実行間隔（分） | `60` = 1 時間ごと |
| `--interval 30` | 30 分ごと | |
| `--interval 1440` | 1 日ごと | |

### 3-2. 登録内容の確認

タスクスケジューラ（`taskschd.msc`）を開くと、`U-Back\Backup-U-Back` という名前でタスクが確認できます。

または PowerShell で:

```powershell
schtasks /Query /TN "U-Back\Backup-U-Back" /FO LIST
```

### 3-3. スケジュールの解除

```powershell
python -m uback.cli unschedule --base-dir "\\192.168.x.x\Backup\U-Back"
```

---

## 4. 保持ポリシー（古いスナップショットの自動削除）

バックアップが増え続けると NAS を圧迫します。U-Back は 2 つのルールで古いスナップショットを自動削除できます。

### 4-1. 保持ポリシーの考え方

```
全スナップショット（古い順）
│
├── [候補: 削除対象になりえる]
│   ├── 2026-01-01-000000  ← keep_count の外 かつ max_age 超え → 削除
│   ├── 2026-02-15-120000  ← keep_count の外 かつ max_age 超え → 削除
│   └── 2026-04-05-000000  ← keep_count の外 だが max_age 以内 → 保持
│
└── [保護: 常に残る]
    ├── 2026-04-17-230000  ─┐
    ├── 2026-04-18-230000   │ 最新 keep_count 件は
    └── 2026-04-19-120000  ─┘ 年齢に関係なく保持
```

### 4-2. `--keep` と `--max-age` の指定

```powershell
python -m uback.cli run `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\U-Back" `
  --keep 10 `
  --max-age 30
```

| オプション | 説明 | 推奨値 |
|---|---|---|
| `--keep N` | 最新 N 件は必ず保持（年齢問わず） | `10` |
| `--max-age DAYS` | `keep` の外にある古いスナップショットを DAYS 日後に削除 | `30` |

### 4-3. スケジュール実行にも保持ポリシーを設定

```powershell
python -m uback.cli schedule `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\U-Back" `
  --interval 60 `
  --keep 10 --max-age 30
```

これで「1 時間ごとにバックアップ＋古いものを自動削除」が全自動になります。

### 4-4. ハードリンクと削除の安全性

U-Back はスナップショットの削除に `shutil.rmtree` を使います。ハードリンクで他のスナップショットと共有しているファイルは、削除してもデータは消えません（inode の参照カウントが 1 減るだけ）。古いスナップショットを削除しても、新しいスナップショットのファイルは完全に保持されます。

---

## 5. ステータスと節約量の確認

### 5-1. status コマンドの実行

```powershell
python -m uback.cli status --base-dir "\\192.168.x.x\Backup\U-Back"
```

### 5-2. 出力の読み方

```
══════════════════════════════════════════════════════════════
  U-Back Status  —  \\192.168.x.x\Backup\U-Back
══════════════════════════════════════════════════════════════

  Snapshots (oldest → newest):

    #  Snapshot                Files    New data  Status
  ──────────────────────────────────────────────────────────
    1  2026-04-17-230000       1,622    8.3 MB    ✓   (A)
    2  2026-04-18-230000       1,625    0.2 MB    ✓
    3  2026-04-19-120000       1,626    0.1 MB    ✓

  Latest backup : 2026-04-19-120000  (2 hours ago)    (B)
  Total snapshots: 3

  Storage summary:
  ──────────────────────────────────────────────────────────
  Virtual full size........................    24.9 MB    (C)
  Actual NAS usage.........................     8.6 MB    (D)
  ──────────────────────────────────────────────────────────
  Hard link savings : 16.3 MB saved  (65.5%)              (E)
```

| 記号 | 意味 |
|---|---|
| **(A) New data** | そのスナップショットが NAS に書き込んだ実際の容量（ハードリンクは除く） |
| **(B) Latest backup** | 最後のバックアップからの経過時間 |
| **(C) Virtual full size** | ハードリンクなしで全スナップショットを単純コピーした場合の仮想容量 |
| **(D) Actual NAS usage** | inode 単位で重複を除いた実際の消費容量 |
| **(E) Hard link savings** | (C) − (D) の差分。「U-Back のおかげで節約できた容量」 |

### 5-3. `backup_info.json` の内容

各スナップショットフォルダには `backup_info.json` が生成されます:

```json
{
  "snapshot_name": "2026-04-19-153000",
  "source": "C:\\Users\\YourName\\Documents",
  "started_at": "2026-04-19T06:30:00.123456+00:00",
  "finished_at": "2026-04-19T06:30:12.456789+00:00",
  "elapsed_seconds": 12.333,
  "previous_snapshot": "2026-04-18-230000",
  "files_copied": 3,
  "files_linked": 1619,
  "errors": 0,
  "total_files": 1622,
  "total_bytes": 102400,
  "success": true
}
```

---

## 6. Windows 通知の設定

### 6-1. win11toast のインストール

```powershell
pip install win11toast
```

### 6-2. `--notify` フラグをつけて実行

```powershell
python -m uback.cli run `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\U-Back" `
  --notify
```

### 6-3. 通知パターン

| 状況 | 通知タイトル | 本文 |
|---|---|---|
| 成功 | `U-Back ✓` | スナップショット名・コピー数・サイズ・経過時間 |
| エラー | `U-Back ✗` | エラー内容 |
| NAS 到達不能 | `U-Back ✗` | NAS is unavailable |
| 二重起動（スキップ） | `U-Back — Skipped` | Another process is running |

### 6-4. スケジュール実行でも通知を受け取る

```powershell
python -m uback.cli schedule `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\U-Back" `
  --interval 60 `
  --notify    # ← これで定期実行のたびに通知される
```

---

## 7. 複数フォルダのバックアップ

U-Back はフォルダ単位で管理します。複数のフォルダをバックアップしたい場合は、`base-dir` を分けて複数タスクを登録します。

### 例: Documents と Projects を別々に管理

```powershell
# Documents 用（1 時間ごと）
python -m uback.cli schedule `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\Documents" `
  --interval 60 --keep 10 --max-age 30

# Projects 用（30 分ごと、より細かく）
python -m uback.cli schedule `
  --source "C:\Users\YourName\Projects" `
  --base-dir "\\192.168.x.x\Backup\Projects" `
  --interval 30 --keep 20 --max-age 60
```

NAS 上のフォルダ構造:

```
\\192.168.x.x\Backup\
├── Documents\
│   ├── 2026-04-19-010000\
│   ├── 2026-04-19-020000\
│   └── ...
└── Projects\
    ├── 2026-04-19-003000\
    ├── 2026-04-19-010000\
    └── ...
```

---

## 8. トラブルシューティング

### NAS に接続できない（終了コード 3）

```
ERROR: NAS is offline or unreachable: \\192.168.x.x\Backup\U-Back
```

**確認手順:**

1. NAS の電源が入っているか確認
2. Windows エクスプローラーで `\\192.168.x.x\Backup` を開けるか確認
3. NAS 管理画面でアカウントのアクセス権限を確認
4. Windows の資格情報マネージャーに NAS のユーザー名・パスワードが保存されているか確認

```powershell
# 資格情報の確認
cmdkey /list | findstr 192.168

# 資格情報の追加（なければ）
cmdkey /add:192.168.x.x /user:nas_username /pass:nas_password
```

---

### 別のプロセスが実行中（終了コード 2）

```
WARNING: Skipping: Backup already running (PID 12345). Lock file: \\..\.uback.lock
```

**原因:** 前回のバックアップが終了していないか、異常終了でロックファイルが残っています。

**対処:**
- タスクマネージャーで `python.exe` プロセスを確認し、バックアップが実行中でないか確認
- 実行中でなければロックファイルを手動で削除:

```powershell
Remove-Item "\\192.168.x.x\Backup\U-Back\.uback.lock"
```

> U-Back は次回起動時にプロセスの生存確認を行い、死んでいれば自動でロックを引き継ぎます。通常は手動削除不要です。

---

### バックアップが遅い

- **初回は必ず全ファイルをコピー**するため時間がかかります。2 回目以降は差分のみです。
- NAS との接続速度（有線 LAN 推奨）を確認してください。
- `--hash` オプションは SHA-256 計算のため通常より遅くなります。mtime+size 比較（デフォルト）で十分です。

---

### `status` の節約量が 0 または不正確

```
NOTE: inode numbers unavailable on this filesystem.
      Savings cannot be accurately measured.
```

**原因:** 一部の SMB/NFS 設定では inode 番号が `0` で報告されます。

**対処:**
- UGREEN NAS の SMB 設定で「UNIX 拡張を有効化」または「inode 番号の通知」を ON にする
- この制限があっても**バックアップ自体は正常に動作**します。表示が不正確になるだけです。

---

## 9. よくある質問

**Q. NAS に保存したファイルを間違えて編集してしまった**  
A. ハードリンクで繋がっているため、NAS 側のファイルを直接上書きすると他のスナップショットのファイルも変わってしまいます。過去のスナップショットを参照する場合は**読み取り専用**として扱ってください。復元したいファイルはローカルにコピーしてから編集してください。

---

**Q. スナップショットから特定のファイルだけ復元したい**  
A. 対象のスナップショットフォルダをエクスプローラーで開き、必要なファイルをローカルにコピーするだけです。

```
\\192.168.x.x\Backup\U-Back\2026-04-18-230000\Documents\report.docx
```

を `C:\Users\YourName\Desktop\` にコピーすれば復元完了です。

---

**Q. バックアップ元に含めたくないフォルダがある**  
A. 現バージョンでは除外リスト機能は未実装です。除外したいフォルダは `--source` を分割して管理してください（例: `Documents` 全体ではなく `Documents\Work` のみ指定）。

---

**Q. `--hash` オプションはいつ使う？**  
A. 通常は mtime+size 比較（デフォルト）で十分です。以下のケースでは `--hash` を検討してください:
- NAS とローカルの間でファイルコピーが多く発生し、mtime がリセットされる環境
- 「内容は同じだが mtime が異なる」ファイルが多い場合

---

**Q. バックアップ実行中に PC をシャットダウンしても大丈夫？**  
A. 途中で中断された場合、その実行分のスナップショットフォルダは不完全な状態になります。次回実行時に自動的に新しいスナップショットが作られます。不完全なフォルダは `status` で `?` マークが付いた状態で表示されます。手動で削除しても構いません。

---

**Q. 複数の PC から同じ NAS フォルダにバックアップしても大丈夫？**  
A. `base-dir` を PC ごとに分けてください。同じ `base-dir` を複数 PC で共有すると、スナップショットの順序が混在して正しく動作しません。

```
\\192.168.x.x\Backup\
├── PC-Work\      ← 仕事用 PC の base-dir
└── PC-Home\      ← 自宅 PC の base-dir
```

---

*最終更新: 2026-04-19 | U-Back v0.1*
