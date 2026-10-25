"""`noodles`: pull NSE's free sources and build the dataset.

    noodles --data D pull bhavcopy --from 2011-06-22 --to 2026-10-23
    noodles --data D pull indices --from ... --to ...    (days that have a bhavcopy)
    noodles --data D pull nse --dataset corporate_actions --from ... --to ...
    noodles --data D pull lists                          (symbol/name changes, holidays)
    noodles --data D build bars | ca-db | actions | hindsight | reports
    noodles --data D build replay --as-of YYYY-MM-DD     (point-in-time replay, exit calendar)
    noodles --data D run --from ... --to ...             (everything, in order)
    noodles --data D import FILE_OR_FOLDER ...           (files you downloaded yourself)
    noodles --data D doctor                              (is each NSE source still reachable?)
    noodles --data D config init | show                  (DATA/noodles.toml: URLs, modes, pace)

`--data` defaults to $NOODLES_DATA. With no arguments, `noodles` opens a guided menu that asks
for each choice and shows the command it runs. Every step is resumable and idempotent: rerun it
after an interruption, and rerun `run` to bring the data up to date (it skips what is on disk).
The exit code is 1 when a download, an import, a probe or a build step failed (the printed
counts say which), 2 for a bad command line or noodles.toml.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from . import (
    actions,
    adjusted,
    bhavcopy,
    ca_db,
    config,
    doctor,
    exits,
    importer,
    indices,
    interactive,
    nse_json,
    reports,
)
from .config import Config
from .http import NseClient
from .layout import Layout

BUILD_STEPS = ("bars", "ca-db", "actions", "hindsight", "reports")  # `run` builds these in order


def _day(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _print(title: str, payload: object) -> None:
    print(f"{title}: {json.dumps(payload, default=str)}", flush=True)


def _range(args: argparse.Namespace) -> tuple[date, date]:
    return args.start, args.end or date.today()


def _manual(cfg: Config, source: str) -> bool:
    """True (and says so) when the source's files come only from `noodles import`."""
    if cfg.manual(source):
        _print(source, {"mode": "manual: add its files with `noodles import`"})
    return cfg.manual(source)


def pull_bhavcopy(layout: Layout, client: NseClient, args: argparse.Namespace, cfg: Config) -> bool:
    if _manual(cfg, "bhavcopy"):
        return True
    start, end = _range(args)
    days = bhavcopy.candidate_days(start, end) + [d for d in args.extra_days if start <= d <= end]
    r = bhavcopy.pull(layout, client, sorted(set(days)), recheck=args.recheck, cfg=cfg)
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
    return not r.errors


def pull_indices(layout: Layout, client: NseClient, args: argparse.Namespace, cfg: Config) -> bool:
    if _manual(cfg, "indices"):
        return True
    start, end = _range(args)
    days = [d for d in indices.session_days(layout) if start <= d <= end]
    r = indices.pull(layout, client, days, recheck=args.recheck, cfg=cfg)
    _print(
        "indices",
        {
            "downloaded": r.downloaded,
            "already": r.already,
            "not_found": len(r.not_found),
            "errors": [str(d) for d in r.errors],
        },
    )
    return not r.errors


def pull_nse(layout: Layout, client: NseClient, args: argparse.Namespace, cfg: Config) -> bool:
    start, end = _range(args)
    ok = True
    for dataset in args.dataset:
        if _manual(cfg, dataset):
            continue
        for index in args.index:
            r = nse_json.pull(layout, client, dataset, start, end, index, cfg=cfg)
            _print(
                f"{dataset}/{index}",
                {
                    "months_written": r.months_written,
                    "months_skipped": r.months_skipped,
                    "rows": r.rows,
                    "failed": r.failed,
                },
            )
            ok = ok and not r.failed
    return ok


def pull_lists(layout: Layout, client: NseClient, cfg: Config) -> bool:
    got = nse_json.pull_lists(layout, client, cfg)
    _print("lists", got)
    return all(status in ("ok", "manual") for status in got.values())


def import_files(layout: Layout, args: argparse.Namespace) -> bool:
    r = importer.run(layout, [Path(p) for p in args.paths], args.kind, args.market, args.replace)
    _print(
        "import",
        {
            "imported": r.imported,
            "already": r.already,
            "replaced": r.replaced,
            "conflicts": r.conflicts,
            "rejected": r.rejected,
        },
    )
    if r.imported:
        print("next: `noodles build bars` (or `noodles run`) reads them", flush=True)
    return r.ok()


def check_sources(cfg: Config, args: argparse.Namespace) -> bool:
    with cfg.network.client(args.interval, tries=2) as client:
        probes = doctor.run(client, cfg)
    print(f"sources ({cfg.path or 'default settings, no ' + config.FILE_NAME}):", flush=True)
    for line in doctor.report(probes):
        print(line, flush=True)
    return all(p.ok for p in probes)


def show_config(layout: Layout, cfg: Config, action: str) -> bool:
    path = layout.root / config.FILE_NAME
    if action == "show":
        print(f"# {cfg.path or f'defaults ({path} does not exist)'}")
        print(config.render(cfg), end="", flush=True)
        return True
    if path.exists():
        print(f"{path} exists; `noodles config show` prints what is in effect", file=sys.stderr)
        return False
    path.write_text(config.render(config.DEFAULT), encoding="utf-8")
    print(f"wrote {path}", flush=True)
    return True


