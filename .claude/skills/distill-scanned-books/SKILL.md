---
name: distill-scanned-books
description: Convert long scanned book PDFs into restartable, page-cited OCR text and progressively distill every page into faithful summaries, critiques, and business applications. Use when a user uploads or points to a scanned or image-only PDF and asks to OCR it, split it, resume interrupted processing, quality-check it, read the whole book, summarize or critique it, extract business ideas, or prepare private text for search/RAG—especially for long Japanese books that cannot be handled reliably in one context window.
---

# Distill Scanned Books

書籍を、不変のOCR層・検証済みの読了台帳・段階的に圧縮された知識に変える。
OCRは決定的なツールでローカルに回し、**カバレッジと品質の検査を通してから**
モデルの解釈に進む。

## 作業の境界を守る

- スキャン(またはMFPの書き出し)までは利用者の手作業。ここが引き継ぎ点
- PDFに手元またはワークスペースからアクセスできるなら、ローカルOCRの手順を使う
- **画像だけのPDFをチャットに上げればOCRされる、と約束しない。**
  PDFの取り込みはページ画像を捨ててテキストだけを残すことがあり、
  スキャン書籍では何も読めないまま「読めた顔」になる
- APIキーは要らない。モデルの解析は利用者のプラン枠を消費し、
  OCRmyPDF と Tesseract はローカルの計算資源を消費する
- ブラウザのチャットしか無く、PDFにテキスト層が無いなら、
  「先にローカルOCRかページ画像化が要る」と説明する
- プランや上限が結果を左右する場面では、そのつど公式ドキュメントを確認する。
  `references/` に書いた環境情報は恒久的な保証ではない

## 原本と権利を守る

- **原本PDFを変更・上書き・移動・削除しない**
- OCRの成果物を自動で消さない。整理したい場合はQCの後に明示的な承認を取る
- 著作権のあるOCR全文は非公開のまま扱う。公開リポジトリに置かない、
  公開される成果物に貼らない
- 解釈はページ参照付きの言い換えで残す。引用は必要最小限
- 事業上の機密文脈は、共有しうる読書ノートとは別に置く

## OCRを始める・再開する

1. 原本一つと、専用の出力フォルダ一つを決める。入力が曖昧なとき、
   出力先が空でないのに manifest が無いときは、進めずに止める

   入力は **PDF でも、ページ画像を並べたフォルダでもよい**
   （`.jpg/.jpeg/.png/.webp/.tif/.tiff/.bmp`）。画像入力のときは:
   - ファイル名を**自然順**で並べる(`page2.jpg` は `page10.jpg` より前)。
     ここが狂うと以降の出典ページが全部ずれる
   - EXIFの回転情報を焼き込んでからOCRに渡す(スマホ撮影対策)
   - バッチ単位でPDFに束ね直してからOCRする。以降の工程は入力形式を意識しない

2. 依存を検査する:

   ```bash
   python3 .claude/skills/distill-scanned-books/scripts/book_ocr.py check --language jpn+eng
   ```

   失敗したら `references/windows-setup.md`（mac は `references/macos-setup.md`）。
   **システムソフトを黙って入れない。**

3. **本番前に必ず数ページで見比べる**:

   ```bash
   python3 .claude/skills/distill-scanned-books/scripts/book_ocr.py sample \
     "/path/to/source.pdf" --pages 20-24 --workdir "/path/to/sample"
   ```

   - 日本語書籍は `jpn+eng` から始める。Tesseractの日本語モデルは縦組みにも
     ある程度対応する
   - 縦組み主体でうまくいかないときだけ `jpn_vert+eng` を比べる
   - **文字数だけで決めない。** 誤認識の羅列は文字数が増えることがある。
     CJK比率と冒頭行を原本と突き合わせて選ぶ
   - `--deskew` は既定でOFF。実測でOCRを壊すことがある
     （「第1章 創業株主間契約の設計」→「第1章 ARE MAH ORE」）。
     傾いたスキャンで効く場合だけ、sample で確認してから付ける

4. 40ページのバッチで走らせる:

   ```bash
   # PDF入力
   python3 .claude/skills/distill-scanned-books/scripts/book_ocr.py run \
     "/path/to/source.pdf" \
     --output "/path/to/book-output" \
     --language jpn+eng \
     --pages-per-batch 40

   # 画像フォルダ入力(同じコマンド。フォルダを渡すだけ)
   python3 .claude/skills/distill-scanned-books/scripts/book_ocr.py run \
     "/path/to/scans/<slug>" \
     --output "/path/to/book-output" \
     --language jpn+eng \
     --pages-per-batch 40
   ```

