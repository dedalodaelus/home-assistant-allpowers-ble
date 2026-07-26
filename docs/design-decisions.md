# Design decisions

> [!NOTE]
> These original informal decisions are retained as source evidence. The
> structured decision log is available in
> [`docs/architecture/decisions/`](architecture/decisions/README.md).

## Use Home Assistant Bluetooth as the only transport entry point

**Decision:** Resolve `BLEDevice` instances through Home Assistant with
`connectable=True` and establish connections through `bleak-retry-connector`.

**Reason:** Home Assistant owns adapter and ESPHome proxy routing. Direct BlueZ
access would bypass that routing, reduce portability, and make failover harder.

**Consequence:** The integration targets Home Assistant rather than generic Python
execution. The protocol package remains generic.

## Keep a persistent active connection

**Decision:** Maintain one active GATT connection per configured station.

**Reason:** The station exposes notifications and writable characteristics; output
and settings controls need timely current state. Reconnecting for every operation
would increase latency, radio traffic, and contention.

**Risk control:** Exponential backoff, route re-resolution, watchdog recovery, and
idempotent shutdown bound failure behavior.

## Classify the integration as local polling

**Decision:** Use `local_polling` in the manifest.

**Reason:** Data travels locally and arrives as notifications, but the integration
periodically writes an explicit status request. State discovery is therefore not
pure unsolicited push.

## Require an active probe during setup

**Decision:** Do not create an entry from an advertisement alone.

**Reason:** Product names and 16-bit vendor UUIDs can overlap across revisions. A
valid status response proves substantially more than a name match.

**Trade-off:** Setup takes a real connection slot and may fail on weak links. This
is preferable to sending commands to an incompatible device.

## Keep protocol code inside the integration but independent from Home Assistant

**Decision:** Use a pure `protocol` package instead of a separate PyPI dependency in
the first release.

**Reason:** The protocol currently supports one verified model and evolves with the
integration. An external dependency would add release coordination without adding
reuse today.

**Future threshold:** Extract a library when another consumer or multiple protocol
families justify independent versioning.

## Cache state in memory only

**Decision:** Persist config and options, not telemetry, counters, parser fragments,
or command shadows.

**Reason:** These values are ephemeral and unsafe to trust after restart. Avoiding
recorder-like writes also reduces storage churn.

## Preserve unknown protocol data

**Decision:** Retain unknown packets and raw settings/status flags, and modify known
bits through read-modify-write.

**Reason:** Reverse-engineered protocols often contain revision-specific fields.
Zeroing or normalizing unknown bits can change behavior that the integration does
not understand.

## Reject unsafe writes

**Decision:** Make controls unavailable and raise an error when the relevant state
is stale, absent, or from a disconnected session.

**Reason:** A guessed command can disable another shared output or settings flag.
Availability is a safety boundary, not only a UI preference.

## Keep keepalive and car-charger behavior opt-in

**Decision:** Disable both by default and mark related entities advanced or
experimental.

**Reason:** They are less universally verified than core R600 telemetry and output
control. Users should choose the additional traffic or writable bit explicitly.

## Apply options without reload

**Decision:** Validate and apply connection-health options to the running client.

**Reason:** Tuning intervals should not create avoidable disconnects. Entity
availability reacts immediately to the new thresholds.
