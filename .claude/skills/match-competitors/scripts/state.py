#!/usr/bin/env python3
"""Workflow state CLI for the competitor-matching run. All writes atomic (tmp+rename).

Commands:
  status                          human-readable summary (+ --json)
  claim [--batch N]               claim next (or resume in-progress) batch; prints batch info
  checkpoint --batch N --file F   append one product result record (JSON file); updates state
  complete --batch N              mark batch done, fold stats
  retry-pop [--max N]             pop up to N retry items, print as JSON array
  retry-push --file F             push one retry item {product_id, competitor?, reason, attempts, ...}
  review-push --file F            append an item to needs_human_review.json
"""
import argparse
import datetime
import json
import os
import sys

DATA_DIR = os.environ.get("MATCH_DATA_DIR", "/srv/crawmatic/matching")
STATE = os.path.join(DATA_DIR, "state.json")
RETRY = os.path.join(DATA_DIR, "retry-queue.json")
REVIEW = os.path.join(DATA_DIR, "needs_human_review.json")
MASTER = os.path.join(DATA_DIR, "matches.master.jsonl")
STALE_HOURS = 2


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def load(path, default=None):
    if not os.path.exists(path):
        if default is not None:
            return default
        sys.exit(f"missing {path} — run prepare_batches.py first")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def results_path(batch_id):
    return os.path.join(DATA_DIR, "results", f"batch-{batch_id:03d}.results.json")


def batch_path(batch_id):
    return os.path.join(DATA_DIR, "batches", f"batch-{batch_id:03d}.json")


def cmd_status(args):
    st = load(STATE)
    retry = load(RETRY, [])
    st["retry_queue_size"] = len(retry)
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return
    b = st["batches"]
    s = st["stats"]
    print(f"run: {st['run_id']}  batches: {len(b['completed'])}/{b['total']} done, "
          f"in_progress: {b['in_progress']}, remaining: {len(b['remaining'])}")
    print(f"products done: {s['products_done']}  high: {s['high']}  medium: {s['medium']}  "
          f"low: {s['low']}  gap: {s['gap']}  review: {s['review']}  errors: {s['errors']}")
    for c, v in s["per_competitor"].items():
        print(f"  {c}: matched {v['matched']}  gap {v['gap']}")
    print(f"retry queue: {len(retry)}")
    cur = st.get("current_batch")
    if cur:
        stale = ""
        try:
            age = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.datetime.fromisoformat(cur["claimed_at"])).total_seconds() / 3600
            if age > STALE_HOURS:
                stale = f"  [STALE {age:.1f}h — resume it]"
        except Exception:
            pass
        print(f"current batch: {cur['id']}  done {len(cur['done_product_ids'])} products{stale}")


def cmd_claim(args):
    st = load(STATE)
    b = st["batches"]
    cur = st.get("current_batch")
    if cur and (args.batch is None or args.batch == cur["id"]):
        bid = cur["id"]  # resume
    else:
        if cur:
            sys.exit(f"batch {cur['id']} is in progress — resume it or complete it first")
        if args.batch is not None:
            if args.batch not in b["remaining"]:
                sys.exit(f"batch {args.batch} not in remaining list")
            bid = args.batch
        elif b["remaining"]:
            bid = b["remaining"][0]
        else:
            sys.exit("no batches remaining")
        st["current_batch"] = {"id": bid, "claimed_at": now(), "done_product_ids": [],
                               "last_checkpoint": None}
        b["in_progress"] = bid
        b["remaining"] = [x for x in b["remaining"] if x != bid]
        atomic_write(STATE, st)
    batch = load(batch_path(bid))
    done = set(st["current_batch"]["done_product_ids"])
    todo = [p for p in batch["products"] if p["product_id"] not in done]
    print(json.dumps({
        "batch_id": bid, "tier": batch["tier"], "batch_file": batch_path(bid),
        "results_file": results_path(bid),
        "total": batch["count"], "done": len(done), "todo": len(todo),
        "todo_product_ids": [p["product_id"] for p in todo],
    }, ensure_ascii=False, indent=2))


CONF_KEYS = {"high", "medium", "low", "gap"}


