"""`noodles`: pull NSE's free sources and build the dataset.

    noodles pull bhavcopy  --data D --from 2011-06-22 --to 2026-10-23
    noodles pull indices   --data D --from ... --to ...      (days that have a bhavcopy)
    noodles pull nse       --data D --dataset corporate_actions --from ... --to ...
    noodles pull lists     --data D                          (symbol/name changes, holidays)
    noodles build bars | ca-db | actions | hindsight | reports  --data D
    noodles build replay   --data D --as-of YYYY-MM-DD   (point-in-time replay and exit calendar)
    noodles run            --data D --from ... --to ...      (everything, in order)

Every step is resumable and idempotent: rerun it after an interruption.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from pathlib import Path

from . import actions, adjusted, bhavcopy, ca_db, exits, indices, nse_json, reports
from .http import DEFAULT_INTERVAL, NseClient
from .layout import Layout


def _day(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _print(title: str, payload: object) -> None:
    print(f"{title}: {json.dumps(payload, default=str)}", flush=True)


def _range(args: argparse.Namespace) -> tuple[date, date]:
    return args.start, args.end or date.today()


def pull_bhavcopy(layout: Layout, client: NseClient, args: argparse.Namespace) -> None:
    start, end = _range(args)
    days = bhavcopy.candidate_days(start, end) + [d for d in args.extra_days if start <= d <= end]
    r = bhavcopy.pull(layout, client, sorted(set(days)), recheck=args.recheck)
    _print(
        "bhavcopy",
        {
            "downloaded": r.downloaded,
            "already": r.already,
            "not_found": len(r.not_found),
            "skipped_known_404": r.skipped_known,
            "errors": [str(d) for d in r.errors],
        },
    )


def pull_indices(layout: Layout, client: NseClient, args: argparse.Namespace) -> None:
    start, end = _range(args)
    days = [d for d in indices.session_days(layout) if start <= d <= end]
    r = indices.pull(layout, client, days, recheck=args.recheck)
    _print(
        "indices",
        {
            "downloaded": r.downloaded,
            "already": r.already,
            "not_found": len(r.not_found),
            "errors": [str(d) for d in r.errors],
        },
    )


def pull_nse(layout: Layout, client: NseClient, args: argparse.Namespace) -> None:
    start, end = _range(args)
    for dataset in args.dataset:
        for index in args.index:
            r = nse_json.pull(
                layout, client, dataset, start, end, index, refresh_current=args.refresh_current
            )
            _print(
                f"{dataset}/{index}",
                {
                    "months_written": r.months_written,
                    "months_skipped": r.months_skipped,
                    "rows": r.rows,
                    "failed": r.failed,
                },
            )


def build_step(layout: Layout, step: str, args: argparse.Namespace) -> None:
    curation = Path(args.curation) if args.curation else layout.root / "curation"
    if step == "bars":
        r = bhavcopy.build(layout, rebuild=args.rebuild)
        _print(
            "bars",
            {
                "days": r.days,
                "rows": r.rows,
                "already": r.already,
                "invalid_rows": r.invalid,
                "failed": r.failed,
            },
        )
        _print("index", {"days": indices.build(layout)})
    elif step == "ca-db":
        _print("ca-db", ca_db.build(layout))
    elif step == "actions":
        _print("actions", actions.build(layout, curation))
    elif step == "hindsight":
        _print("hindsight", adjusted.build(layout))
    elif step == "replay":
        if args.as_of is None:
            raise SystemExit("build replay needs --as-of YYYY-MM-DD")
        _print(f"replay as of {args.as_of}", adjusted.build(layout, args.as_of))
        _print(f"demerger exits as of {args.as_of}", exits.write(layout, args.holidays, args.as_of))
    elif step == "reports":
        _print("reports", reports.write_all(layout, args.holidays))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noodles", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", required=True, help="data directory (outside any git repo)")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="seconds between requests (default 2)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def ranged(p: argparse.ArgumentParser) -> None:
        p.add_argument("--from", dest="start", type=_day, default=bhavcopy.FIRST_ISIN_DAY)
        p.add_argument("--to", dest="end", type=_day, default=None)

    pull = sub.add_parser("pull").add_subparsers(dest="what", required=True)
    pb = pull.add_parser("bhavcopy")
    ranged(pb)
    pb.add_argument(
        "--extra-days", type=_day, nargs="*", default=[], help="weekend sessions to fetch"
    )
    pb.add_argument("--recheck", action="store_true", help="ask again for days that were 404")
    pi = pull.add_parser("indices")
    ranged(pi)
    pi.add_argument("--recheck", action="store_true")
    pn = pull.add_parser("nse")
    ranged(pn)
    pn.add_argument(
        "--dataset", nargs="+", default=["corporate_actions"], choices=sorted(nse_json.DATASETS)
    )
    pn.add_argument("--index", nargs="+", default=["equities"], choices=nse_json.INDEXES)
    pn.add_argument("--refresh-current", action="store_true", help="refetch the current month")
    pull.add_parser("lists")

    build = sub.add_parser("build")
    build.add_argument(
        "step", choices=["bars", "ca-db", "actions", "hindsight", "replay", "reports"]
    )
    build.add_argument("--curation", help="curation folder (default DATA/curation)")
    build.add_argument("--rebuild", action="store_true", help="bars: re-parse every zip")
    build.add_argument(
        "--holidays", help="reports, replay: extra holiday list, one ISO date per line"
    )
    build.add_argument(
        "--as-of",
        type=_day,
        help="replay: the point-in-time series as it could have been built after this day",
    )

    run = sub.add_parser("run", help="pull everything for the range, then build every step")
    ranged(run)
    run.add_argument("--curation")
    run.add_argument("--holidays")
    run.add_argument(
        "--with-announcements",
        action="store_true",
        help="also pull announcements and board meetings (slow; dates each event's first notice)",
    )
    return parser


def _pull(layout: Layout, client: NseClient, args: argparse.Namespace) -> None:
    if args.what == "bhavcopy":
        pull_bhavcopy(layout, client, args)
    elif args.what == "indices":
        pull_indices(layout, client, args)
    elif args.what == "nse":
        pull_nse(layout, client, args)
    else:
        _print("lists", nse_json.pull_lists(layout, client))


def _run(layout: Layout, client: NseClient, args: argparse.Namespace) -> None:
    """Pull everything for the range in order; the caller then builds every step."""
    args.extra_days, args.recheck, args.refresh_current, args.rebuild = [], False, False, False
    pull_bhavcopy(layout, client, args)
    build_step(layout, "bars", args)
    pull_indices(layout, client, args)
    args.dataset = (
        ["corporate_actions", "announcements", "board_meetings"]
        if args.with_announcements
        else ["corporate_actions"]
    )
    args.index = ["equities", "sme"]
    pull_nse(layout, client, args)
    _print("lists", nse_json.pull_lists(layout, client))


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )
    layout = Layout(Path(args.data).expanduser().resolve())
    layout.root.mkdir(parents=True, exist_ok=True)

    if args.cmd == "build":
        build_step(layout, args.step, args)
        return
    with NseClient(interval=args.interval) as client:
        if args.cmd == "pull":
            _pull(layout, client, args)
            return
        _run(layout, client, args)
    for step in ("bars", "ca-db", "actions", "hindsight", "reports"):
        build_step(layout, step, args)


if __name__ == "__main__":
    main()
