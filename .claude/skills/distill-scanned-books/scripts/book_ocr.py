#!/usr/bin/env python3
"""スキャン書籍PDFを決定的にOCRし、再開可能なバッチ単位のテキスト層を作る。

このスクリプトは *解釈しない*。OCRとカバレッジ管理だけを担う。
解釈をモデルに任せる前に「全ページが機械的に読めている」ことを保証するのが役目で、
だからこそ乱数も推論も挟まず、同じ入力からは同じ出力が出る作りにしてある。

原本は絶対に書き換えない。OCR成果物も自動では消さない(証拠として残す)。

  check   依存(ocrmypdf/tesseract/ghostscript/言語データ)を検査する
  run     OCRを実行する。中断しても同じコマンドで再開できる
  qc      QCレポートを作り直す

OS非依存。Windows は `py -3`、mac/Linux は `python3`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_VERSION = 1

# 入力はPDFでも、ページ画像を並べたフォルダでもよい。
# スキャナやスマホの書き出しは画像の束であることが多く、
# 「PDFに束ね直してから渡す」という手作業を利用者に強いる理由がない。
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}

# QCの機械的フラグ。ここで拾うのは「明らかに失敗した匂い」だけで、
# 品質の最終判断は人とモデルが原本と突き合わせて行う(SKILL.md の検証手順)。
MIN_CHARS_PER_PAGE = 20      # これ未満は白紙かOCR失敗の疑い
MIN_CJK_RATIO = 0.10         # 日本語指定なのにCJKがほぼ無い = 認識失敗の疑い
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def need_pypdfium2():
    try:
        import pypdfium2
    except ImportError:
        die("pypdfium2 が必要です:\n  python3 -m pip install -r <skill>/requirements.txt")
    import pypdfium2

    return pypdfium2


# ---------------------------------------------------------------- check


def which(name: str) -> str | None:
    return shutil.which(name)


def tesseract_langs() -> list[str]:
    exe = which("tesseract")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--list-langs"], capture_output=True, text=True, timeout=30, check=False
        )
        return [ln.strip() for ln in out.stdout.splitlines()[1:] if ln.strip()]
    except (subprocess.SubprocessError, OSError):
        return []


def cmd_check(a) -> int:
    print("依存の検査")
    ok = True

    for tool, hint in (
        ("ocrmypdf", "python3 -m pip install ocrmypdf"),
        ("tesseract", "OSごとの手順は references/ の setup を参照"),
        ("gs", "Ghostscript。ocrmypdf の最適化・PDF/A変換に使う"),
    ):
        p = which(tool)
        print(f"  {'OK  ' if p else 'MISS'} {tool:<12} {p or '→ ' + hint}")
        ok = ok and bool(p)

    langs = tesseract_langs()
    print(f"  言語データ: {', '.join(langs) if langs else '(取得できず)'}")
    for want in (a.language or "").split("+"):
        want = want.strip()
        if not want:
            continue
        hit = want in langs
        print(f"  {'OK  ' if hit else 'MISS'} {want}")
        ok = ok and hit

    # 日本語の縦書きは専用モデルが要る場面がある。無くても致命的ではないので警告に留める。
    if any(x.startswith("jpn") for x in (a.language or "").split("+")) and "jpn_vert" not in langs:
        print("  warn jpn_vert が無い。縦組みが多い本では認識が落ちることがある")

    try:
        import pypdfium2  # noqa: F401

        print("  OK   pypdfium2")
    except ImportError:
        print("  MISS pypdfium2   → python3 -m pip install pypdfium2")
        ok = False

    print()
    print("すべて満たしています。run に進めます。" if ok else "不足があります。上の MISS を解消してください。")
    return 0 if ok else 1


# ---------------------------------------------------------------- manifest


def natural_key(p: Path):
    """page2.jpg が page10.jpg より先に来るように、数字は数値として比較する。

    ここを辞書順にすると、ページ順が狂ったまま全工程が進み、
    出典ページが全部ずれる。最初に効かせておくべき順序。
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def image_files(d: Path) -> list[Path]:
    files = sorted([p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXTS], key=natural_key)
    if not files:
        die(f"{d} に画像が見つかりません(対応: {', '.join(sorted(IMAGE_EXTS))})")
    return files


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sha256_of_dir(files: list[Path]) -> str:
    """画像フォルダ全体の内容ハッシュ。順序も名前も内容も含める。

    1枚でも差し替わったり並びが変われば別物になる。
    再開時に「同じ入力か」を判定するのが目的なので、そこまで含めないと意味がない。
    """
    h = hashlib.sha256()
    for p in files:
        h.update(p.name.encode("utf-8"))
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    return h.hexdigest()


