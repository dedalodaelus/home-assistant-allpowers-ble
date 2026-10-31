# Troubleshooting

## Device is discovered but setup says it is not connectable

The advertisement reached Home Assistant through a passive route, but no route can
open a GATT connection.

- Enable `bluetooth_proxy.active: true` on at least one ESPHome proxy.
- Confirm the local adapter is supported and not blocked.
- Move the device closer to a connectable proxy.
- Check that another application is not holding the device connection.

## No devices found

- Power on the station and open its Bluetooth function if the model requires it.
- Request setup while the device is advertising.
- Confirm Home Assistant sees the proxy or local adapter under Bluetooth diagnostics.
- Avoid relying only on a proxy that is isolated by VLAN or firewall rules.
- Restart the station's Bluetooth function before restarting all of Home Assistant.

## Device address or display name changed

Do not remove and recreate the entry unless recovery requires it.

- Use **Settings -> Devices & services -> ALLPOWERS BLE -> Reconfigure**.
- Reconfigure is for mutable entry data only: Bluetooth address and display name.
- The flow runs the same active probe contract before applying changes.
- If the selected address is already owned by another entry, the change is rejected.

Use **Options** only for runtime timing and keepalive values.

## Setup times out after connecting

The GATT connection opened, but no checksum-valid status frame arrived.

Possible causes:

- unsupported protocol revision;
- weak or unstable link;
- another client connected to the station;
- notification subscription failure;
- a device that exposes FFF0/FFF1/FFF2 for a different payload format.

Collect debug logs and sanitized diagnostics. Do not bypass the active probe: it is
the barrier that prevents a false-positive device from receiving control frames.

Every unsuccessful startup after client initialization explicitly shuts down the
coordinator/client and releases the advertisement subscription before returning
to Core. This includes startup/readiness errors, option reconciliation failures,
registry transition errors, platform initialization failures, and cancellation.
Connection, maintenance, and refresh tasks are stopped; the BLE connection is
released. Partially initialized platforms are unloaded, failed runtime data is
removed, and Core processes the remaining entry unload callbacks.

If partial-platform cleanup fails, the error is reported rather than treated as
successful setup; restart Home Assistant as instructed in Repairs. Same-device
registry records are not deleted by setup cleanup.

## Integration unload failed

Home Assistant puts an entry in the non-recoverable `FAILED_UNLOAD` state if
platform unloading returns `False` or platform/coordinator unloading raises an
exception. Resetting a reload flag cannot recover this Core state.
Core's own post-unload callbacks can also fail after the integration reports a
successful unload; the final-state observer reports that failure in Repairs too.

- Restart Home Assistant, not just the integration. Do not remove and recreate the
  entry: a restart retains registry IDs, user customizations, and references.
- Until restart, the integration suppresses update-triggered reloads, does not
  apply options to the partially unloaded runtime, and rejects reconfiguration
  without changing the entry. Options saved during this state remain pending.
- These protections also apply while `UNLOAD_IN_PROGRESS`: concurrent configuration
  and options updates remain pending without touching the unloading client or
  scheduling an additional reload. Reconfiguration is rejected until unloading
  finishes.
- A persistent error in **Settings -> System -> Repairs** explains the restart
  requirement. It is not an automatic repair and clears only after a successful
  new setup; fresh telemetry alone does not dismiss it.
  If the entry is deleted, its persistent unload Repair is removed as well.
- Diagnostics include `entry.state` and `entry.restart_required`. Check the unload
  error in the Home Assistant logs if the problem repeats.

The runtime and registrations retained by Core after a failed unload are not
treated as a cleanly unloaded integration. No second BLE client is started.

## Entities become unavailable while Connected remains on

This is expected when the socket remains open but telemetry or settings have
expired. The distinction prevents stale values from being treated as live.

- Check RSSI and proxy placement.
- Review protocol-error and watchdog counters.
- Use **Refresh status** once.
- Use **Reconnect** if the station stopped responding.
- Do not increase timeouts until the link problem is understood.

## Output control is unavailable

AC, DC, and light writes require a fresh status snapshot. Wait for telemetry or use
**Refresh status**. A write is intentionally rejected after reconnect until the new
session has supplied state.

## ECO or work-mode control is unavailable

Settings controls require a fresh settings notification, which may be less frequent
than status. Keepalive is off by default. Use the disabled-by-default **Send settings
keepalive** button only after a valid settings snapshot exists.

## Car charger switch is unavailable

The control is experimental and disabled by default. Enable it in the integration
options only for hardware on which the bit has been verified. The entity also
requires fresh settings.

## Frequent reconnects

- Improve BLE signal and reduce radio obstruction.
- Ensure only one controller is actively connected to the station.
- Check whether the proxy is rebooting or roaming between network paths.
- Keep the status interval at or above 10 seconds.
- Keep `stale_timeout` above the status interval and the watchdog above both.
- Review `last_error`, `parser_discards`, `protocol_errors`, `write_errors`, `watchdog_resets`,
  `telemetry_watchdog_resets`, and `transport_watchdog_resets` in diagnostics.
  Those counters are session-scoped diagnostics and can reset after reloads or reconnects.

## Home Assistant Repairs

The integration creates low-noise Home Assistant Repairs only for persistent,
actionable conditions and dismisses them automatically after recovery:

- `persistent_no_route`: created only after repeated no-route failures where no
  connectable Bluetooth path is available.
- `repeated_watchdog_resets`: created only after repeated watchdog reconnects
  and dismissed once telemetry is fresh again.
- `invalid_migrated_options`: created when a legacy entry cannot be migrated due
  to invalid option relationships and dismissed after successful migration.
- `failed_unload`: created immediately after unsuccessful platform/coordinator
  unloading or Core post-unload cleanup, requires a Home Assistant restart, and
  is dismissed after successful new setup or entry deletion.
  See [Integration unload failed](#integration-unload-failed).

Normal transient reconnects do not create Repairs.

## Aggressive timing causes proxy instability

Very small status or keepalive intervals can saturate the local adapter or an
ESPHome Bluetooth Proxy when multiple devices compete for active BLE sessions.

- Keep `status_interval` at 20 seconds unless you have measured need.
- Keep settings keepalive disabled unless settings freshness is required.
- Increase intervals before increasing watchdog timeout.
- Prefer one stable connectable route over many marginal proxies.

## Enabling logs

```yaml
logger:
  logs:
    custom_components.allpowers_ble: debug
    bleak_retry_connector: debug
```

Reproduce one failure, download diagnostics, then disable debug logging.
Diagnostics already redact Bluetooth identifiers and device names recursively,
including nested strings and sanitized `last_error` details. Still review raw
debug logs for personal data before sharing.

## HACS installs the integration but it does not appear

- Confirm Home Assistant meets the minimum version in `hacs.json`.
- Restart Home Assistant after installation.
- Confirm `config/custom_components/allpowers_ble/manifest.json` exists.
- For a release asset, verify `manifest.json` is at the ZIP root rather than nested
  under another `custom_components/allpowers_ble` directory.
- Check Home Assistant startup logs for manifest or import errors.
