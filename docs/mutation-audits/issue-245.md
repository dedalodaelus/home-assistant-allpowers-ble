# Issue 245: targeted BLE safety mutation audit

This is a bounded offline audit of existing BLE safety assertions. Mutations
were applied one at a time to production code, tested, and reverted before the
next mutation. No mutation tooling or production changes remain.

Baseline on Python 3.14, before and after the assertion changes:

- `USE_REAL_HOMEASSISTANT=0 pytest tests/test_protocol_codec.py tests/test_client_logic.py tests/test_client_runtime.py tests/test_gatt_topology.py` — 312 passed.
- `USE_REAL_HOMEASSISTANT=1 pytest tests/homeassistant/test_lifecycle_runtime.py` — 61 passed.

## Mutations

| ID | Invariant and precise mutation | Baseline test | Mutant outcome and behavioral reason | Corrective assertion |
| --- | --- | --- | --- | --- |
| M1 | Combined output bitmap: change `codec.py::encode_output_control` from `flags |= OUTPUT_LIGHT_MASK` to `flags |= OUTPUT_AC_MASK`. | `tests/test_protocol_codec.py::test_encode_output_vectors[False-False-True-32-81]`; `...[True-True-True-35-82]` | Killed: on-wire flags at byte 7 changed from `0x20`/`0x23` to `0x02`/`0x03`, and checksums changed from `0x51`/`0x52` to `0x73`/`0x72`. | Existing exact-frame and XOR-checksum assertion; none needed. |
| M2 | Transaction ordering: in `client.py::_safe_output_snapshot`, replace `pending = self._pending_output_transaction` with `pending = None`. | `tests/test_client_logic.py::test_transaction_rejects_unconfirmed_dependent_write[output]` | Killed: the dependent DC command sent a second frame (`0x21`) while the AC transaction was unconfirmed; two writes were recorded instead of one. | Added a `finally` assertion requiring exactly one GATT write, including when the expected rejection is missing. |
| M3 | Keepalive replay: in `client.py::_safe_settings_snapshot`, bypass the pending-transaction guard when `control_kind == "keepalive"`. | `tests/test_client_logic.py::test_transaction_keepalive_rejects_pending_settings` | Killed: the keepalive sent the current stale settings as a second frame (`0xA0`) while the pending target (`0xA1`) was unconfirmed. | Added a `finally` assertion requiring exactly one GATT write. |
| M4 | Contradictory confirmation: in the output notification match, omit `packet.dc_enabled == pending_output.target_dc`. | `tests/test_client_logic.py::test_consecutive_output_commands_handle_delayed_and_duplicate_confirmations` | Killed: a notification with DC off was accepted for a target with DC on and cleared the pending transaction. | Existing transaction-state assertion detects that the contradictory notification must leave the transaction pending. |
| M5 | Late confirmation: replace the output notification deadline decision `elif now > pending_output.confirm_deadline_monotonic` with `elif False`. | `tests/test_client_logic.py::test_transaction_expiration_during_write_keeps_group_ambiguous[False-output]` | Killed: telemetry after the deadline confirmed the transaction instead of blocking writes until a fresh status; the expected block version `2` was absent. | Existing block-version and subsequent-write assertions; none needed. |
| M6 | Read-only capability: replace the production capability/presence guard in `client.py::_require_feature_write_capability_unlocked` with an unconditional return. | `tests/test_client_logic.py::test_write_commands_are_rejected_for_read_only_profiles` | Killed: the read-only profile attempted an output-control GATT write. | Added a `finally` assertion requiring zero GATT writes. |
| M7 | Session freshness: in `client.py::_runtime_model_support`, allow stored settings revision reuse whenever `_settings` exists, without requiring `_settings_are_fresh()`. | `tests/test_client_runtime.py::test_reconnect_requires_current_session_settings_for_write_capability` | Killed: prior-session revision evidence authorized an output-control frame after reconnect; the trace contained the initial status request plus an unauthorized output write. | Moved the capability assertion after the write attempt and added a `finally` assertion requiring only the initial status request. |
| M8 | GATT ownership: in `client.py::_required_characteristics`, replace service-scoped characteristic lookup with collection-wide lookup. | `tests/test_gatt_topology.py::test_runtime_rejects_misplaced_characteristics_before_io[members0]`, `[members1]`, `[members2]` | Killed: characteristics from the wrong service reached notification setup instead of being rejected before I/O. | Added a `finally` assertion requiring no notification target and no GATT writes. |
| M9 | GATT properties: remove the `notify`/`indicate` property check in `client.py::_required_characteristics`. | `tests/test_client_runtime.py::test_connection_rejects_invalid_gatt_properties_before_io[notify-properties0]`, `[notify-properties1]` | Killed: a characteristic without notification capability reached notification setup. The invalid write-property cases remained rejected. | Added `finally` assertions requiring no notification callback or GATT write. |

## Unusable attempt

The M3 guard bypass was first run against
`test_automatic_keepalive_does_not_replay_pending_settings`. With the guard
removed, the expected recovery reconnect did not occur and the maintenance loop
did not terminate. That attempt was interrupted and is **not counted as a
kill**. The same mutation was then run against the direct keepalive test in M3,
which completed and proved the extra GATT write.

All mutation outcomes above are deterministic fake-BLE regression evidence.
They do not establish physical device behavior or HIL qualification.