def cmd_checkpoint(args):
    rec = load(args.file)
    if "product_id" not in rec or "matches" not in rec:
        sys.exit("record must have product_id and matches[]")
    st = load(STATE)
    cur = st.get("current_batch")
    if not cur or cur["id"] != args.batch:
        sys.exit(f"batch {args.batch} is not the current batch")
    pid = rec["product_id"]
    if pid in cur["done_product_ids"]:
        print(f"product {pid} already checkpointed — skipping duplicate")
        return
    rec.setdefault("batch_id", args.batch)
    rec.setdefault("checkpointed_at", now())

    rp = results_path(args.batch)
    results = load(rp, [])
    results.append(rec)
    atomic_write(rp, results)
    with open(MASTER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    s = st["stats"]
    s["products_done"] += 1
    for m in rec["matches"]:
        comp = m.get("competitor")
        conf = m.get("confidence")
        status = m.get("status")
        if conf in CONF_KEYS:
            s[conf] += 1
        if status == "matched" and comp in s["per_competitor"]:
            s["per_competitor"][comp]["matched"] += 1
        elif conf == "gap" and comp in s["per_competitor"]:
            s["per_competitor"][comp]["gap"] += 1
        if status in ("error_transient", "error"):
            s["errors"] += 1
        if status in ("ambiguous", "needs_human_review"):
            s["review"] += 1
    cur["done_product_ids"].append(pid)
    cur["last_checkpoint"] = now()
    atomic_write(STATE, st)
    print(f"checkpointed product {pid} ({len(cur['done_product_ids'])} done in batch {args.batch})")


def cmd_complete(args):
    st = load(STATE)
    cur = st.get("current_batch")
    if not cur or cur["id"] != args.batch:
        sys.exit(f"batch {args.batch} is not the current batch")
    batch = load(batch_path(args.batch))
    missing = [p["product_id"] for p in batch["products"]
               if p["product_id"] not in set(cur["done_product_ids"])]
    if missing and not args.force:
        sys.exit(f"{len(missing)} products not checkpointed: {missing[:10]} … use --force to close anyway")
    st["batches"]["completed"].append(args.batch)
    st["batches"]["in_progress"] = None
    st["current_batch"] = None
    st["retry_queue_size"] = len(load(RETRY, []))
    atomic_write(STATE, st)
    print(f"batch {args.batch} complete. remaining: {len(st['batches']['remaining'])}")


def cmd_retry_pop(args):
    q = load(RETRY, [])
    take, keep = q[:args.max], q[args.max:]
    atomic_write(RETRY, keep)
    print(json.dumps(take, ensure_ascii=False, indent=2))


def cmd_retry_push(args):
    item = load(args.file)
    item.setdefault("attempts", 0)
    item["attempts"] += 1
    item["pushed_at"] = now()
    q = load(RETRY, [])
    if item["attempts"] > 3:
        rv = load(REVIEW, [])
        item["reason_final"] = "max retries exceeded"
        rv.append(item)
        atomic_write(REVIEW, rv)
        print(f"product {item.get('product_id')} exceeded 3 attempts -> needs_human_review")
    else:
        q.append(item)
        atomic_write(RETRY, q)
        print(f"queued retry (attempt {item['attempts']}) for product {item.get('product_id')}")


def cmd_review_push(args):
    item = load(args.file)
    item["pushed_at"] = now()
    rv = load(REVIEW, [])
    rv.append(item)
    atomic_write(REVIEW, rv)
    print(f"review item added ({len(rv)} total)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("claim"); p.add_argument("--batch", type=int); p.set_defaults(fn=cmd_claim)
    p = sub.add_parser("checkpoint"); p.add_argument("--batch", type=int, required=True)
    p.add_argument("--file", required=True); p.set_defaults(fn=cmd_checkpoint)
    p = sub.add_parser("complete"); p.add_argument("--batch", type=int, required=True)
    p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_complete)
    p = sub.add_parser("retry-pop"); p.add_argument("--max", type=int, default=15); p.set_defaults(fn=cmd_retry_pop)
    p = sub.add_parser("retry-push"); p.add_argument("--file", required=True); p.set_defaults(fn=cmd_retry_push)
    p = sub.add_parser("review-push"); p.add_argument("--file", required=True); p.set_defaults(fn=cmd_review_push)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
