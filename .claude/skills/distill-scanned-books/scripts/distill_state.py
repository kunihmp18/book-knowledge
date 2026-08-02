#!/usr/bin/env python3
"""蒸留(L1〜L6)の進捗台帳。

なぜ台帳が要るか: 500ページの本を読み切るには何十回もの往復がかかり、
途中でセッションが切れる。「どのバッチをまだ読んでいないか」を人の記憶や
会話履歴に置くと必ず抜けが出る。抜けたまま「この本によれば」と書くのが
一番まずい失敗なので、カバレッジを機械が持つ。

  status     進捗を出す(--verbose で全ユニット)
  set        ユニットの状態を更新する
  add-stage  上位ステージとそのユニットを登録する

OS非依存。Windows は `py -3`、mac/Linux は `python3`。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1
STATUSES = ("pending", "in_progress", "complete", "skipped")

# 上の層は下の層のカバレッジの上にしか立たない。順序はここで固定する。
#
# l0 が最初にあるのは、日本語のスキャン本では生OCRをそのまま読解の土台にできないため。
# OCRは文字を間違えるだけでなく、節や文を丸ごと落とすことがあり、
# しかも残った文章が自然に読めるので欠落に気づけない(実測: 印刷p.425で2文が消失)。
# 画像と突き合わせた補正層を先に作り、以降の層はそれを読む。
STAGE_ORDER = [
    "l0_corrected_text",
    "l1_batch_cards",
    "l2_chapters",
    "l3_book_structure",
    "l4_critique",
    "l5_business_application",
    "l6_decisions",
]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def state_path(out: Path) -> Path:
    return out / "analysis" / "state.json"


def load_manifest(out: Path) -> dict:
    p = out / "manifest.json"
    if not p.exists():
        die(f"manifest.json がありません: {out}\n先に book_ocr.py run を実行してください。")
    return json.loads(p.read_text(encoding="utf-8"))


def load_state(out: Path, manifest: dict) -> dict:
    p = state_path(out)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    # L1のユニットはOCRのバッチと1対1。ここを人が手で並べると取りこぼすので機械が作る。
    units = [
        {"id": b["id"], "status": "pending", "output": None, "note": None, "updated_at": None}
        for b in manifest["batches"]
    ]
    st = {
        "version": STATE_VERSION,
        "source_sha256": manifest["source"]["sha256"],
        "stages": [{"name": "l1_batch_cards", "units": units}],
        "created_at": now(),
        "updated_at": now(),
    }
    save_state(out, st)
    return st


def save_state(out: Path, st: dict):
    st["updated_at"] = now()
    p = state_path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_stage(st: dict, name: str) -> dict | None:
    return next((s for s in st["stages"] if s["name"] == name), None)


def ordered_stages(st: dict) -> list[dict]:
    """登録順ではなく層の順で並べる。

    L1を先に登録したあとでL0(補正層)を足す、という順序は普通に起きる。
    表示と「次にやること」が登録順のままだと、下の層を飛ばして上を作ってしまう。
    """
    def key(s):
        name = s["name"]
        return (STAGE_ORDER.index(name) if name in STAGE_ORDER else len(STAGE_ORDER), name)

    return sorted(st["stages"], key=key)


def unit_range(unit_id: str) -> tuple[int, int] | None:
    """ユニットIDからページ範囲を読む。`p0221-0230` `part-0201-0240` のような形。

    範囲が読めるなら、依存は範囲の重なりで判定できる。
    章単位(`chapter-01`)のように範囲を持たないIDは None を返す。
    """
    m = re.search(r"(\d{3,5})\s*-\s*(\d{3,5})", unit_id)
    return (int(m.group(1)), int(m.group(2))) if m else None


def lower_gap(st: dict, stage_name: str, unit_id: str) -> str | None:
    """この1ユニットが依存する下位ユニットのうち、未処理のものを返す。

    ステージ全体の完了を求めると、目的の章だけ縦に掘る進め方ができない。
    知りたいのは「このページ範囲の下地が埋まっているか」だけなので、
    範囲が読めるときは重なりで判定する。読めないときだけステージ全体を見る。
    """
    if stage_name not in STAGE_ORDER:
        return None
    rng = unit_range(unit_id)
    for lower in STAGE_ORDER[: STAGE_ORDER.index(stage_name)]:
        ls = find_stage(st, lower)
        if not ls:
            continue
        if rng is None or any(unit_range(u["id"]) is None for u in ls["units"]):
            d, t = coverage(ls)
            if d < t:
                return f"{lower} が未完了 ({d}/{t})"
            continue
        missing = [
            u["id"] for u in ls["units"]
            if u["status"] not in ("complete", "skipped")
            and unit_range(u["id"])[0] <= rng[1] and unit_range(u["id"])[1] >= rng[0]
        ]
        if missing:
            return f"{lower} の {', '.join(missing[:5])}{' 他' if len(missing) > 5 else ''} が未処理"
    return None


def coverage(stage: dict) -> tuple[int, int]:
    done = len([u for u in stage["units"] if u["status"] in ("complete", "skipped")])
    return done, len(stage["units"])


def cmd_status(a) -> int:
    out = Path(a.output).expanduser().resolve()
    m = load_manifest(out)
    st = load_state(out, m)

    ocr_done = len([b for b in m["batches"] if b["status"] == "complete"])
    print(f"出力先: {out}")
    print(f"原本  : {Path(m['source']['path']).name} / {m['source']['pages']}ページ")
    print(f"OCR   : {m['status']}  ({ocr_done}/{len(m['batches'])} バッチ)")
    if m["status"] != "complete":
        print("        ⚠ OCRが未完了。本全体についての結論はまだ書かない。")
    print()

    for stage in ordered_stages(st):
        done, total = coverage(stage)
        bar = "#" * round(20 * done / max(total, 1))
        print(f"{stage['name']:<26} {done:>3}/{total:<3} [{bar:<20}]")
        if a.verbose:
            for u in stage["units"]:
                mark = {"complete": "✓", "in_progress": "…", "skipped": "-", "pending": " "}[u["status"]]
                print(f"    [{mark}] {u['id']:<22} {u['output'] or ''}")

    print()
    nxt = None
    for stage in ordered_stages(st):
        pend = [u for u in stage["units"] if u["status"] == "pending"]
        if pend:
            nxt = (stage["name"], pend[0]["id"])
            break
    if nxt:
        print(f"次にやること: {nxt[0]} / {nxt[1]}")
        if nxt[0] == "l1_batch_cards":
            b = next((b for b in m["batches"] if b["id"] == nxt[1]), None)
            if b and b["text"]:
                print(f"  読む対象: {out / b['text']}  (p.{b['start']}–{b['end']})")
    else:
        print("登録済みのユニットはすべて処理済み。次の層を add-stage で登録する。")
    return 0


def cmd_set(a) -> int:
    out = Path(a.output).expanduser().resolve()
    m = load_manifest(out)
    st = load_state(out, m)

    stage = find_stage(st, a.stage)
    if not stage:
        die(f"ステージ {a.stage} は未登録です。先に add-stage で登録してください。")
    unit = next((u for u in stage["units"] if u["id"] == a.unit), None)
    if not unit:
        die(f"ユニット {a.unit} が {a.stage} にありません。")

    if a.status == "complete":
        # 完了と言うからには成果物がある。無いまま complete にすると台帳が嘘になる。
        if not a.output_path:
            die("--output で成果物のパスを指定してください(completeには成果物が要る)")
        if not (out / a.output_path).exists():
            die(f"成果物が見つかりません: {out / a.output_path}")
        # 下の層が埋まっていないのに上を完了扱いにすると、読んでいない箇所を
        # 読んだことにしてしまう。ただし判定はこのユニットのページ範囲に限る
        # (目的の章だけ縦に掘る進め方を潰さないため)。
        gap = lower_gap(st, a.stage, a.unit)
        if gap:
            die(f"このユニットの下地が埋まっていません: {gap}\n"
                "先に該当範囲の下位ユニットを済ませてください。")

    unit.update(status=a.status, output=a.output_path, note=a.note, updated_at=now())
    save_state(out, st)
    d, t = coverage(stage)
    print(f"{a.stage} / {a.unit} → {a.status}  ({d}/{t})")
    return 0


def cmd_add_stage(a) -> int:
    out = Path(a.output).expanduser().resolve()
    m = load_manifest(out)
    st = load_state(out, m)

    if find_stage(st, a.stage):
        die(f"ステージ {a.stage} は既に登録済みです。")
    if a.stage not in STAGE_ORDER:
        print(f"警告: {a.stage} は標準の層名ではありません ({', '.join(STAGE_ORDER)})")
    else:
        # 登録は先にしてよい(未処理として並ぶだけ)。完了できるかは set 側で
        # ユニットのページ範囲ごとに判定する。
        idx = STAGE_ORDER.index(a.stage)
        for lower in STAGE_ORDER[:idx]:
            ls = find_stage(st, lower)
            if ls is None:
                continue
            d, t = coverage(ls)
            if d < t:
                print(f"注意: {lower} は {d}/{t}。範囲が埋まったユニットから完了にできます。")

    st["stages"].append({
        "name": a.stage,
        "units": [{"id": u, "status": "pending", "output": None, "note": None, "updated_at": None}
                  for u in a.units],
    })
    save_state(out, st)
    print(f"{a.stage} を登録: {len(a.units)}ユニット")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="進捗を出す")
    p.add_argument("output")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("set", help="ユニットの状態を更新する")
    p.add_argument("output")
    p.add_argument("--stage", required=True)
    p.add_argument("--unit", required=True)
    p.add_argument("--status", required=True, choices=STATUSES)
    p.add_argument("--output", dest="output_path", help="成果物のパス(出力先からの相対)")
    p.add_argument("--note")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("add-stage", help="上位ステージを登録する")
    p.add_argument("output")
    p.add_argument("--stage", required=True)
    p.add_argument("--units", nargs="+", required=True)
    p.set_defaults(func=cmd_add_stage)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
