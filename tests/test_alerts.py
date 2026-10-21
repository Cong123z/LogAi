from logai2.alerts import alert_rows


def st(key, state, since, service="s", count=1):
    return {"service": service, "key": key, "state": state, "since": since, "count": count,
            "band_low": 0.0, "band_high": 2.0}


def test_rows_group_by_document():
    rows = alert_rows(
        [st("s:T1", "ALERTING", 100), st("s:T2", "WARMING", 50), st("s:T3", "ALERTING", 200),
         st("*total", "COOLING", 10), st("s:T4", "NORMAL", 5), st("doc:DOC-1", "WARMING", 40)],
        {("s", "s:T1"): "DOC-1", ("s", "s:T2"): "DOC-1"}, {"DOC-1": "Restart pool"},
        {("s", "s:T1"): "pool <*> exhausted", ("s", "s:T3"): "disk full"})
    assert [(r["kind"], r["doc_id"], r["state"]) for r in rows] == [
        ("document", "DOC-1", "ALERTING"), ("template", None, "ALERTING"), ("service", None, "COOLING")]
    doc = rows[0]
    assert doc["title"] == "Restart pool" and doc["since"] == 40
    assert sorted(doc["keys"]) == ["doc:DOC-1", "s:T1", "s:T2"]
    assert [t["template_id"] for t in doc["templates"]] == ["s:T1", "s:T2"]
    assert doc["templates"][0] == {"template_id": "s:T1", "text": "pool <*> exhausted", "state": "ALERTING",
                                   "count": 1, "band_low": 0.0, "band_high": 2.0}
    assert rows[1]["templates"][0]["text"] == "disk full"
    assert rows[2]["templates"] == [] and rows[2]["keys"] == ["*total"]


def test_service_rows_split_by_service_and_doc_key_alone():
    rows = alert_rows([st("*error", "ALERTING", 5, "a"), st("*total", "WARMING", 9, "a"),
                       st("*total", "ALERTING", 1, "b"), st("doc:D", "WARMING", 3, "a")],
                      {}, {}, {})
    assert [(r["service"], r["kind"], r["state"], r["since"]) for r in rows] == [
        ("b", "service", "ALERTING", 1), ("a", "service", "ALERTING", 5), ("a", "document", "WARMING", 3)]
    assert rows[2]["doc_id"] == "D" and rows[2]["title"] is None and rows[2]["templates"] == []


def test_similar_undocumented_templates_become_one_cluster_row():
    states = [st("s:T1", "WARMING", 100), st("s:T2", "ALERTING", 50), st("s:T3", "ALERTING", 70),
              st("s:T4", "ALERTING", 60), st("o:T1", "ALERTING", 10, service="o")]
    edges = [("s", "s:T1", "s:T2"), ("s", "s:T2", "s:T4"),   # T4 is documented: never joined
             ("o", "o:T1", "o:T9")]
    rows = alert_rows(states, {("s", "s:T4"): "D1"}, {}, {}, edges)
    assert [(r["service"], r["kind"], sorted(r["keys"])) for r in rows] == [
        ("o", "template", ["o:T1"]), ("s", "cluster", ["s:T1", "s:T2"]),
        ("s", "document", ["s:T4"]), ("s", "template", ["s:T3"])]
    assert rows[1]["state"] == "ALERTING" and rows[1]["since"] == 50
