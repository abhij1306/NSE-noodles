import io
import zipfile
from datetime import date

from nse_noodles import bhavcopy, cli, interactive
from nse_noodles.layout import Layout, write_bytes

TODAY = date(2026, 10, 23)


def _session(answers):
    """Drive the menu with scripted answers; returns (exit code, lines said, argv run)."""
    pending, said, ran = list(answers), [], []

    def ask(prompt):
        said.append(prompt)
        return pending.pop(0)

    def run(argv):
        ran.append(argv)
        return 0

    code = interactive.main(ask, said.append, run, today=TODAY)
    assert pending == [], "the menu asked fewer questions than scripted"
    return code, said, ran


def test_run_everything_with_defaults_on_an_empty_directory(tmp_path):
    root = str(tmp_path / "data")
    code, said, ran = _session([root, "", "", "", "", "", "6"])
    assert code == 0
    assert "  empty: start with 'Run everything for a date range'" in said
    assert ran == [["--data", root, "run", "--from", "2026-09-23", "--to", "2026-10-23"]]
    assert f"  = noodles --data {root} run --from 2026-09-23 --to 2026-10-23" in said


def test_pull_nse_for_both_markets_and_a_typo_retried(tmp_path):
    root = str(tmp_path / "data")
    answers = [root, "2", "9", "3", "2026-10-01", "", "2", "3", "", "q"]
    code, said, ran = _session(answers)
    assert code == 0
    assert "  Type a number from the list (q goes back)." in said
    assert ran == [
        [
            "--data", root, "pull", "nse", "--from", "2026-10-01", "--to", "2026-10-23",
            "--dataset", "announcements", "--index", "equities", "sme",
        ]
    ]  # fmt: skip


def test_replay_defaults_to_the_last_bar_day_and_q_cancels(tmp_path):
    layout = Layout(tmp_path / "data")
    write_bytes(layout.bars / "date=2026-10-20.parquet", b"")
    code, said, ran = _session([str(layout.root), "4", "q", "4", "", "", "6"])
    assert code == 0
    assert "As of the close of (YYYY-MM-DD) [2026-10-20]: " in said
    assert ran == [["--data", str(layout.root), "build", "replay", "--as-of", "2026-10-20"]]
    assert "  price bars        1 days, 2026-10-20 to 2026-10-20" in said


def test_a_data_directory_inside_a_git_repository_is_refused(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    inside, outside = str(tmp_path / "repo" / "data"), str(tmp_path / "data")
    _, said, _ = _session([inside, outside, "6"])
    assert "  That is inside a git repository; data must never be committed. Pick another." in said


def test_the_menu_runs_through_the_cli_and_reports_its_exit_code(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("cm06MAR2012bhav.csv", "SYMBOL,SERIES\n")
    layout = Layout(tmp_path / "data")
    write_bytes(layout.bhavcopy / bhavcopy.archive_name(date(2012, 3, 6)), buf.getvalue())
    answers = iter([str(layout.root), "3", "1", "", "6"])
    said = []
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("builtins.print", lambda *a, **k: said.append(" ".join(map(str, a))))
    try:
        cli.main([])
    except SystemExit as exc:
        assert exc.code == 1
    assert "  finished with exit code 1: see the counts above." in said
