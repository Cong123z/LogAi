from prometheus_client import generate_latest

from logai2.metrics import Metrics, rss_mib, trim


def row(state, kind="template", doc_id=None):
    return {"service": "s", "kind": kind, "doc_id": doc_id, "state": state}


def test_row_back_to_normal_leaves_log_alert_state():
    m = Metrics(None)
    m.export_rows([row("ALERTING"), row("WARMING", "document", "D1"), row("ALERTING", "service")])

    def value(doc_id, state):
        return m.registry.get_sample_value("log_alert_state", {"service": "s", "doc_id": doc_id, "state": state})
    assert value("undocumented", "ALERTING") == 1 and value("D1", "WARMING") == 1 and value("service", "NORMAL") == 0
    m.export_rows([row("ALERTING", "service")])   # the others returned to NORMAL: alert_rows drops them
    assert value("undocumented", "ALERTING") is None and value("D1", "WARMING") is None
    assert value("service", "ALERTING") == 1


def test_process_memory_exported():
    m = Metrics(None)
    assert b"process_resident_memory_bytes" in generate_latest(m.registry)


def test_rss_mib_positive():
    assert rss_mib() > 0
    trim()   # never raises