5. 長時間走るので進捗を流す。ライブセッションが取れるなら、
   少なくとも毎分1回は利用者に状況を伝える

6. 中断や範囲の失敗のあとは、**同じコマンドをそのまま再実行する**。
   完了済みバッチは再利用され、未完了だけが再試行される

7. `--force-ocr` は既存のテキスト層が壊れて使えないときだけ。
   言語・バッチ幅・force-ocr を変えるときは**新しい出力フォルダ**を使う
   （スクリプトが manifest と照合して、違えば止める）

## 解釈の前にOCRを検証する

1. `manifest.json` が `complete` であること。`partial` のあいだは
   本全体についての主張を書かない

2. `reports/ocr-qc.md` を読み、機械的にフラグの付いたページを**全部**見る

3. 原本PDFとOCRテキストを突き合わせる:
   - 最初の本文ページ
   - 中間の1ページ
   - 最後の本文ページ
   - フラグの付いた全ページ
   - 表・数式・脚注・図版・縦書きの代表例

4. ページ画像をレンダリングして目で比べる。ローカルでの目視確認ができない、
   または曖昧さが残るときだけ、利用者にスポットチェックを頼む

5. 訂正は派生ファイル(`analysis/corrections.md`)に記録する。
   **`text/batches/*.md` を黙って書き換えない** — ここは不変の生OCR層

6. 出典は `[scan p.0123]` で引く。本に印刷されたノンブルとの対応は
   **照合できたときだけ**併記する。PDFの通し番号が本のページ番号と同じ、
   と決して仮定しない

## 全ページの蒸留を実行する

解析の前に `references/distillation-method.md` を読む。

1. 進捗を確認する:

   ```bash
   python3 .claude/skills/distill-scanned-books/scripts/distill_state.py status "/path/to/book-output" --verbose
   ```

2. **未処理のOCRバッチを1つずつ**処理する。バッチ全体を読み、
   不明な箇所は原本ページに戻り、L1カードを書き、**すぐに完了を記録する**

3. 検索や取得を、全バッチを一度は通す第一パスの代わりにしない

4. すべての出力で四つの声を分ける:
   `【著者】`明示的な主張 / `【推論】`こちらの推論 /
   `【評価】`批判的評価 / `【判断】`利用者の事業判断

5. 実質的な主張と、そこから導いた案には、必ずページ参照を残す

6. **有用な事業示唆がゼロでもよい。** 件数のノルマを置かず、
   弱い関連を水増ししない

7. 上位の層は、下位の層が埋まってから作る。ただし決定的な主張・矛盾・
   落とした論点については生OCRに戻って確認する

8. 1ユニットごとに台帳を更新する:

   ```bash
   python3 .claude/skills/distill-scanned-books/scripts/distill_state.py set \
     "/path/to/book-output" \
     --stage l1_batch_cards \
     --unit part-0001-0040 \
     --status complete \
     --output analysis/l1/part-0001-0040.md
   ```

9. 上位ステージを作る前に、そのユニットを全部登録してから1つずつ埋める:

   ```bash
   python3 .claude/skills/distill-scanned-books/scripts/distill_state.py add-stage \
     "/path/to/book-output" \
     --stage l2_chapters \
     --units chapter-01 chapter-02
   ```

## 再利用できるテキストとして束ねる

- `text/batches/*.md` を一次のコーパスにする。40ページ単位なら
  500ページの本でも13ファイル程度に収まる
- `text/full.md` はローカル全文検索と書き出し用。
  一発解析の唯一の入力にはしない
- `manifest.json` / `reports/ocr-qc.md` / `analysis/state.json` を
  テキストの隣に置く。カバレッジと由来が後から監査できる状態を保つ
- 派生層は `analysis/l1` 〜 `analysis/l6` に置く
- OCR全文をコミットするのは、利用者がその保管方式を明示的に選んだ
  **非公開リポジトリ**に限る。生成物と原本PDFは、依頼がなければGitに入れない

## 止まる条件

次の場合は進めず、詰まった箇所をそのまま報告する:

- 原本にアクセスできない、またはまだコピー中
- 日本語の言語データが無い
- manifest の入力ハッシュや設定が、今回の再開の指定と違う
- 代表ページで見てOCR品質が明らかに信用できない
- 手書き・数式・複雑な表・特殊な縦組みが主体で、選んだOCR設定の射程外
- 事業への適用を求められたが、使える事業文脈が無い

事業文脈が無い場合は、中立な L1〜L4 まで進めて、**L5の手前で止まる**。
文脈を捏造して埋めない。
