"""Storage-compatibility evidence across a runtime change (Phase 2B; simulation only).

    build  - drive a GIVEN interpreter + source tree through the CLI to create isolated runtime
             roots with rich persisted state, quiesce them and back them up with SQLite APIs:
               A  compat profile (schema v2): protected multi-TP position with a deadline,
                  TP1 filled (partial TP progress), close-before-open tombstone, and a STOP
                  intent left INTENT by a crash point during an amendment (unknown order);
               B  strict profile: signed protected position, signed tombstone, and a signed
                  OPEN HELD in the inbox because the key was absent (strict pending event);
               C  (optional, --phase1-src) a real schema-v1 root built with Phase 1 code.
    verify - with the CURRENT interpreter (must be the approved runtime): check the backups,
             restore A/B into NEW roots (C: SQLite-API copies into a new root), run one
             reconciling cycle, then check PRAGMA integrity_check / foreign_key_check and
             semantic invariants (positions vs remaining quantity, client ids, executed TP
             quantities, fees, event ordering, tombstones, deadlines, protection).

Only processes started here are ever terminated (crash points exit by themselves). Keys are
generated into the fixture directory (outside the repository), passed to children via the
environment only, and never printed. Never point --out at var\\ or an existing runtime root.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
KEY_ENV = "ASEKE_EXEC_HMAC_SIM_DEMO_K1"


def ro(db: Path) -> sqlite3.Connection:
    for _ in range(4):  # same bounded retry rationale as journal.connect_readonly
        c = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return c
        except sqlite3.OperationalError as e:
            c.close()
            if getattr(e, "sqlite_errorname", None) != "SQLITE_IOERR_TRUNCATE":
                raise
    raise RuntimeError(f"cannot open {db} read-only")


def q(db: Path, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    c = ro(db)
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def integrity(db: Path) -> dict[str, Any]:
    c = ro(db)
    try:
        ic = [r[0] for r in c.execute("PRAGMA integrity_check")]
        fk = c.execute("PRAGMA foreign_key_check").fetchall()
        return {"integrity_check": "ok" if ic == ["ok"] else ic[:10], "foreign_key_violations": len(fk)}
    finally:
        c.close()


class Tree:
    """CLI of a specific source tree run by a specific interpreter (isolated env)."""

    def __init__(self, python: str, src_root: Path, cfg: Path, data_dir: Path | None, key: str | None = None):
        self.python, self.src, self.cfg, self.data_dir, self.key = python, src_root, cfg, data_dir, key

    def env(self, with_key: bool = True) -> dict[str, str]:
        e = {k: v for k, v in os.environ.items() if not k.startswith(("ASEKE_EXEC_", "PYTHON"))}
        e["PYTHONPATH"] = str(self.src / "src")
        if self.key and with_key:
            e[KEY_ENV] = self.key
        return e

    def run(self, *args: str, crash: str | None = None, with_key: bool = True, check: bool = True,
            expect_rc: int = 0) -> subprocess.CompletedProcess:
        env = self.env(with_key)
        if crash:
            env["ASEKE_EXEC_CRASHPOINT"] = crash
        cmd = [self.python, "-m", "aseke_exec", "--config", str(self.cfg)]
        if self.data_dir is not None:
            cmd += ["--data-dir", str(self.data_dir)]
        r = subprocess.run(cmd + list(args), capture_output=True, text=True, env=env, cwd=str(self.src),
                           timeout=180)
        if check and r.returncode != expect_rc:
            raise SystemExit(f"{' '.join(args)} -> rc {r.returncode} (want {expect_rc})\n{r.stdout}\n{r.stderr}")
        return r

    def py(self, code: str, with_key: bool = True) -> str:
        r = subprocess.run([self.python, "-c", code], capture_output=True, text=True, env=self.env(with_key),
                           cwd=str(self.src), timeout=120)
        if r.returncode:
            raise SystemExit(f"helper failed: {r.stderr}")
        return r.stdout


def _key_from_file(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig").strip()


def _snapshot(db: Path) -> dict[str, Any]:
    return {
        "events": [(r["seq"], r["event_id"], r["event_type"], r["process_status"])
                   for r in q(db, "SELECT seq, event_id, event_type, process_status FROM events ORDER BY seq")],
        "orders": {r["client_order_id"]: {"leg": r["leg"], "kind": r["kind"], "status": r["status"],
                                          "qty": r["qty"]}
                   for r in q(db, "SELECT client_order_id, leg, kind, status, qty FROM orders")},
        "executions": {f'{r["trade_id"]}/{r["account_id"]}': {
            k: r.get(k) for k in ("state", "filled_qty", "entry_fees", "deadline_at", "exit_plan", "direction")}
            for r in q(db, "SELECT * FROM executions")},
        "fills": [(r["client_order_id"], r["qty"], r["price"], r["fee"]) for r in q(
            db, "SELECT o.client_order_id, f.qty, f.price, f.fee FROM fills f "
                "JOIN orders o ON o.id=f.order_id ORDER BY f.id")],
        "tombstones": sorted(r["trade_id"] for r in q(db, "SELECT trade_id FROM trades WHERE status='TOMBSTONED'")),
        "schema_version": int((q(db, "SELECT value FROM meta WHERE key='schema_version'") or [{"value": 1}])[0]["value"]),
    }


# ------------------------------------------------------------------ build
def build(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refused: {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    src = Path(args.src).resolve()
    manifest: dict[str, Any] = {"python": args.python, "src": str(src)}
    ident = subprocess.run([args.python, "-c", "import sys,sqlite3;print(sys.version.split()[0], sqlite3.sqlite_version)"],
                           capture_output=True, text=True).stdout.split()
    manifest["builder_python"], manifest["builder_sqlite"] = ident

    # ---- A: compat profile
    cfg_a = out / "sim.toml"
    shutil.copy(src / "config/sim.toml", cfg_a)
    ta = Tree(args.python, src, cfg_a, out / "A-root")
    ta.run("init-runtime")
    ta.py("from aseke_exec.examples import build_open; from aseke_exec.inbox import publish_event; "
          "from aseke_exec.clock import SystemClock; import pathlib; "
          f"publish_event(pathlib.Path(r'{out / 'A-root' / 'inbox'}'), build_open(SystemClock().now(), 'fx-multi', "
          "take_profits=[('65000.10','0.5'),('66000.10','0.5')], holding={'max_holding_seconds': 3600}))")
    ta.run("cycle")
    ta.run("sim", "set-market", "--price", "65100")
    ta.run("cycle")
    ta.run("publish-example", "close", "--trade-id", "fx-ghost", "--revision", "2")
    ta.run("cycle")
    ta.run("publish-example", "amend", "--trade-id", "fx-multi", "--stop", "64200.10", "--tp", "66000.10")
    ta.run("cycle", crash="after_intent:STOP", expect_rc=97)
    ta.run("backup", "--dest", str(out / "A-backup"))
    manifest["A"] = _snapshot(out / "A-backup" / "journal.db")
    manifest["A"]["intent_orders"] = [k for k, v in manifest["A"]["orders"].items() if v["status"] == "INTENT"]

    # ---- B: strict profile (test-only key generated here, outside the repository)
    cfg_b = out / "sim-strict.toml"
    shutil.copy(src / "config/sim-strict.toml", cfg_b)
    keyfile = out / "keys" / "sim-demo-k1.key"
    Tree(args.python, src, cfg_b, None).run("auth", "gen-key", "--out", str(keyfile))
    key = _key_from_file(keyfile)
    tb = Tree(args.python, src, cfg_b, out / "B-root", key=key)
    tb.run("init-runtime")
    tb.run("publish-example", "long", "--signed", "--trade-id", "fxs-1")
    tb.run("cycle")
    tb.run("publish-example", "close", "--signed", "--trade-id", "fxs-ghost", "--revision", "2")
    tb.run("cycle")
    tb.run("publish-example", "timed", "--signed", "--trade-id", "fxs-held")
    tb.run("cycle", with_key=False)  # key absent -> signed file HELD in the inbox (fail closed)
    tb.run("backup", "--dest", str(out / "B-backup"))
    manifest["B"] = _snapshot(out / "B-backup" / "journal.db")
    manifest["B"]["held_inbox_files"] = sorted(p.name for p in (out / "B-backup" / "inbox").glob("*.json"))

    # ---- C: Phase 1 schema v1 (optional)
    if args.phase1_src:
        p1 = Path(args.phase1_src).resolve()
        root_c = out / "C-root"
        root_c.mkdir()
        text = (p1 / "config/sim.toml").read_text(encoding="utf-8")
        text = text.replace('data_dir = "../var"', f"data_dir = '{root_c.as_posix()}'")
        cfg_c = out / "sim-phase1.toml"
        cfg_c.write_text(text, encoding="utf-8")
        tc = Tree(args.python, p1, cfg_c, None)
        tc.run("sim", "init")
        tc.run("publish-example", "long", "--trade-id", "fx1-long")
        tc.run("cycle")
        tc.run("publish-example", "close", "--trade-id", "fx1-ghost", "--revision", "2")
        tc.run("cycle")
        copy = out / "C-copy"
        copy.mkdir()
        tc.py("import sqlite3, pathlib\n"  # quiesced: no process runs; SQLite Online Backup API
              f"for n in ('journal.db', 'sim_exchange.db'):\n"
              f"    s = sqlite3.connect(pathlib.Path(r'{root_c}', n).as_uri() + '?mode=ro', uri=True)\n"
              f"    d = sqlite3.connect(str(pathlib.Path(r'{copy}', n)))\n"
              "    s.backup(d); d.execute('PRAGMA journal_mode=DELETE'); d.close(); s.close()\n")
        manifest["C"] = _snapshot(copy / "journal.db")
        manifest["C"]["phase1_src"] = str(p1)
    (out / "fixture-manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"built": str(out), "builder": ident, "roots": [k for k in ("A", "B", "C") if k in manifest]}))
    return 0


# ------------------------------------------------------------------ verify
def _semantic(root: Path, before: dict[str, Any], cfg: Path, label: str) -> list[str]:
    """Checks on a restored/migrated root after one reconciling cycle (approved runtime)."""
    sys.path.insert(0, str(REPO / "src"))
    from aseke_exec.config import load_config
    from aseke_exec.engine import TERMINAL
    from aseke_exec.lock import ProcessLock
    from aseke_exec.runtime import build_runtime
    from aseke_exec.soak import check_invariants

    problems: list[str] = []
    after = _snapshot(root / "journal.db")
    # event history: the pre-change sequence is an unchanged prefix (ids, order, type)
    for b, a in zip(before["events"], after["events"]):
        if tuple(b[:3]) != tuple(a[:3]):
            problems.append(f"{label}: event order changed {b} -> {a}")
    if len(after["events"]) < len(before["events"]):
        problems.append(f"{label}: events lost")
    # every pre-existing client order id still exists with the same leg/kind/qty
    for cid, o in before["orders"].items():
        x = after["orders"].get(cid)
        if x is None or (x["leg"], x["kind"], x["qty"]) != (o["leg"], o["kind"], o["qty"]):
            problems.append(f"{label}: order {cid} changed/lost: {o} -> {x}")
    # fills are append-only: the earlier fills are an unchanged prefix (qty, price, fee)
    if [tuple(f) for f in after["fills"][:len(before["fills"])]] != [tuple(f) for f in before["fills"]]:
        problems.append(f"{label}: fills changed")
    for k, e in before["executions"].items():
        x = after["executions"].get(k)
        if x is None:
            problems.append(f"{label}: execution {k} lost")
            continue
        if x["entry_fees"] != e["entry_fees"] or x["filled_qty"] != e["filled_qty"]:
            problems.append(f"{label}: {k} entry accounting changed {e} -> {x}")
        if e["deadline_at"] and x["deadline_at"] != e["deadline_at"]:
            problems.append(f"{label}: {k} deadline changed")
        bp = json.loads(e["exit_plan"]) if e["exit_plan"] else {}
        ap = json.loads(x["exit_plan"]) if x["exit_plan"] else {}
        if bp.get("version") == ap.get("version"):  # same plan: per-leg progress never decreases
            al = {leg.get("index"): leg for leg in ap.get("legs", [])}
            for leg in bp.get("legs", []):
                got = (al.get(leg.get("index")) or {}).get("executed_qty") or 0
                if Decimal(str(leg.get("executed_qty") or 0)) > Decimal(str(got)):
                    problems.append(f"{label}: {k} TP leg {leg.get('index')} executed qty decreased")
        elif ap:  # an amendment re-bases the new plan on the REMAINING position (fills keep history)
            if ap.get("version", 0) < bp.get("version", 0):
                problems.append(f"{label}: {k} exit plan version went backwards")
    if not set(before["tombstones"]) <= set(after["tombstones"]):
        problems.append(f"{label}: tombstone lost")
    if [c for c, o in after["orders"].items() if o["status"] == "INTENT"]:
        problems.append(f"{label}: INTENT orders remain unresolved after reconciliation")
    # live invariants against the restored simulator (positions vs remaining qty, stops, TPs)
    c = load_config(cfg, data_dir=root)
    with ProcessLock(c.lock_path):
        rt = build_runtime(c)
        try:
            problems += [f"{label}: {v}" for v in check_invariants(rt)]
            for e in rt.journal.active_executions(TERMINAL):
                pos = rt.sim.position(rt.config.accounts[e["account_id"]].venue, e["account_id"], e["native_symbol"])
                exits = sum((f["qty"] for f in rt.journal.fills_for(e["id"], ("STOP", "TP", "CLOSE"))), Decimal(0))
                if pos["qty"] != e["filled_qty"] - exits:
                    problems.append(f"{label}: {e['trade_id']}/{e['account_id']} venue qty {pos['qty']} != "
                                    f"filled {e['filled_qty']} - exits {exits}")
                if e["state"] == "OPEN_PROTECTED" and e["stop_coverage"] != "VERIFIED":
                    problems.append(f"{label}: {e['trade_id']}/{e['account_id']} protected without verified stop")
        finally:
            rt.close()
    return problems


def _check_integrity(label: str, d: dict[str, Any]) -> list[str]:
    return [f"{label}: integrity {k}" for k, v in d.items()
            if v["integrity_check"] != "ok" or v["foreign_key_violations"]]


def verify(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO / "src"))
    from aseke_exec.runtime_policy import current
    pol = current()
    fx = Path(args.fixtures).resolve()
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refused: {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    man = json.loads((fx / "fixture-manifest.json").read_text(encoding="utf-8"))
    me = sys.executable
    rep: dict[str, Any] = {"verifier": {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                                        "operational": pol.operational, "executable": me},
                           "builder": {"python": man["builder_python"], "sqlite": man["builder_sqlite"]},
                           "checks": {}}
    problems: list[str] = []
    if not pol.operational:
        problems.append("verifier is not the approved runtime")
    for label, cfgname, backup in (("A", "sim.toml", "A-backup"), ("B", "sim-strict.toml", "B-backup")):
        if label not in man:
            continue
        cfg = fx / cfgname
        key = _key_from_file(fx / "keys" / "sim-demo-k1.key") if label == "B" else None
        root = out / f"{label}-restored"
        pre = {n: integrity(fx / backup / n) for n in ("journal.db", "sim_exchange.db")}
        t = Tree(me, REPO, cfg, root, key=key)
        t.run("restore", "--backup", str(fx / backup))
        t.run("cycle")
        if label == "B":
            t.run("cycle")
        # unknown (INTENT) orders resolve by client id after the BOUNDED not-found checks (one
        # missing observation is never proof of absence) - allow up to 5 cycles, record how many
        cycles_to_resolve = 1
        while cycles_to_resolve < 5 and q(root / "journal.db", "SELECT id FROM orders WHERE status='INTENT'"):
            t.run("cycle")
            cycles_to_resolve += 1
        post = {n: integrity(root / n) for n in ("journal.db", "sim_exchange.db")}
        sem = _semantic(root, man[label], cfg, label)
        extra: dict[str, Any] = {}
        if label == "A":
            intents = man["A"]["intent_orders"]
            extra["intent_orders_after"] = {c: q(root / "journal.db",
                                                 "SELECT status FROM orders WHERE client_order_id=?", (c,))[0]["status"]
                                            for c in intents}
            if not intents:
                sem.append("A: fixture has no INTENT order (crash point did not fire as designed)")
            # the tombstone still blocks a later OPEN for the same trade
            t.py("from aseke_exec.examples import build_open; from aseke_exec.inbox import publish_event; "
                 "from aseke_exec.clock import SystemClock; import pathlib; "
                 f"publish_event(pathlib.Path(r'{root / 'inbox'}'), build_open(SystemClock().now(), 'fx-ghost'))")
            t.run("cycle")
            gh = q(root / "journal.db", "SELECT state FROM executions WHERE trade_id='fx-ghost'")
            ev = q(root / "journal.db",
                   "SELECT process_status, process_code FROM events WHERE trade_id='fx-ghost' AND event_type='OPEN'")
            extra["ghost_open_after_restore"] = {"executions": gh, "event": ev}
            if any(r["state"] != "REJECTED" for r in gh) or not ev or ev[0]["process_status"] == "APPLIED":
                sem.append("A: tombstoned trade opened after restore")
        if label == "B":
            rows = q(root / "journal.db", "SELECT process_status, process_code FROM events WHERE trade_id='fxs-held'")
            extra["held_files_before"] = man["B"]["held_inbox_files"]
            extra["held_event_after_restore_with_key"] = rows
            if not man["B"]["held_inbox_files"]:
                sem.append("B: fixture has no held strict file")
            if not rows:
                sem.append("B: held signed event was not ingested after the key became available")
            if q(root / "journal.db", "SELECT id FROM executions WHERE trade_id='fxs-held' AND state <> 'REJECTED'"):
                sem.append("B: held OPEN created exposure while another trade was active")
        rep["checks"][label] = {"integrity_backup": pre, "integrity_after": post, "semantic_problems": sem,
                                "cycles_after_restore_until_no_intent": cycles_to_resolve, **extra}
        problems += sem + _check_integrity(label, {**{f"backup/{k}": v for k, v in pre.items()},
                                                   **{f"after/{k}": v for k, v in post.items()}})
    if "C" in man:
        root = out / "C-migrated"
        root.mkdir()
        for n in ("journal.db", "sim_exchange.db"):  # SQLite Online Backup API copies into a NEW root
            s = ro(fx / "C-copy" / n)
            d = sqlite3.connect(str(root / n))
            s.backup(d)
            d.close()
            s.close()
        for sub in ("inbox", "archive", "quarantine", "control"):
            (root / sub).mkdir()
        cfg = fx / "sim.toml"  # current compat config, same synthetic accounts/venues
        pre = {n: integrity(fx / "C-copy" / n) for n in ("journal.db", "sim_exchange.db")}
        Tree(me, REPO, cfg, root).run("cycle")
        post = {n: integrity(root / n) for n in ("journal.db", "sim_exchange.db")}
        sem = _semantic(root, man["C"], cfg, "C")
        sv = _snapshot(root / "journal.db")["schema_version"]
        if man["C"]["schema_version"] != 1 or sv < 2:
            sem.append(f"C: schema {man['C']['schema_version']} -> {sv} (expected 1 -> 2)")
        rep["checks"]["C"] = {"schema_before": man["C"]["schema_version"], "schema_after": sv,
                              "integrity_copy": pre, "integrity_after": post, "semantic_problems": sem}
        problems += sem + _check_integrity("C", {**{f"copy/{k}": v for k, v in pre.items()},
                                                 **{f"after/{k}": v for k, v in post.items()}})
    rep["problems"] = problems
    rep["result"] = "PASS" if not problems else "FAIL"
    (out / "compat-report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"result": rep["result"], "problems": problems, "report": str(out / "compat-report.json")}))
    return 0 if not problems else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--python", required=True, help="interpreter that builds the fixtures")
    b.add_argument("--src", required=True, help="source tree root (e.g. a `git archive` export)")
    b.add_argument("--phase1-src", help="optional Phase 1 tree (schema v1 fixture)")
    b.add_argument("--out", required=True, help="NEW empty fixture directory (never var\\)")
    b.set_defaults(fn=build)
    v = sub.add_parser("verify")
    v.add_argument("--fixtures", required=True)
    v.add_argument("--out", required=True, help="NEW empty directory for restored roots")
    v.set_defaults(fn=verify)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
