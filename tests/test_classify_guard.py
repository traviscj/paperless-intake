"""classify must not turn a model-server outage into a pile of failed files."""
import sqlite3

import pytest

from pintake import classify as clf


def test_infrastructure_errors_are_told_apart_from_document_errors():
    assert clf.infrastructure_error("RuntimeError: POST http://h:9292/v1/chat/completions -> 500: b'{\"src\":\"llama-swap\",\"error\":{\"message\":\"upstream command exited prematurely\"'")
    assert clf.infrastructure_error("URLError: <urlopen error [Errno 61] Connection refused>")
    assert clf.infrastructure_error("TimeoutError: timed out")
    assert not clf.infrastructure_error("ValueError: no JSON in model reply: I think this is")
    assert not clf.infrastructure_error("RuntimeError: POST http://h/x -> 400: b'bad schema'")
    assert not clf.infrastructure_error("KeyError: 'disposition'")


@pytest.fixture
def stages(tmp_path, monkeypatch):
    monkeypatch.setenv("PINTAKE_DB", str(tmp_path / "state.db"))
    import importlib
    from pintake import config, db
    from pintake import stages as st
    importlib.reload(config); importlib.reload(db); importlib.reload(st)
    con = db.connect()
    for i in range(12):
        con.execute("INSERT INTO files (path, name, mtime, status, text, pages, ocr) VALUES (?,?,?,?,?,?,0)", (f"/x/{i}.pdf", f"{i}.pdf", 1000 + i, "extracted", "text " * 50, 1))
    con.commit()
    monkeypatch.setattr(st.paperless, "taxonomy", lambda: {})
    monkeypatch.setattr(st.clf, "preflight", lambda model=None: ["m"])
    return st


def _status(tmp_path):
    con = sqlite3.connect(tmp_path / "state.db")
    return dict(con.execute("SELECT status, count(*) FROM files GROUP BY status"))


def test_a_server_outage_stops_the_run_and_marks_nothing_failed(stages, tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "_classify_one", lambda tax, row, model, vision=True: (row["path"], None, "RuntimeError: POST http://h/v1/chat/completions -> 500: b'upstream command exited prematurely'"))
    with pytest.raises(clf.LLMUnavailable, match="failed 5 requests in a row"):
        stages.classify(workers=1)
    assert _status(tmp_path) == {"extracted": 12}                     # all still waiting: the next run retries them


def test_a_bad_document_is_an_error_and_the_rest_carry_on(stages, tmp_path, monkeypatch):
    def one(tax, row, model, vision=True):
        if row["name"] == "3.pdf":
            return row["path"], None, "ValueError: no JSON in model reply"
        if row["name"] == "5.pdf":
            return row["path"], None, "TimeoutError: timed out"       # one blip: left for next time, run continues
        return row["path"], {"disposition": "file"}, None
    monkeypatch.setattr(stages, "_classify_one", one)
    stages.classify(workers=1)
    assert _status(tmp_path) == {"classified": 10, "error": 1, "extracted": 1}


def test_a_model_that_will_not_load_stops_before_any_file_is_touched(stages, tmp_path, monkeypatch):
    def boom(model=None):
        raise clf.LLMUnavailable("model 'm' is listed but will not start")
    monkeypatch.setattr(stages.clf, "preflight", boom)
    with pytest.raises(clf.LLMUnavailable):
        stages.classify(workers=3)
    assert _status(tmp_path) == {"extracted": 12}


def test_go_file_waits_for_paperless_before_archiving(stages, monkeypatch):
    """Right after an upload nearly everything is still being consumed; archiving then would move almost nothing."""
    rounds = iter([{"done": 10, "pending": 236, "failed": 0}, {"done": 102, "pending": 134, "failed": 0}, {"done": 134, "pending": 0, "failed": 0}])
    slept = []
    monkeypatch.setattr(stages, "finalize", lambda: next(rounds))
    assert stages.finalize_wait(minutes=45, sleep=slept.append) == {"done": 246, "pending": 0, "failed": 0}
    assert slept == [30, 30]
    monkeypatch.setattr(stages, "finalize", lambda: {"done": 0, "pending": 5, "failed": 0})
    assert stages.finalize_wait(minutes=0, sleep=slept.append)["pending"] == 5        # out of time: says so and returns


def test_archive_only_moves_files_under_the_source_it_was_pointed_at(stages, tmp_path, monkeypatch):
    """A statement archive another tool owns was scanned and filed too; archiving Downloads must not empty it."""
    from pintake import db
    downloads, bank, dest = tmp_path / "Downloads", tmp_path / "Documents" / "bank" / "chase", tmp_path / "Downloads" / "archived"
    downloads.mkdir(); bank.mkdir(parents=True)
    (downloads / "receipt.pdf").write_bytes(b"%PDF a"); (bank / "8558_2026-10-30_Statement.pdf").write_bytes(b"%PDF b")
    con = db.connect()
    con.execute("DELETE FROM files")
    for i, p in enumerate((downloads / "receipt.pdf", bank / "8558_2026-10-30_Statement.pdf")):
        con.execute("INSERT INTO files (path, name, mtime, status, paperless_id) VALUES (?,?,?,?,?)", (str(p), p.name, 1000, "uploaded", 10 + i))
    con.commit()
    monkeypatch.setattr(stages, "get_json", lambda url, headers=None, timeout=120: {"results": [{"id": 10}, {"id": 11}], "next": None})
    stages.archive(dest=str(dest), source=str(downloads))
    assert (dest / "receipt.pdf").exists() and not (downloads / "receipt.pdf").exists()
    assert (bank / "8558_2026-10-30_Statement.pdf").exists() and not (dest / "8558_2026-10-30_Statement.pdf").exists()
    paths = dict(db.connect().execute("SELECT name, path FROM files"))
    assert paths["8558_2026-10-30_Statement.pdf"] == str(bank / "8558_2026-10-30_Statement.pdf")
