# Issue 245: targeted BLE safety mutation audit

This is a bounded offline audit of existing BLE safety assertions. Mutations
were applied one at a time to production code, tested, and reverted before the
next mutation. No mutation tooling or production changes remain.

Baseline on Python 3.14, before and after the assertion changes:

- `USE_REAL_HOMEASSISTANT=0 pytest tests/test_protocol_codec.py tests/test_client_logic.py tests/test_client_runtime.py tests/test_gatt_topology.py` — 312 passed.
- `USE_REAL_HOMEASSISTANT=1 pytest tests/homeassistant/test_lifecycle_runtime.py` — 61 passed.

## Mutations

All nine mutants below were already detected by the original tests before this
PR. This audit therefore does not claim that the new assertions newly kill
these mutants. For M2, M3, M6, M8, and M9, the original `pytest.raises`
expectations detected the missing rejection; the added `finally` assertions
make the forbidden GATT write or notification setup directly observable. For
M7, the original capability assertion detected the mutant, while this PR also
moves the attempted write before that assertion and verifies the GATT trace,
providing direct behavioral evidence for stale-session safety.

| ID | Invariant and precise mutation | Original test and outcome | What this PR adds |
| --- | --- | --- | --- |
| M1 | Combined output bitmap: change `codec.py::encode_output_control` from `flags |= OUTPUT_LIGHT_MASK` to `flags |= OUTPUT_AC_MASK`. | `tests/test_protocol_codec.py::test_encode_output_vectors[False-False-True-32-81]`; `...[True-True-True-35-82]` — already killed by the original exact-frame/checksum assertions; flags changed from `0x20`/`0x23` to `0x02`/`0x03`, with corresponding checksum changes. | No new assertion; existing exact-frame and XOR-checksum coverage remains the detector. |
| M2 | Transaction ordering: in `client.py::_safe_output_snapshot`, replace `pending = self._pending_output_transaction` with `pending = None`. | `tests/test_client_logic.py::test_transaction_rejects_unconfirmed_dependent_write[output]` — already killed by the original missing-rejection expectation; bypassing the guard also emitted a second DC frame (`0x21`). | Added a `finally` assertion requiring exactly one GATT write, explicitly attributing the forbidden side effect even if the rejection expectation fails. |
| M3 | Keepalive replay: in `client.py::_safe_settings_snapshot`, bypass the pending-transaction guard when `control_kind == "keepalive"`. | `tests/test_client_logic.py::test_transaction_keepalive_rejects_pending_settings` — already killed by the original missing-rejection expectation; bypassing the guard also emitted a second stale-settings frame (`0xA0`) while target (`0xA1`) was pending. | Added a `finally` assertion requiring exactly one GATT write, explicitly attributing the replay side effect. |
| M4 | Contradictory confirmation: in the output notification match, omit `packet.dc_enabled == pending_output.target_dc`. | `tests/test_client_logic.py::test_consecutive_output_commands_handle_delayed_and_duplicate_confirmations` — already killed by the original transaction-state assertion when contradictory DC-off telemetry cleared a DC-on target. | No new assertion; existing pending-transaction state check remains the detector. |
| M5 | Late confirmation: replace the output notification deadline decision `elif now > pending_output.confirm_deadline_monotonic` with `elif False`. | `tests/test_client_logic.py::test_transaction_expiration_during_write_keeps_group_ambiguous[False-output]` — already killed by the original expected block-version and subsequent-write assertions. | No new assertion; existing ambiguity/recovery checks remain the detector. |
| M6 | Read-only capability: replace the production capability/presence guard in `client.py::_require_feature_write_capability_unlocked` with an unconditional return. | `tests/test_client_logic.py::test_write_commands_are_rejected_for_read_only_profiles` — already killed by the original missing-rejection expectation; bypassing the guard allows the output command to attempt a write. | Added a `finally` assertion requiring zero GATT writes, explicitly attributing the side effect to an unauthorized control attempt. |
| M7 | Session freshness: in `client.py::_runtime_model_support`, allow stored settings revision reuse whenever `_settings` exists, without requiring `_settings_are_fresh()`. | `tests/test_client_runtime.py::test_reconnect_requires_current_session_settings_for_write_capability` — already killed by the original false write-capability assertion. With the mutation, prior-session revision evidence also allows an output frame after reconnect. | Moved the capability assertion after the attempted write and added a `finally` trace assertion requiring only the initial status request. This strengthens the test with direct behavioral evidence that stale evidence does not authorize a GATT write. |
| M8 | GATT ownership: in `client.py::_required_characteristics`, replace service-scoped characteristic lookup with collection-wide lookup. | `tests/test_gatt_topology.py::test_runtime_rejects_misplaced_characteristics_before_io[members0]`, `[members1]`, `[members2]` — already killed by the original missing-rejection expectation; the bypass lets misplaced characteristics reach setup. | Added `finally` assertions requiring no notification target and no GATT writes, explicitly attributing the forbidden setup/I/O effects. |
| M9 | GATT properties: remove the `notify`/`indicate` property check in `client.py::_required_characteristics`. | `tests/test_client_runtime.py::test_connection_rejects_invalid_gatt_properties_before_io[notify-properties0]`, `[notify-properties1]` — already killed by the original missing-rejection expectation; without the check, a characteristic lacking notification capability reaches setup. | Added `finally` assertions requiring no notification callback or GATT write, explicitly attributing the forbidden setup/I/O effects. |

## Unusable attempt

The M3 guard bypass was first run against
`test_automatic_keepalive_does_not_replay_pending_settings`. With the guard
removed, the expected recovery reconnect did not occur and the maintenance loop
did not terminate. That attempt was interrupted and is **not counted as a
kill**. The same mutation was then run against the direct keepalive test in M3,
which completed and recorded the extra GATT write. The original rejection
expectation already detected this mutation; the attempt confirmed the side
effect used to describe it in M3.

All mutation outcomes above are deterministic fake-BLE regression evidence.
They do not establish physical device behavior or HIL qualification.
