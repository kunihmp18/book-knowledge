# macOS セットアップ

（現在の主環境は Windows です。`references/windows-setup.md` を参照してください。
このファイルは mac に移ったとき用。）

## 必要なもの

| もの | 役割 |
|---|---|
| Python 3.10+ | スクリプト本体 |
| Tesseract OCR + `jpn` / `jpn_vert` | 文字認識の本体 |
| Ghostscript | ocrmypdf のPDF処理 |
| ocrmypdf | 上2つを束ねてPDFにテキスト層を付ける |

システムソフトを黙って入れない。以下は**利用者が実行する**手順として提示する。

## Homebrew

```bash
brew install tesseract tesseract-lang ghostscript
python3 -m pip install ocrmypdf pypdfium2
```

`tesseract-lang` に `jpn` と `jpn_vert` が含まれる。個別に落とす場合は
tessdata の `jpn.traineddata` / `jpn_vert.traineddata` を
`$(brew --prefix)/share/tessdata/` に置く。

## 動作確認

```bash
python3 .claude/skills/distill-scanned-books/scripts/book_ocr.py check --language jpn+eng
tesseract --list-langs   # jpn, jpn_vert があること
```

## 注意

- Apple Silicon で Rosetta 経由の Python を使うと ocrmypdf が遅い。
  ネイティブの Python を使う
- `--jobs` は性能コア数程度に
- プレビュー.app で書き出した PDF は既にテキスト層を持つことがある。
  その場合 `--skip-text`（既定）が効いて OCR がスキップされる。
  テキスト層が壊れているなら `--force-ocr`

## 環境情報の鮮度について

料金・プラン・製品仕様は変わる。
利用可否や上限が結果を左右する場面では、ここの記述を信じず、
そのつど公式のドキュメントを確認すること。ここに書いてあるのは
**インストール手順**であって、恒久的な保証ではない。