def build_step(layout: Layout, step: str, args: argparse.Namespace) -> bool:
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
                "wrong_date_rows": r.wrong_date,
                "failed": r.failed,
            },
        )
        _print("index", {"days": indices.build(layout)})
        return not r.failed
    if step == "ca-db":
        _print("ca-db", ca_db.build(layout))
    elif step == "actions":
        _print("actions", actions.build(layout, curation))
    elif step == "hindsight":
        _print("hindsight", adjusted.build(layout))
    elif step == "replay":
        if args.as_of is None:
            raise SystemExit("build replay needs --as-of YYYY-MM-DD")
        _print(f"replay as of {args.as_of}", adjusted.build(layout, args.as_of))
        _print(
            f"demerger exits as of {args.as_of}",
            exits.write(layout, args.holidays, args.as_of, curation),
        )
    elif step == "reports":
        _print("reports", reports.write_all(layout, args.holidays, curation))
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noodles", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data",
        default=os.environ.get("NOODLES_DATA"),
        help="data directory, outside any git repo (default $NOODLES_DATA)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="seconds between requests (default 2, or [network] interval in noodles.toml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def ranged(p: argparse.ArgumentParser) -> None:
        p.add_argument("--from", dest="start", type=_day, default=None)  # else [run] from
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

    imp = sub.add_parser("import", help="file downloaded files into raw/, as a pull would")
    imp.add_argument("paths", nargs="+", help="files, folders or zips")
    imp.add_argument("--kind", choices=importer.KINDS, help="read every file as this kind")
    imp.add_argument(
        "--market", default="equities", choices=nse_json.INDEXES, help="for the JSON feeds"
    )
    imp.add_argument(
        "--replace", action="store_true", help="swap a day already on disk for the imported one"
    )
    sub.add_parser("doctor", help="ask each NSE source once and say what is broken")
    settings = sub.add_parser("config", help=f"DATA/{config.FILE_NAME}: write the defaults, show")
    settings.add_argument("action", choices=["init", "show"])
    return parser


def _pull(layout: Layout, client: NseClient, args: argparse.Namespace, cfg: Config) -> bool:
    if args.what == "bhavcopy":
        return pull_bhavcopy(layout, client, args, cfg)
    if args.what == "indices":
        return pull_indices(layout, client, args, cfg)
    if args.what == "nse":
        return pull_nse(layout, client, args, cfg)
    return pull_lists(layout, client, cfg)


def _run(layout: Layout, client: NseClient, args: argparse.Namespace, cfg: Config) -> bool:
    """Pull everything for the range in order; the caller then builds every step."""
    args.extra_days, args.recheck, args.rebuild = [], False, False
    ok = [pull_bhavcopy(layout, client, args, cfg), build_step(layout, "bars", args)]
    ok.append(pull_indices(layout, client, args, cfg))
    args.dataset = (
        ["corporate_actions", "announcements", "board_meetings"]
        if args.with_announcements or cfg.run.with_announcements
        else ["corporate_actions"]
    )
    args.index = ["equities", "sme"]
    ok += [pull_nse(layout, client, args, cfg), pull_lists(layout, client, cfg)]
    return all(ok)


def _defaults(args: argparse.Namespace, cfg: Config, root: Path) -> None:
    """Fill what the command line left out from noodles.toml (paths relative to `root`)."""
    if getattr(args, "start", False) is None:
        args.start = cfg.run.start
    for name in ("curation", "holidays"):
        if getattr(args, name, False) is None and getattr(cfg.run, name):
            setattr(args, name, str(root / Path(getattr(cfg.run, name)).expanduser()))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        code = run(argv)
    else:  # no arguments: the guided menu
        try:
            code = interactive.main(input, print, run)
        except EOFError:
            code = 0
        except KeyboardInterrupt:
            raise SystemExit(130) from None
    if code:
        raise SystemExit(code)


def run(argv: list[str]) -> int:
    """Run one command line; 1 when a download, import, probe or build step failed, else 0."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.data:
        parser.error("--data is required (or set NOODLES_DATA)")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )
    layout = Layout(Path(args.data).expanduser().resolve())
    layout.root.mkdir(parents=True, exist_ok=True)
    try:
        cfg = config.load(layout.root)
    except config.ConfigError as exc:
        print(f"{layout.root / config.FILE_NAME}: {exc}", file=sys.stderr)
        return 2
    _defaults(args, cfg, layout.root)
    return 0 if _dispatch(layout, args, cfg) else 1


def _dispatch(layout: Layout, args: argparse.Namespace, cfg: Config) -> bool:
    if args.cmd == "build":
        return build_step(layout, args.step, args)
    if args.cmd == "import":
        return import_files(layout, args)
    if args.cmd == "doctor":
        return check_sources(cfg, args)
    if args.cmd == "config":
        return show_config(layout, cfg, args.action)
    with cfg.network.client(args.interval) as client:
        if args.cmd == "pull":
            return _pull(layout, client, args, cfg)
        ok = _run(layout, client, args, cfg)
    for step in BUILD_STEPS:  # every step runs, even after one fails
        ok = build_step(layout, step, args) and ok
    return ok


if __name__ == "__main__":
    main()
