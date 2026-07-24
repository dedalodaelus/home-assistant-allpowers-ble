# Adding a new model

Adding a model is a protocol-validation task, not a name-list change.

## 1. Identify the exact hardware

Record the complete product name, revision, firmware, advertised name, and any
regional variant. Photograph labels for your own reference, but remove serial
numbers before sharing them.

## 2. Verify the GATT contract

Confirm the service and characteristic UUIDs, properties, notification behavior,
and whether the device requires pairing or authentication. The existing transport
expects:

- service FFF0;
- FFF1 notifications;
- FFF2 writes without response.

A UUID match alone is insufficient.

## 3. Capture read-only behavior first

Start with advertisements, service discovery, the known status request, and
notifications. Do not transmit control frames until the payload and checksum are
understood.

Sanitize captures before committing or attaching them. Remove Bluetooth addresses,
serial numbers, timestamps that reveal occupancy, Home Assistant identifiers, and
unrelated traffic.

## 4. Add immutable models and vectors

Protocol changes belong in `protocol/`. Add representative byte vectors covering:

- minimum valid frame;
- realistic values;
- every documented flag;
- boundary values;
- unknown enum or flag values;
- bad header, length, checksum, and payload;
- fragmentation, concatenation, and noise.

Keep parsing independent from Home Assistant and BLE transport.

## 5. Prove write safety

For every write command determine whether fields are independent or combined.
Record the complete pre-write and post-write state. Verify that toggling one field
does not silently change another.

The preferred pattern is:

1. require a fresh device snapshot;
2. begin with the raw field value;
3. change only the known mask;
4. preserve unknown bits;
5. serialize writes;
6. clear temporary shadows on disconnect;
7. request and verify fresh state after the command.

Do not add a guessed default frame as a fallback.

## 6. Extend model policy

Update `model_support.py` only after protocol evidence exists. Keep explicit
rejections ahead of broad family matches. Add tests for capitalization, missing
names, exact rejected revisions, and generic service-UUID candidates.

## 7. Add Home Assistant entities

Use existing platforms and entity descriptions. New entities should:

- have stable unique IDs;
- use device classes and units where applicable;
- derive availability from relevant freshness;
- be disabled by default when diagnostic, advanced, or experimental;
- translate errors into `HomeAssistantError` for service calls;
- include English and Spanish translation keys.

## 8. Update user-facing material

Update the compatibility matrix, entity table, protocol notes, diagnostics, tests,
and changelog. State whether support is verified or experimental and identify the
specific hardware revision.

## 9. Validate on failure paths

Test unplugged proxies, route changes, Bluetooth restarts, stale telemetry, partial
notifications, rapid consecutive commands, Home Assistant shutdown, and integration
reload. A model is not ready merely because one happy-path read succeeds.
