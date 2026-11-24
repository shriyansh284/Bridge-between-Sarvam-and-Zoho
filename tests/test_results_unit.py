"""How Sarvam Analytics records become call results (ams/results.py), without any servers."""
from datetime import datetime, timedelta

from ams import results
from ams.results import _best, parse_utc, to_payload

NOW = datetime(2026, 11, 24, 10, 0, 0)
WAITING = {"campaign_id": "AMS-1", "row_id": "R0001", "ref": "r1", "triggered_at": "2026-11-24T09:50:00"}
OUTPUTS = {"disposition_call_disposition": "agreed_to_submit", "summary": "Will upload tomorrow"}


def rec(status, end=None, attempted="2026-11-24T09:55:00", retry=1, duration=60, outputs=None, interaction="20261124/x"):
    return {"attempt_id": "A1", "connectivity_status": status, "end_datetime": end, "attempted_at": attempted,
            "server_retry_attempt": retry, "duration_in_seconds": duration, "failure_reason": "NO_FAILURE_REASON",
            "agent_variables": outputs, "interaction_id": interaction if status == "connected" else None,
            "audio_url": "https://apps.sarvam.ai/api/analytics/public/x?sig=1" if status == "connected" else None}


def budget():
    return {"transcripts": 20}


def test_parse_utc_accepts_what_sarvam_and_python_310_disagree_on():
    assert parse_utc("2026-10-07T05:14:31") == datetime(2026, 10, 7, 5, 14, 31)
    assert parse_utc("2026-10-07T05:14:31.12345") == datetime(2026, 10, 7, 5, 14, 31, 123450)   # 5 digits
    assert parse_utc("2026-10-07T05:14:31.1234567Z") == datetime(2026, 10, 7, 5, 14, 31, 123456)
    assert parse_utc("2026-10-07T10:44:31+05:30") == datetime(2026, 10, 7, 5, 14, 31)            # to UTC
    assert parse_utc("not a date") is None and parse_utc(None) is None


def test_answered_call_wins_and_a_newer_try_is_not_hidden_by_an_older_failure():
    answered = rec("connected", end="2026-11-24T09:57:00", outputs=OUTPUTS)
    assert _best([rec("failed"), answered]) is answered                       # same try listed twice
    short = rec("connected", end="2026-11-24T09:56:00", duration=3, outputs=OUTPUTS)
    assert _best([short, answered]) is answered                                # the real conversation, not a drop
    ringing = rec(None, retry=2, attempted="2026-11-24T09:58:00")
    assert _best([rec("no_answer", retry=1), ringing]) is ringing              # platform retry still going
    assert to_payload(_best([rec("no_answer", retry=1), ringing]), WAITING, NOW, budget()) is None


def test_in_progress_and_unfinished_calls_wait():
    assert to_payload(rec(None), WAITING, NOW, budget()) is None                       # ringing
    assert to_payload(rec("connected", end=None, outputs=OUTPUTS), WAITING, NOW, budget()) is None  # still talking
    long_ago = rec("connected", end=None, attempted="2026-11-24T09:00:00", outputs=OUTPUTS)
    assert to_payload(long_ago, WAITING, NOW, budget()) is not None            # never got an end time: taken anyway


def test_waits_for_answers_and_transcript_right_after_hang_up(monkeypatch):
    monkeypatch.setattr(results, "fetch_transcript", lambda iid: [{"role": "agent", "text": "Hello"}])
    just_ended = "2026-11-24T09:59:00"
    assert to_payload(rec("connected", end=just_ended, outputs={}), WAITING, NOW, budget()) is None   # answers not saved yet
    p = to_payload(rec("connected", end=just_ended, outputs=OUTPUTS), WAITING, NOW, budget())
    assert p["status"] == "connected" and p["final_agent_variables"] == OUTPUTS and p["interaction_transcript"]
    assert p["webhook_config"]["metadata"] == {"campaign_id": "AMS-1", "row_id": "R0001", "ref": "r1"}

    monkeypatch.setattr(results, "fetch_transcript", lambda iid: [])           # transcript not saved yet
    assert to_payload(rec("connected", end=just_ended, outputs=OUTPUTS), WAITING, NOW, budget()) is None
    old = rec("connected", end="2026-11-24T09:40:00", outputs=OUTPUTS)         # 20 min later: take it without
    assert to_payload(old, WAITING, NOW, budget())["interaction_transcript"] == []

    def broken(iid):
        raise results.SarvamError("Sarvam API 503", retryable=True, status_code=503)
    monkeypatch.setattr(results, "fetch_transcript", broken)
    assert to_payload(rec("connected", end=just_ended, outputs=OUTPUTS), WAITING, NOW, budget()) is None
    assert to_payload(old, WAITING, NOW, budget())["interaction_transcript"] is None


def test_transcript_limit_per_poll_defers_the_rest(monkeypatch):
    monkeypatch.setattr(results, "fetch_transcript", lambda iid: [{"role": "agent", "text": "Hi"}])
    b = {"transcripts": 1}
    old = rec("connected", end="2026-11-24T09:40:00", outputs=OUTPUTS)
    assert to_payload(old, WAITING, NOW, b) is not None
    assert to_payload(old, WAITING, NOW, b) is None                             # next poll


def test_missed_calls_carry_no_outputs():
    p = to_payload(rec("busy", outputs={"name": "x"}), WAITING, NOW, budget())
    assert p["status"] == "busy" and p["final_agent_variables"] is None and p["failure_reason"] is None
    assert p["duration"] is None and p["audio_url"] is None
