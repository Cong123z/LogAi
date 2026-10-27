from array import array
from datetime import date

from scripts.lab.data import MIX, UNOBSERVED, ServiceData, minute_at, slot_of
from scripts.lab.kinds import KINDS, assign, has_new, is_new

D1, D2 = date(2026, 10, 13), date(2026, 10, 14)


def arr(fill=0, **at):
    a = array("i", [fill] * 1440)
    for slot, n in at.items():
        a[int(slot[1:])] = n
    return a


def busy(rate):      # `rate`/min during hour 3 only, zero elsewhere
    a = arr()
    for i in range(180, 240):
        a[i] = rate
    return a


def make():
    keys = {"*total": busy(5), "*error": busy(1), "*warn": busy(1), "*info": busy(1), "*minutes": arr(1),
            "big": busy(4), "quiet_err": busy(1), "quiet_warn": busy(1), "quiet_info": busy(1),
            "svc:unparsed": busy(9), "doc:D": busy(1), "gone_err": arr(UNOBSERVED), "gone_info": arr(UNOBSERVED)}
    sd = ServiceData("s", [D1, D2], {k: {D1: v, D2: arr()} for k, v in keys.items()}, {D1: arr(1), D2: arr(1)},
                     {}, {}, set(), set(),
                     {"big": "INFO", "quiet_err": "ERROR", "quiet_warn": "WARNING", "quiet_info": "INFO",
                      "gone_err": "ERROR", "gone_info": "INFO"},
                     {"quiet_err": 1000}, {"doc:D": ["big"]})
    return sd


def test_assign_kinds():
    sd = make()
    for k in ("gone_err", "gone_info"):
        sd.minutes[k][D2] = arr(UNOBSERVED)
    assert assign(sd, [D1]) == {"*total": "M1", "*error": "M2", "doc:D": "M5", "big": "M3",
                                "quiet_err": "M4", "quiet_warn": "M4", "gone_err": "M4"}
    assert KINDS == ("M1", "M2", "M3", "M4", "M5", "M6", "M7") and MIX == "*mix"


def test_busy_hours_only_and_unobserved_minutes_ignored():
    sd = make()
    sd.minutes["big"][D1] = arr()
    for i in range(180, 240):          # 1/min busy hour, so < 2: INFO -> not scored
        sd.minutes["big"][D1][i] = 1
    assert "big" not in assign(sd, [D1])
    for i in range(180, 210):          # 3/min over observed half hour, rest unobserved -> mean 3
        sd.minutes["big"][D1][i] = 3
    for i in range(210, 240):
        sd.minutes["big"][D1][i] = UNOBSERVED
    assert assign(sd, [D1])["big"] == "M3"


def test_is_new_exactly_1440_minutes():
    sd = make()
    f = sd.first_seen["quiet_err"]
    assert is_new(sd, "quiet_err", f) and is_new(sd, "quiet_err", f + 1439)
    assert not is_new(sd, "quiet_err", f + 1440) and not is_new(sd, "quiet_err", f - 1)
    assert not is_new(sd, "big", f) and not is_new(sd, "*total", f)


def test_slot_roundtrip_across_local_midnight():
    m = minute_at(D1, 0)
    assert slot_of(m) == (D1, 0) and slot_of(m - 1) == (date(2026, 10, 12), 1439)
    for slot in (0, 1, 719, 1439):
        assert slot_of(minute_at(D2, slot)) == (D2, slot)
    assert minute_at(D2, 0) - minute_at(D1, 0) == 1440


def test_m6_for_new_templates_otherwise_unscored():
    sd = make()
    assert "quiet_info" not in assign(sd, [D1]) and not has_new(sd, "quiet_info")
    sd.first_seen["quiet_info"] = minute_at(D2, 100)           # born inside the window, INFO < 2/min
    sd.first_seen["old_info"] = minute_at(D1, 0) - 5000          # 'new' period ended before the window
    sd.first_seen["gone_info"] = minute_at(D1, 0) - 1000         # still new at the window start
    sd.minutes["old_info"] = {D1: busy(1), D2: arr()}
    sd.levels["old_info"] = "INFO"
    kinds = assign(sd, [D1])
    assert kinds["quiet_info"] == "M6" and "old_info" not in kinds and kinds["gone_err"] == "M4"
    assert has_new(sd, "gone_info") and not has_new(sd, "old_info") and not has_new(sd, "*total")
    assert "gone_info" not in kinds or kinds["gone_info"] == "M6"


def test_service_data_level():
    sd = make()
    sd.levels.update({"w": "WARNING"})
    sd.docs.update({"doc:X": ["big", "w"], "doc:Y": ["quiet_err", "w", "big"], "doc:Z": []})
    assert sd.level("big") == "INFO" and sd.level("quiet_err") == "ERROR" and sd.level("nope") == "INFO"
    assert sd.level("*total") == "INFO" and sd.level("*error") == "ERROR"
    assert sd.level("doc:X") == "WARNING" and sd.level("doc:Y") == "ERROR" and sd.level("doc:Z") == "INFO"