def load_manifest(out: Path) -> dict | None:
    p = out / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_manifest(out: Path, m: dict):
    """バッチ完了ごとに書き戻す。外から足された項目は消さない。

    OCRは数百ページで数十分かかるので、その間に人やモデルが manifest へ
    注記を足すことが普通に起きる(page_offset、書誌情報、OCRの癖など)。
    メモリ上の dict をそのまま上書きすると、その追記が黙って消える。
    自分が持っていないキーはディスク側を優先して残す。
    """
    path = out / "manifest.json"
    if path.exists():
        try:
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            for k, v in on_disk.items():
                if k not in m:
                    m[k] = v
        except json.JSONDecodeError:
            pass  # 壊れていたら書き直す方が復旧が早い
    m["updated_at"] = now()
    path.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def settings_of(a, force_ocr: bool) -> dict:
    return {
        "language": a.language,
        "pages_per_batch": a.pages_per_batch,
        "force_ocr": force_ocr,
        "deskew": bool(a.deskew),
        "rotate_pages": bool(a.rotate_pages),
    }


# ---------------------------------------------------------------- run


def page_count(pdf: Path) -> int:
    pdfium = need_pypdfium2()
    doc = pdfium.PdfDocument(str(pdf))
    n = len(doc)
    doc.close()
    return n


def split_range(src: Path, start: int, end: int, dst: Path, files: list[Path] | None = None):
    """1..N(1始まり・両端含む)を、OCRにかける1本のPDFとして切り出す。

    原本(PDFでも画像フォルダでも)には触れない。
    画像入力のときは該当ページの画像だけをPDFに束ね直す。
    OCR側の経路をPDFに一本化しておくと、以降の工程が入力形式を意識しなくて済む。
    """
    if files is not None:
        from PIL import Image, ImageOps

        pages = []
        for p in files[start - 1 : end]:
            im = ImageOps.exif_transpose(Image.open(p))  # スマホ写真の回転情報を焼き込む
            pages.append(im.convert("RGB"))
        if not pages:
            die(f"切り出す画像がありません: {start}-{end}")
        pages[0].save(str(dst), save_all=True, append_images=pages[1:], resolution=300)
        for im in pages:
            im.close()
        return

    pdfium = need_pypdfium2()
    doc = pdfium.PdfDocument(str(src))
    new = pdfium.PdfDocument.new()
    new.import_pages(doc, list(range(start - 1, end)))
    new.save(str(dst))
    new.close()
    doc.close()


