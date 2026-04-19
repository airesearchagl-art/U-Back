# U-Back — Time Machine for Windows & UGREEN NAS

**U-Back** は、Mac の Time Machine のようなバックアップ体験を Windows 環境で実現する、クリエイター・エンジニア向けのインテリジェントなバックアップツールです。

UGREEN NAS などの SMB 共有フォルダに対して**ハードリンクを活用した増分バックアップ**を行い、ストレージ消費を最小限に抑えながら、過去のあらゆる時点の状態を「フルセット」として保持します。

---

## ✨ 特徴

| 機能 | 詳細 |
|---|---|
| **増分バックアップ** | 変更のないファイルはハードリンクを作成。各スナップショットは独立したフルバックアップとして見えるが、実消費は差分のみ |
| **UGREEN NAS 最適化** | SMB 接続の死活確認、errno/winerror による認証エラー検出、二重起動防止ロック |
| **節約の可視化** | `status` コマンドで仮想総容量と実消費を比較。「ハードリンクで XX GB 節約」をリアルタイム表示 |
| **自動ローテーション** | 最新 N 件の保持 + 日数による古いスナップショットの自動削除 |
| **Windows タスク統合** | `schtasks` への自動登録で定期実行をスケジュール |
| **トースト通知** | `win11toast` による成功/失敗の通知センター表示（オプション） |

---

## 🗂️ スナップショットの仕組み

```
\\NAS\Backup\Documents\
├── 2026-04-17-230000\     ← 初回: 全ファイルをコピー
│   ├── report.docx        (実体: 2.1 MB)
│   └── photo.jpg          (実体: 4.8 MB)
│
├── 2026-04-18-230000\     ← 2回目: 変更なし → ハードリンク
│   ├── report.docx ──┐   (inode 共有: 0 bytes 追加)
│   └── photo.jpg ────┘   (inode 共有: 0 bytes 追加)
│
└── 2026-04-19-230000\     ← 3回目: report.docx だけ変更
    ├── report.docx        (実体: 2.3 MB  ← 新規コピー)
    └── photo.jpg ────────  (inode 共有: 0 bytes 追加)
```

全スナップショットが「フルバックアップに見える」のに、NAS の実消費は差分ファイル分のみです。

---

## 🚀 クイックスタート

### 1. インストール

```bash
git clone https://github.com/airesearchagl-art/U-Back.git
cd U-Back
# 標準ライブラリのみ使用。通知機能を使う場合は追加インストール:
pip install win11toast
```

> **Python 3.11 以上**が必要です。

### 2. 初回バックアップ

```powershell
python -m uback.cli run `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\U-Back" `
  --notify
```

出力例:

```
Snapshot : 2026-04-19-153000
Previous : (none — first backup)
Copied   : 1,622
Linked   : 0
Errors   : 0
Size     : 8.3 MB
Elapsed  : 12.4s
Success  : True
```

### 3. 定期実行のスケジュール登録

1 時間おきにバックアップを自動実行します（管理者権限で実行してください）:

```powershell
python -m uback.cli schedule `
  --source "C:\Users\YourName\Documents" `
  --base-dir "\\192.168.x.x\Backup\U-Back" `
  --interval 60 `
  --keep 10 --max-age 30 `
  --notify
```

登録解除:

```powershell
python -m uback.cli unschedule --base-dir "\\192.168.x.x\Backup\U-Back"
```

---

## 📊 ステータス確認

```powershell
python -m uback.cli status --base-dir "\\192.168.x.x\Backup\U-Back"
```

```
══════════════════════════════════════════════════════════════
  U-Back Status  —  \\192.168.x.x\Backup\U-Back
══════════════════════════════════════════════════════════════

  Snapshots (oldest → newest):

    #  Snapshot                Files    New data  Status
  ──────────────────────────────────────────────────────────
    1  2026-04-17-230000       1,622    8.3 MB    ✓
    2  2026-04-18-230000       1,625    0.2 MB    ✓
    3  2026-04-19-120000       1,626    0.1 MB    ✓

  Latest backup : 2026-04-19-120000  (2 hours ago)
  Total snapshots: 3

  ──────────────────────────────────────────────────────────
  Storage summary:
  ──────────────────────────────────────────────────────────
  Virtual full size........................    24.9 MB
    (as if every snapshot were a full copy)
  Actual NAS usage.........................     8.6 MB
    (hard links share identical blocks)
  ──────────────────────────────────────────────────────────
  Hard link savings : 16.3 MB saved  (65.5%)

  Thanks to hard links, U-Back saved 16.3 MB on your NAS
  — that's 65.5% of the 24.9 MB a naive full-copy
    strategy would have used!
```

---

## 🛠️ CLI リファレンス

### `run` — バックアップ実行

```
python -m uback.cli run --source DIR --base-dir DIR [options]
```

| オプション | 説明 | デフォルト |
|---|---|---|
| `--source DIR` | バックアップ元ディレクトリ | 必須 |
| `--base-dir DIR` | NAS のスナップショット保存先 | 必須 |
| `--keep N` | 常に保持する最新スナップショット数 | なし（ローテーション無効） |
| `--max-age DAYS` | 保持ウィンドウ外のスナップショットを削除する日数 | `30` |
| `--hash` | mtime+size の代わりに SHA-256 で変更検出 | off |
| `--notify` | 完了時に Windows トースト通知を送る | off |

**終了コード:**

| コード | 意味 |
|---|---|
| `0` | 成功 |
| `1` | バックアップエラー |
| `2` | 別プロセスが実行中（スキップ） |
| `3` | NAS 到達不能 / 認証エラー |

### `status` — スナップショット一覧と節約量

```
python -m uback.cli status --base-dir DIR
```

### `schedule` — タスクスケジューラに登録

```
python -m uback.cli schedule --source DIR --base-dir DIR --interval MINUTES [options]
```

`run` と同じオプション（`--keep`, `--max-age`, `--hash`, `--notify`）が使えます。

### `unschedule` — 登録解除

```
python -m uback.cli unschedule --base-dir DIR
```

---

## 📁 プロジェクト構成

```
uback/
├── backup_engine.py   # コアロジック: ハードリンク判定・コピー・再帰走査
├── manager.py         # セッション管理: スナップショット探索・backup_info.json 書き出し・ローテーション
├── lock.py            # PID ベースのアトミックロック（二重起動防止）
├── errors.py          # 例外クラス: BackupError / BackupAlreadyRunningError / NASUnavailableError
├── status.py          # ステータス収集・inode デデュプ・節約量算出
├── notify.py          # win11toast ラッパー（オプション依存）
└── cli.py             # CLI エントリポイント (run / status / schedule / unschedule)
```

---

## ⚠️ 注意事項

- **ファイルシステム:** バックアップ先は Btrfs / EXT4 / XFS など、**ハードリンクをサポートするファイルシステム**が必要です。UGREEN NAS のデフォルト設定（EXT4）は対応しています。
- **ファイルの直接編集禁止:** NAS 上のスナップショット内のファイルを直接編集しないでください。ハードリンクで繋がった他のスナップショットにも影響します。
- **SMB の inode サポート:** 一部のSMB設定では inode 番号が `0` で報告されることがあります。その場合、`status` の節約量計算が正確でない旨がメッセージに表示されます。
- **スケジュール登録:** `schtasks` への登録には管理者権限が必要です。

---

## 🔬 開発・テスト

```bash
pip install pytest
python -m pytest tests/ -v
```

91 テストがすべて通ることを確認してください。

---

## 📄 ライセンス

MIT License
