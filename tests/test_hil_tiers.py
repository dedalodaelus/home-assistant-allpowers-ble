"""Support tiers remain scoped to reviewed, route-bound physical evidence."""

from copy import deepcopy

from scripts.hil.tiers import evaluate_tiers


def _reviewed(status="pass", states=None):
    return {
        "status": status,
        "evidence": ["evidence.jsonl"],
        "physical_review": {
            "outcome": "confirmed_pass" if status == "pass" else "confirmed_fail",
            "reviewed_by": "reviewer",
            "evidence_sha256": {"evidence.jsonl": "a" * 64},
            **({"verified_states": states} if states is not None else {}),
        },
    }


def _run(route="active_proxy", firmware="1.0", captured="2026-01-01T00:00:00Z"):
    return {
        "route": route,
        "firmware_version": firmware,
        "captured_at": captured,
        "authorization_context": "production",
        "qualification_status": "pass",
        "bronze_evidence": {
            key: _reviewed()
            for key in ("identity", "protocol_probe", "fresh_telemetry")
        },
        "feature_presence": {
            "ac_output": {"status": "present"},
            "dc_output": {"status": "present"},
            "light_output": {
                "status": "absent",
                "reviewed_by": "reviewer",
                "reviewed_at": "2026-01-01T00:00:00Z",
                "evidence": ["absence.json"],
                "evidence_sha256": {"absence.json": "b" * 64},
            },
            "eco_mode": {"status": "unknown"},
            "work_mode": {"status": "unknown"},
            "eco_timeout": {"status": "unknown"},
            "car_charger": {"status": "unknown"},
        },
        "write_capabilities": {},
        "scenarios": {},
    }


def _matrix(*runs):
    return {
        "schema_version": 3,
        "revisions": [
            {
                "id": "r1500-lite-hw01",
                "model": "ALLPOWERS R1500 LITE",
                "hardware_version": "0.1",
                "runs": list(runs),
            }
        ],
    }


def test_scope_does_not_mix_routes_or_firmware():
    result = evaluate_tiers(_matrix(_run("active_proxy"), _run("local_adapter", "2.0")))
    assert len(result["tiers"]) == 2
    assert {item["tier"] for item in result["tiers"]} == {"bronze"}
    assert {item["scope"]["firmware"] for item in result["tiers"]} == {"1.0", "2.0"}


def test_later_reviewed_failure_supersedes_feature_pass():
    first = _run()
    first["write_capabilities"]["ac_output"] = _reviewed("pass")
    later = deepcopy(first)
    later["captured_at"] = "2026-02-01T00:00:00Z"
    later["write_capabilities"]["ac_output"] = _reviewed("fail")
    result = evaluate_tiers(_matrix(first, later))["tiers"][0]
    assert "ac_output" not in result["qualified_features"]
    assert result["tier"] == "bronze"


def test_unreviewed_absence_does_not_remove_silver_blocker():
    run = _run()
    run["feature_presence"]["light_output"] = {"status": "absent"}
    result = evaluate_tiers(_matrix(run))["tiers"][0]
    assert result["feature_presence"]["light_output"] == "unknown"
    assert any("light_output" in reason for reason in result["unmet_requirements"])


def test_no_scoped_evidence_is_explicitly_unqualified():
    result = evaluate_tiers(_matrix())["tiers"][0]
    assert result["tier"] == "unqualified"
    assert result["scope"]["observed_route"] is None


def test_ac_off_only_does_not_qualify_a_silver_output():
    run = _run()
    run["write_capabilities"]["ac_output"] = _reviewed("pass", ["off"])
    run["scenarios"]["reconnect_failover"] = _reviewed()
    result = evaluate_tiers(_matrix(run))["tiers"][0]
    assert result["tier"] == "bronze"
    assert "ac_output" not in result["qualified_features"]
