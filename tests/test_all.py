import os
import tempfile

os.environ["DIAC_PROVIDERS"] = "echo"
os.environ["DIAC_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from fastapi.testclient import TestClient

from diacritizer import api
from diacritizer.providers import BudgetExceeded, EchoProvider, Router
from diacritizer.text import diacritic_error_rate, enforce_diacritics_only, strip_diacritics

G = "ذَهَبَ الطَّالِبُ إِلَى الْمَدْرَسَةِ صَبَاحًا."
P = strip_diacritics(G)


def test_exact_passthrough():
    out, info = enforce_diacritics_only(P, G)
    assert out == G and info["exact"]


def test_guard_rejects_letter_changes_but_keeps_marks():
    bad = "إليك النص: " + G.replace("إ", "ا").replace("ة", "ه")
    out, info = enforce_diacritics_only(P, bad)
    assert strip_diacritics(out) == P
    assert not info["exact"] and info["aligned"] > 0.9 * len(P)


def test_existing_marks_preserved():
    partially = "ذَهب الطالب إلى المدرسة صباحا."
    out, _ = enforce_diacritics_only(partially, P)  # model removed marks
    assert out.startswith("ذَهب")


def test_der():
    assert diacritic_error_rate(G, G) == 0
    assert diacritic_error_rate(G, P) > 0.5


def test_router_fallback_and_budget():
    broken = EchoProvider(script=lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    good = EchoProvider(script=lambda t: G)
    out, info, res, errors = Router([broken, good]).run(P)
    assert out == G and errors == ["echo:RuntimeError"]
    r = Router([good], daily_budget_usd=0)
    try:
        r.run(P)
        assert False
    except BudgetExceeded:
        pass


def test_api_roundtrip():
    api.router.providers = [EchoProvider(script=lambda t: G)]
    c = TestClient(api.app)
    r = c.post("/v1/diacritize", json={"text": P}).json()
    assert r["text"] == G and r["guard"]["exact"]
    bad = c.post("/v1/corrections", json={"request_id": r["request_id"], "corrected": "كلام آخر"})
    assert bad.status_code == 422
    fixed = G.replace("ذَهَبَ", "ذَهَبْ")
    ok = c.post("/v1/corrections", json={"request_id": r["request_id"], "corrected": fixed, "editor": "qa1"}).json()
    assert 0 < ok["der_of_model_output"] < 0.2
    lines = c.get("/v1/corrections/export").text.strip().splitlines()
    assert len(lines) == 1 and '"editor": "qa1"' in lines[0]
    assert c.get("/v1/stats").json()["corrections"] == 1
