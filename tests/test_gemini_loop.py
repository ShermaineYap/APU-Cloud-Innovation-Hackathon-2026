"""The ask loop with a fake Gemini: bad SQL goes back as an error, Gemini fixes it, the answer cites rows."""
from dataclasses import dataclass, field
from types import SimpleNamespace

from google.genai import types

from campuspulse.agent import gemini


@dataclass
class FakeModels:
    script: list
    seen: list = field(default_factory=list)

    def generate_content(self, model, contents, config):
        self.seen.append(contents[-1])
        step = self.script.pop(0)
        if step[0] == "sql":
            call = types.FunctionCall(name="run_sql", args={"query": step[1]})
            content = types.Content(role="model", parts=[types.Part(function_call=call)])
            return SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content=content)], text=None)
        return SimpleNamespace(function_calls=[], candidates=[], text=step[1])


def test_ask_recovers_from_bad_sql(store, monkeypatch):
    fake = FakeModels([
        ("sql", "DELETE FROM readings"),
        ("sql", "SELECT room_id, SUM(kwh) AS kwh FROM v_empty_cooled GROUP BY room_id ORDER BY kwh DESC"),
        ("text", "Auditorium 2 wasted the most."),
    ])
    monkeypatch.setattr(gemini.settings.__class__, "gemini_enabled", property(lambda self: True))
    monkeypatch.setattr(gemini, "_client", lambda: SimpleNamespace(models=fake))

    out = gemini.ask(store, "Which room wasted the most?")
    assert out["answer"] == "Auditorium 2 wasted the most."
    assert out["rows"][0]["room_id"] == "A-02"
    assert [a["ok"] for a in out["attempts"]] == [False, True]
    error_part = fake.seen[1].parts[0].function_response.response
    assert "only SELECT" in error_part["error"]


def test_brief_drops_unknown_ids(monkeypatch):
    resp = SimpleNamespace(text='{"headline": "h", "summary": "s", "items": [{"id": "real"}, {"id": "made-up"}]}')
    monkeypatch.setattr(gemini.settings.__class__, "gemini_enabled", property(lambda self: True))
    monkeypatch.setattr(gemini, "_client", lambda: SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kw: resp)))
    acts = [{"id": "real", "kind": "right_size", "title": "t", "summary": "s", "kwh_per_week": 1,
             "rm_per_week": 0.5, "comfort": "", "confidence": 90, "confidence_level": "high", "rooms": []}]
    out = gemini.daily_brief(acts, {"wasted_kwh": 1})
    assert [i["id"] for i in out["items"]] == ["real"]
