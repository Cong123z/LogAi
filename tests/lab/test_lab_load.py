from datetime import date, datetime

from logai2.timeutil import TZ
from scripts.lab.data import UNOBSERVED, minute_at
from scripts.lab.load import load_service, services
from tests.test_links import doc, link, tmpl

D1, D2 = date(2026, 10, 13), date(2026, 10, 14)


def put(conn, key, day, hour, counts, service="s"):
    conn.execute("INSERT INTO lab_minute_counts VALUES (%s, %s, %s, %s, %s)", (service, key, day, hour, counts))


def hour(*pairs):
    c = [0] * 60
    for i, n in pairs:
        c[i] = n
    return c


def fill(conn):
    for d in (D1, D2):
        for h in range(24):
            if (d, h) != (D2, 5):                      # hour 5 of D2 is not observed
                put(conn, "*minutes", d, h, [1] * 60)
    put(conn, "*total", D1, 0, hour((0, 7), (59, 2)))
    put(conn, "A", D1, 0, hour((0, 3)))
    put(conn, "B", D1, 0, hour((0, 4), (1, 1)))
    put(conn, "A", D2, 5, hour((3, 9)))                # recorded inside an unobserved hour: must read UNOBSERVED
    for d in (date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)):
        conn.execute("INSERT INTO hourly_counts VALUES ('s', 'A', %s, 3, 10, 50)", (d,))
        conn.execute("INSERT INTO service_hours VALUES ('s', %s, 3, 60)", (d,))
        conn.execute("INSERT INTO day_status VALUES ('s', %s, true, 'weekday')", (d,))
    conn.execute("INSERT INTO hourly_counts VALUES ('s', 'A', %s, 3, 1, 1)", (date(2026, 10, 9),))   # older than history
    conn.execute("INSERT INTO review_exclusions (service, day, hour_from, hour_to, decision) VALUES "
                 "('s', '2026-10-11', 3, 5, 'exclude'), ('s', '2026-10-11', 8, 9, 'keep')")
    tmpl(conn, "A", "ERROR"), tmpl(conn, "B", "INFO"), tmpl(conn, "C", "WARN")
    conn.execute("UPDATE templates SET first_seen = %s WHERE template_id = 'A'", (datetime(2026, 10, 12, 1, tzinfo=TZ),))
    doc(conn, "D1"), doc(conn, "D2")
    link(conn, "A", "D1"), link(conn, "B", "D1"), link(conn, "C", "D2", source="draft")


def test_load_exact_arrays_and_history(conn):
    fill(conn)
    conn.commit()
    assert services(conn, D1, D2) == ["s"] and services(conn, date(2026, 10, 20), date(2026, 10, 21)) == []
    sd = load_service(conn, "s", D1, D2, 3)
    assert sd.days == [D1, D2]
    a1, a2 = sd.minutes["A"][D1], sd.minutes["A"][D2]
    assert len(a1) == 1440 and a1[0] == 3 and a1[1] == 0 and a1[1439] == 0
    assert a2[5 * 60 + 3] == UNOBSERVED and a2[5 * 60] == UNOBSERVED and a2[4 * 60 + 59] == 0 and a2[6 * 60] == 0
    assert sd.minutes["*total"][D1][59] == 2 and sd.minutes["*total"][D2][0] == 0
    assert sd.observed[D1][0] == 1 and sd.observed[D2][5 * 60] == 0 and sd.observed[D2][6 * 60] == 1
    m = minute_at(D2, 5 * 60 + 3)
    assert sd.count("A", m) == UNOBSERVED and sd.count("A", minute_at(D1, 0)) == 3 and sd.count("nope", m) == UNOBSERVED
    # *error was never recorded: still present, zeros where observed, UNOBSERVED elsewhere
    assert sd.minutes["*error"][D1][0] == 0 and sd.minutes["*error"][D2][5 * 60] == UNOBSERVED
    # doc key: element-wise sum of its linked templates (A + B); the draft link is ignored
    assert sd.docs == {"doc:D1": ["A", "B"]}
    d1 = sd.minutes["doc:D1"][D1]
    assert d1[0] == 7 and d1[1] == 1 and d1[2] == 0
    assert sd.minutes["doc:D1"][D2][5 * 60 + 3] == UNOBSERVED
    assert "doc:D2" not in sd.minutes and "doc:D1" in sd.keys() and "*minutes" in sd.keys()
    # history goes back history_days
    assert set(sd.hourly) == {("A", date(2026, 10, d), 3) for d in (10, 11, 12)}
    assert sd.hourly[("A", date(2026, 10, 10), 3)] == (10, 50)
    assert set(sd.n_minutes) == {(date(2026, 10, d), 3) for d in (10, 11, 12)}
    assert sd.committed == {date(2026, 10, d) for d in (10, 11, 12)}
    assert sd.excluded == {(date(2026, 10, 11), 3), (date(2026, 10, 11), 4)}
    assert sd.levels == {"A": "ERROR", "B": "INFO", "C": "WARN"}
    assert sd.first_seen == {"A": minute_at(date(2026, 10, 12), 60), "B": sd.first_seen["B"], "C": sd.first_seen["C"]}


def test_load_runs_read_only(conn):
    fill(conn)
    conn.commit()
    conn.execute("SET TRANSACTION READ ONLY")
    sd = load_service(conn, "s", D1, D2, 3)
    assert sd.minutes["A"][D1][0] == 3
    conn.rollback()
