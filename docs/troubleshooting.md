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
- Review `last_error`, `write_errors`, `watchdog_resets`,
  `telemetry_watchdog_resets`, and `transport_watchdog_resets` in diagnostics.

## Enabling logs

```yaml
logger:
  logs:
    custom_components.allpowers_ble: debug
    bleak_retry_connector: debug
```

Reproduce one failure, download diagnostics, then disable debug logging. Redact
Bluetooth addresses and personal data before sharing logs.

## HACS installs the integration but it does not appear

- Confirm Home Assistant meets the minimum version in `hacs.json`.
- Restart Home Assistant after installation.
- Confirm `config/custom_components/allpowers_ble/manifest.json` exists.
- For a release asset, verify `manifest.json` is at the ZIP root rather than nested
  under another `custom_components/allpowers_ble` directory.
- Check Home Assistant startup logs for manifest or import errors.
