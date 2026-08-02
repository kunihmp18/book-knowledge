# Windows セットアップ

OCRは**手元のPCで動かす**。書籍PDFをクラウドに上げずに済み、
モデルの負担も（OCR分だけ）ローカル計算に逃がせる。

## 目次
- [必要なもの](#必要なもの)
- [winget で入れる](#winget-で入れる)
- [手動で入れる](#手動で入れる)
- [PATH が通らないとき](#path-が通らないとき)
- [動作確認](#動作確認)
- [Windows 固有の注意](#windows-固有の注意)

---

## 必要なもの

| もの | 役割 |
|---|---|
| Python 3.10+ | スクリプト本体 |
| Tesseract OCR + 日本語データ(`jpn`, `jpn_vert`) | 文字認識の本体 |
| Ghostscript | ocrmypdf のPDF処理 |
| ocrmypdf | 上2つを束ねてPDFにテキスト層を付ける |

システムソフトを黙って入れない。以下は**利用者が実行する**手順として提示する。

## winget で入れる

PowerShell（管理者）で:

```powershell
winget install --id UB-Mannheim.TesseractOCR
winget install --id ArtifexSoftware.GhostScript
```

Tesseract のインストーラでは **Additional language data** を開いて
`Japanese` と `Japanese (vertical)` にチェックを入れる。ここを飛ばすと
`jpn` が無く、`book_ocr.py check` が MISS になる。

続いて Python 側:

```powershell
py -3 -m pip install --upgrade pip
py -3 -m pip install ocrmypdf pypdfium2
```

## 手動で入れる

winget が使えない場合:

- Tesseract: UB Mannheim ビルドの `.exe` を入手して実行
  （言語データで Japanese / Japanese vertical を選択）
- Ghostscript: Artifex 配布の Windows 64-bit インストーラ

## PATH が通らないとき

インストール直後は既存のターミナルに PATH が反映されない。
**PowerShell を開き直す**のが先。それでも駄目なら:

```powershell
$env:Path += ";C:\Program Files\Tesseract-OCR;C:\Program Files\gs\gs10.02.1\bin"
```

恒久的に通すなら「システム環境変数の編集」→ Path に同じ2つを追加。
Ghostscript のバージョン番号はインストール先を実際に確認して合わせる。

## 動作確認

```powershell
py -3 .claude\skills\distill-scanned-books\scripts\book_ocr.py check --language jpn+eng
```

すべて `OK` になってから `run` に進む。`MISS` があるまま走らせると、
数十バッチ進んでから失敗して時間を捨てることになる。

## Windows 固有の注意

- `python3` ではなく **`py -3`**（または `python`）
- パスは `"` で囲む。日本語フォルダ名・スペース入りが普通にある
  （例: `"$env:USERPROFILE\Downloads\スタートアップの法務AtoZ"`）
- **PowerShell の行継続はバッククォート `` ` ``**。`\` ではない
- 長いパスで失敗する場合、出力先を `C:\books\<slug>` のように浅くする
- ウイルス対策ソフトが `ocrmypdf` の一時ファイルを掴んで失敗することがある。
  出力先フォルダを除外設定に入れると通ることが多い
- `--jobs` は物理コア数程度に。上げすぎるとメモリを食って途中で落ちる

## 実行例

```powershell
# 1. 本番前に設定を見比べる(必須)
py -3 .claude\skills\distill-scanned-books\scripts\book_ocr.py sample `
  "C:\books\src\startup-legal.pdf" --pages 20-24 --workdir "C:\books\sample"

# 2. 決めた設定で走らせる(中断しても同じコマンドで再開)
py -3 .claude\skills\distill-scanned-books\scripts\book_ocr.py run `
  "C:\books\src\startup-legal.pdf" `
  --output "C:\books\startup-legal" `
  --language jpn+eng --pages-per-batch 40
```