def ocr_one(src: Path, dst: Path, sidecar: Path, s: dict, jobs: int) -> tuple[bool, str]:
    """OCRして、テキストは sidecar(Tesseractの生出力)に書かせる。

    テキストをOCR済みPDFから再抽出してはいけない。PDFの不可視テキスト層は
    単語ごとに座標付きで置かれるため、抽出器が語を切り貼りして日本語の助詞が落ちる。
    実測: 再抽出だと「共同創業 二名 以上 いる 場合」、sidecarだと
    「共同創業者が二名以上いる場合、」。sidecarが正しい。
    """
    cmd = ["ocrmypdf", "-l", s["language"], "--output-type", "pdf", "--jobs", str(jobs),
           "--sidecar", str(sidecar)]
    # 既にテキストがあるページはスキップする。原文の文字データを潰さないため。
    cmd.append("--force-ocr" if s["force_ocr"] else "--skip-text")
    # 画像変換は既定で入れない。deskew は実測でOCRを壊すことがある
    # (「第1章 創業株主間契約の設計」→「第1章 ARE MAH ORE」)。
    # 必要かどうかは `sample` で見比べてから決める。
    if s["deskew"]:
        cmd.append("--deskew")
    if s["rotate_pages"]:
        cmd += ["--rotate-pages", "--rotate-pages-threshold", "12"]
    cmd += [str(src), str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return r.returncode == 0, (r.stderr or r.stdout)[-1500:]


def read_sidecar(sidecar: Path, first_page_no: int, expected: int) -> list[tuple[int, str]]:
    """sidecarを改ページ(\\f)で割り、スキャン通し番号を振る。

    ページ数が合わないまま進めると、以降の全引用のページ番号がずれる。
    ずれた出典は検証できないので、ここで落とす。
    """
    raw = sidecar.read_text(encoding="utf-8", errors="replace")
    parts = raw.split("\f")

    # 末尾の空要素を無条件に削ってはいけない。Tesseract は改ページをページ *間* にだけ
    # 入れるので、Nページなら通常ちょうどN要素になる。最終ページが白紙(巻末の白紙など)だと
    # 最後の要素も空になり、「余分な区切り」と見分けがつかない。
    # 数が1つ多いときだけ末尾を落とす。それ以外は落とさずに検査へ回す。
    if len(parts) == expected + 1 and not parts[-1].strip():
        parts = parts[:-1]

    if len(parts) != expected:
        raise ValueError(
            f"sidecarのページ数が合いません(期待{expected} / 実際{len(parts)})。"
            "ページ番号がずれるため中断します。"
        )
    return [(first_page_no + i, t) for i, t in enumerate(parts)]


def write_batch_md(path: Path, batch_id: str, pages: list[tuple[int, str]]):
    lines = [
        f"# {batch_id}",
        "",
        "<!-- 生OCR。不変層。ここを直さない。訂正は analysis/corrections.md に書く。 -->",
        "",
    ]
    for pno, text in pages:
        lines.append(f"## [scan p.{pno:04d}]")
        lines.append("")
        lines.append(text.strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def cmd_run(a) -> int:
    src = Path(a.source).expanduser().resolve()
    out = Path(a.output).expanduser().resolve()

    if not src.exists():
        die(f"入力が見つかりません: {src}")
    if src.is_dir():
        kind = "images"
        files = image_files(src)
        total_bytes = sum(p.stat().st_size for p in files)
    elif src.suffix.lower() == ".pdf":
        kind, files = "pdf", None
        total_bytes = src.stat().st_size
    else:
        die(f"PDF または画像フォルダを指定してください: {src}")
    if out == src.parent or (src.is_dir() and out == src):
        die("出力先を原本と同じフォルダにしないでください(原本保護のため)")

    existing = load_manifest(out)
    if out.exists() and any(out.iterdir()) and existing is None:
        die(f"出力先が空でなく manifest.json もありません: {out}\n"
            "別のフォルダを指定してください(既存の成果物を壊さないため)")

    print(f"入力のハッシュを計算中… ({total_bytes / 1024 / 1024:.1f}MB"
          f"{f' / 画像{len(files)}枚' if files else ''})")
    digest = sha256_of_dir(files) if files else sha256_of(src)
    settings = settings_of(a, a.force_ocr)

    if existing:
        # 設定やソースが違うのに同じフォルダへ足すと、由来の違う成果物が混ざる。
        # 混ざったOCR層は後から分離できないので、ここで止める。
        if existing["source"]["sha256"] != digest:
            die("manifest の入力ハッシュが今回の原本と一致しません。\n"
                f"  manifest: {existing['source']['sha256'][:16]}…\n"
                f"  今回    : {digest[:16]}…\n"
                "別の出力フォルダを使ってください。")
        if existing["settings"] != settings:
            diff = {k: (existing["settings"].get(k), v) for k, v in settings.items()
                    if existing["settings"].get(k) != v}
            die(f"manifest の設定と一致しません(既存, 今回): {diff}\n"
                "言語・バッチ幅・force-ocr を変えるときは新しい出力フォルダを使ってください。")
        m = existing
        print(f"再開: {out}")
    else:
        n = len(files) if files else page_count(src)
        batches = []
        for start in range(1, n + 1, a.pages_per_batch):
            end = min(start + a.pages_per_batch - 1, n)
            batches.append({
                "id": f"part-{start:04d}-{end:04d}", "start": start, "end": end,
                "status": "pending", "chars": 0, "text": None, "ocr_pdf": None,
                "completed_at": None, "error": None,
            })
        m = {
            "version": MANIFEST_VERSION,
            "source": {"path": str(src), "kind": kind, "sha256": digest,
                       "bytes": total_bytes, "pages": n},
            "settings": settings,
            "status": "partial",
            "batches": batches,
            "created_at": now(), "updated_at": now(),
        }
        for sub in ("text/batches", "ocr", "reports", "analysis"):
            (out / sub).mkdir(parents=True, exist_ok=True)
        save_manifest(out, m)
        print(f"新規: {out} / 全{n}ページ / {len(batches)}バッチ")

    todo = [b for b in m["batches"] if b["status"] != "complete"]
    if a.limit:
        todo = todo[: a.limit]
    print(f"未処理 {len([b for b in m['batches'] if b['status'] != 'complete'])}バッチ"
          f" / 今回 {len(todo)}バッチ を処理します")

    tmpdir = Path(tempfile.mkdtemp(prefix="bookocr-"))
    try:
        for i, b in enumerate(todo, 1):
            bid = b["id"]
            print(f"[{i}/{len(todo)}] {bid} (p.{b['start']}–{b['end']}) … ", end="", flush=True)
            part_src = tmpdir / f"src-{bid}.pdf"
            part_ocr = out / "ocr" / f"{bid}.pdf"
            try:
                split_range(src, b["start"], b["end"], part_src, files)
            except Exception as e:  # noqa: BLE001 - PDFの壊れ方は一定でない
                b.update(status="failed", error=f"split: {e}")
                save_manifest(out, m)
                print(f"NG(分割失敗: {e})")
                continue

            sidecar = out / "ocr" / f"{bid}.txt"
            ok, log = ocr_one(part_src, part_ocr, sidecar, m["settings"], a.jobs)
            if not ok:
                b.update(status="failed", error=log)
                save_manifest(out, m)
                print("NG(OCR失敗)")
                print(f"       {log.splitlines()[-1] if log else ''}")
                continue

            try:
                pages = read_sidecar(sidecar, b["start"], b["end"] - b["start"] + 1)
            except ValueError as e:
                b.update(status="failed", error=str(e))
                save_manifest(out, m)
                print(f"NG({e})")
                continue
            md = out / "text" / "batches" / f"{bid}.md"
            write_batch_md(md, bid, pages)
            chars = sum(len(t.strip()) for _, t in pages)
            b.update(status="complete", chars=chars, text=f"text/batches/{bid}.md",
                     ocr_pdf=f"ocr/{bid}.pdf", completed_at=now(), error=None)
            save_manifest(out, m)
            print(f"OK ({chars:,}字)")
            part_src.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    done = [b for b in m["batches"] if b["status"] == "complete"]
    m["status"] = "complete" if len(done) == len(m["batches"]) else "partial"
    save_manifest(out, m)

    if m["status"] == "complete":
        build_full_md(out, m)
    build_qc(out, m)

    print()
    print(f"状態: {m['status']}  ({len(done)}/{len(m['batches'])} バッチ完了)")
    if m["status"] != "complete":
        failed = [b["id"] for b in m["batches"] if b["status"] == "failed"]
        if failed:
            print(f"失敗: {', '.join(failed)}  → 同じコマンドを再実行すれば再試行します")
        print("すべて complete になるまで、本全体についての結論は書かないこと。")
    return 0


def build_full_md(out: Path, m: dict):
    parts = ["# 全文(生OCR)", "", "<!-- 検索・エクスポート用。解析の入力は text/batches/*.md を使う。 -->", ""]
    for b in m["batches"]:
        if b["text"]:
            parts.append((out / b["text"]).read_text(encoding="utf-8"))
    (out / "text" / "full.md").write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------- qc


def page_blocks(md: str) -> list[tuple[int, str]]:
    blocks = []
    cur, buf = None, []
    for line in md.splitlines():
        mm = re.match(r"^## \[scan p\.(\d+)\]", line)
        if mm:
            if cur is not None:
                blocks.append((cur, "\n".join(buf).strip()))
            cur, buf = int(mm.group(1)), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        blocks.append((cur, "\n".join(buf).strip()))
    return blocks


def build_qc(out: Path, m: dict):
    want_cjk = any(x.startswith("jpn") for x in m["settings"]["language"].split("+"))
    flagged, total_chars, total_pages = [], 0, 0

    for b in m["batches"]:
        if not b["text"]:
            continue
        for pno, text in page_blocks((out / b["text"]).read_text(encoding="utf-8")):
            total_pages += 1
            n = len(text)
            total_chars += n
            reasons = []
            if n < MIN_CHARS_PER_PAGE:
                reasons.append(f"文字数{n}(白紙かOCR失敗の疑い)")
            elif want_cjk:
                ratio = len(CJK_RE.findall(text)) / max(n, 1)
                if ratio < MIN_CJK_RATIO:
                    reasons.append(f"CJK比率{ratio:.0%}(日本語として認識できていない疑い)")
            if reasons:
                flagged.append((pno, b["id"], "; ".join(reasons)))

    done = [b for b in m["batches"] if b["status"] == "complete"]
    lines = [
        "# OCR QCレポート",
        "",
        f"生成: {now()}",
        f"状態: **{m['status']}**  ({len(done)}/{len(m['batches'])} バッチ)",
        f"設定: {m['settings']}",
        "",
        "## カバレッジ",
        "",
        f"- 原本ページ数: {m['source']['pages']}",
        f"- テキスト化済みページ: {total_pages}",
        f"- 総文字数: {total_chars:,}",
        f"- 1ページ平均: {total_chars // max(total_pages, 1):,}字",
        "",
        "## 機械的に怪しいページ",
        "",
        "ここに挙がったページは **必ず原本と目視で突き合わせる**。",
        "白紙・図版のみのページなら問題なし、という判断もここで下す。",
        "",
    ]
    if flagged:
        lines += [f"{len(flagged)}件 / {total_pages}ページ中", "", "| scanページ | バッチ | 理由 |", "|---|---|---|"]
        lines += [f"| [scan p.{p:04d}] | {bid} | {why} |" for p, bid, why in flagged]
    else:
        lines.append("機械的なフラグはなし。ただし**これは品質の保証ではない**。")
    lines += [
        "",
        "## 人が確認するページ(SKILL.mdの検証手順)",
        "",
        "- 最初の本文ページ / 中間の1ページ / 最後の本文ページ",
        "- 上の表の全ページ",
        "- 表・数式・脚注・図版・縦書きの代表例",
        "",
        "## 訂正の置き場",
        "",
        "`text/batches/*.md` は不変層なので直接編集しない。",
        "訂正は `analysis/corrections.md` に scanページ番号付きで記録する。",
        "",
    ]
    (out / "reports").mkdir(parents=True, exist_ok=True)
    (out / "reports" / "ocr-qc.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"QC: フラグ {len(flagged)}件 → reports/ocr-qc.md")


def cmd_sample(a) -> int:
    """本番前に設定を決めるための比較。

    500ページを回してから「縦組みが読めていない」と気づくと全部やり直しになる。
    数ページで先に見比べるほうが、ほぼ常に安い。
    """
    src = Path(a.source).expanduser().resolve()
    if not src.exists():
        die(f"入力が見つかりません: {src}")
    files = image_files(src) if src.is_dir() else None
    mm = re.fullmatch(r"(\d+)\s*-\s*(\d+)", a.pages.strip())
    if not mm:
        die("--pages は 1-4 の形式で指定してください。")
    start, end = int(mm.group(1)), int(mm.group(2))

    work = Path(a.workdir).expanduser().resolve() if a.workdir else Path(tempfile.mkdtemp(prefix="ocrsample-"))
    work.mkdir(parents=True, exist_ok=True)
    part = work / "sample-src.pdf"
    split_range(src, start, end, part, files)

    combos = []
    for lang in a.languages:
        combos.append((f"{lang}", {"language": lang, "force_ocr": False, "deskew": False, "rotate_pages": False}))
        combos.append((f"{lang}+deskew", {"language": lang, "force_ocr": False, "deskew": True, "rotate_pages": False}))

    print(f"p.{start}–{end} を {len(combos)}通りで比較します → {work}\n")
    results = []
    for name, s in combos:
        tag = name.replace("+", "_")
        side = work / f"{tag}.txt"
        ok, log = ocr_one(part, work / f"{tag}.pdf", side, s, a.jobs)
        if not ok:
            print(f"[{name}] 失敗: {log.splitlines()[-1] if log else ''}")
            continue
        text = side.read_text(encoding="utf-8", errors="replace")
        chars = len(re.sub(r"\s", "", text))
        cjk = len(CJK_RE.findall(text))
        results.append((name, chars, cjk / max(chars, 1), side))
        print(f"[{name}] 文字数{chars:,} / CJK比率{cjk / max(chars, 1):.0%}")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        print(f"    冒頭: {first[:60]}")

    print()
    print("数字だけで決めないこと。文字数が多くても誤認識の羅列ということがある。")
    print("上の『冒頭』と、下のファイルを原本と目視で突き合わせてから run の設定を決める:")
    for name, _, _, side in results:
        print(f"  {name:<20} {side}")
    return 0


def cmd_pages(a) -> int:
    """補正・確認のために、原本ページを読める大きさの画像として書き出す。

    生OCRは文や節を落とすことがあるので、補正するには結局ページを見るしかない。
    毎回フル解像度を開くと重いだけなので、長辺を落としたJPEGに揃えておく。
    原本には触れない。
    """
    Image, ImageOps = None, None
    try:
        from PIL import Image, ImageOps
    except ImportError:
        die("Pillow が必要です:\n  python3 -m pip install Pillow")

    src = Path(a.source).expanduser().resolve()
    out = Path(a.outdir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    mm = re.fullmatch(r"(\d+)\s*-\s*(\d+)", a.pages.strip())
    if not mm:
        die("--pages は 1-10 の形式で指定してください。")
    start, end = int(mm.group(1)), int(mm.group(2))

    files = image_files(src) if src.is_dir() else None
    written = []
    for p in range(start, end + 1):
        dst = out / f"p{p:04d}.jpg"
        if dst.exists() and not a.force:
            written.append(dst)
            continue
        if files:
            if p > len(files):
                break
            im = ImageOps.exif_transpose(Image.open(files[p - 1]))
        else:
            pdfium = need_pypdfium2()
            doc = pdfium.PdfDocument(str(src))
            if p > len(doc):
                doc.close()
                break
            page = doc[p - 1]
            w, h = page.get_size()
            im = page.render(scale=a.long_side / max(w, h)).to_pil()
            doc.close()
        if max(im.size) > a.long_side:
            r = a.long_side / max(im.size)
            im = im.resize((round(im.width * r), round(im.height * r)), Image.LANCZOS)
        im.convert("RGB").save(dst, "JPEG", quality=a.quality, optimize=True)
        written.append(dst)

    total = sum(p.stat().st_size for p in written) / 1024 / 1024
    print(f"p.{start}–{end} → {out} ({len(written)}枚 / {total:.1f}MB)")
    return 0


def cmd_qc(a) -> int:
    out = Path(a.output).expanduser().resolve()
    m = load_manifest(out)
    if not m:
        die(f"manifest.json がありません: {out}")
    build_qc(out, m)
    if m["status"] == "complete":
        build_full_md(out, m)
    return 0


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="依存を検査する")
    p.add_argument("--language", default="jpn+eng")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="OCRを実行/再開する")
    p.add_argument("source")
    p.add_argument("--output", required=True)
    p.add_argument("--language", default="jpn+eng")
    p.add_argument("--pages-per-batch", type=int, default=40)
    p.add_argument("--force-ocr", action="store_true",
                   help="既存テキスト層が壊れている場合のみ。既定は --skip-text")
    # 画像変換は既定OFF。入れるかどうかは sample で見比べてから決める。
    p.add_argument("--deskew", action="store_true",
                   help="傾き補正。OCRを壊すことがあるので sample で確認してから使う")
    p.add_argument("--rotate-pages", action="store_true", help="90度単位の向き補正")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--limit", type=int, help="今回処理するバッチ数の上限(様子見用)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("sample", help="設定違いを数ページで見比べる(本番前に必ず)")
    p.add_argument("source")
    p.add_argument("--pages", default="1-4", help="比較に使うページ範囲(既定 1-4)")
    p.add_argument("--languages", nargs="+", default=["jpn+eng", "jpn_vert+eng"])
    p.add_argument("--workdir", help="比較結果の置き場(既定: 一時フォルダ)")
    p.add_argument("--jobs", type=int, default=4)
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("pages", help="原本ページを読める大きさの画像にする(補正・確認用)")
    p.add_argument("source", help="PDF または画像フォルダ")
    p.add_argument("--pages", required=True, help="1-10 のような範囲")
    p.add_argument("--outdir", required=True)
    p.add_argument("--long-side", type=int, default=1250)
    p.add_argument("--quality", type=int, default=80)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_pages)

    p = sub.add_parser("qc", help="QCレポートを作り直す")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_qc)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
