from logai2.owner.miners import Miners


def test_ids_are_per_service(cfg):
    m = Miners(cfg.drain3, {})
    a = m.match("lixi", "connect timeout host <*>")
    b = m.match("topup", "connect timeout host <*>")
    assert a[0] == "lixi:T00001" and b[0] == "topup:T00001" and a[2] and b[2]
    assert m.match("lixi", "connect timeout host <*>")[0] == "lixi:T00001"


def test_state_round_trip(cfg):
    m = Miners(cfg.drain3, {}); m.match("lixi", "a b c")
    blobs = m.dirty()
    assert set(blobs) == {"lixi"} and m.dirty() == {}
    m2 = Miners(cfg.drain3, blobs)
    assert m2.match("lixi", "a b c") == ("lixi:T00001", "a b c", False)


def test_placeholder_display(cfg):
    assert "<*>" in Miners(cfg.drain3, {}).match("s", "user <*> logged in")[1]
