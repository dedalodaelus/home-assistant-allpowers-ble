# ALLPOWERS BLE protocol notes

This document describes the protocol behavior implemented by the integration. It
is based on observed R600 traffic and intentionally avoids claiming compatibility
with visually similar hardware.

## GATT interface

| Purpose | UUID |
|---|---|
| Service | `0000fff0-0000-1000-8000-00805f9b34fb` |
| Notifications | `0000fff1-0000-1000-8000-00805f9b34fb` |
| Writes | `0000fff2-0000-1000-8000-00805f9b34fb` |

The integration requires all three during the active setup probe.

## Notification envelope

Decoded notifications use this envelope:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 2 | Header `A5 65` |
| 2 | 3 | Vendor envelope bytes |
| 5 | 1 | Payload length `N` |
| 6 | 1 | Command |
| 7 | `N` | Payload |
| 7 + `N` | 1 | XOR checksum |

The XOR of every byte in a complete frame must equal zero. A frame therefore has a
total length of `8 + N` bytes.

The stream decoder does not assume that one GATT callback equals one frame. It can
recover:

- a frame split across several callbacks;
- several frames in one callback;
- leading noise;
- an invalid candidate followed by a valid frame;
- a trailing partial header.

Payload lengths above 128 bytes are rejected as implausible for this protocol.

## Commands

| Command | Direction | Implemented meaning |
|---:|---|---|
| `0x01` | Device → HA | Status notification |
| `0x02` | HA → Device | Settings write |
| `0x03` | Device → HA | Settings notification |
| `0x35` | Device → HA | Optional UTF-8 device name |
| Other | Device → HA | Retained as a validated unknown packet |

### Status request

The exact observed request is:

```text
A5 65 B1 00 01 06 01 00 00 00 00 00
```

It is sent immediately after subscribing and periodically while connected.

## Status payload (`0x01`)

The implemented parser requires at least eight payload bytes:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 1 | Status flags |
| 1 | 1 | Battery percentage, validated as 0–100 |
| 2 | 2 | Input power in watts, big-endian |
| 4 | 2 | Output power in watts, big-endian |
| 6 | 2 | Remaining time in minutes, big-endian |

Known status masks:

| Mask | Meaning |
|---:|---|
| `0x01` | DC output enabled |
| `0x02` | AC output enabled |
| `0x10` | Light enabled |

The raw flags byte is retained in the immutable model for diagnostics and future
protocol work.

## Combined output command

Output writes use one combined flags byte rather than independent commands:

| Mask | Meaning |
|---:|---|
| `0x01` | DC output enabled |
| `0x02` | AC output enabled |
| `0x20` | Light enabled |

The control frame is built as:

```text
A5 65 00 B1 01 01 00 <flags> <xor>
```

Note that the light mask differs between status (`0x10`) and output control
(`0x20`). The integration never builds this command from defaults. It requires a
fresh status snapshot and preserves the other output states.

## Settings notification (`0x03`)

The parser requires at least six payload bytes:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 1 | Settings flags |
| 1 | 1 | ECO timeout in hours |
| 2 | 2 | Undocumented/reserved data |
| 4 | 1 | Hardware version byte |
| 5 | 1 | Firmware version byte |

Known settings masks:

| Mask | Meaning |
|---:|---|
| `0x01` | ECO enabled |
| `0x06` | Work mode, two bits shifted right by one |
| `0x10` | Car charger enabled; experimental control |

Known work-mode values are 0 (mute), 1 (standard), and 2 (fast). Unknown values are
kept as `None` rather than coerced to a supported mode.

Version bytes are displayed as BCD-style `high.low` when both nibbles are decimal;
otherwise the raw hexadecimal byte is shown.

## Settings write (`0x02`)

A settings command is:

```text
A5 65 00 B1 01 02 02 <raw flags> <eco hours> <xor>
```

Only ECO timeouts 1, 2, 4, and 6 hours are writable. Unknown settings flag bits are
preserved from the latest safe notification. The two undocumented notification
bytes and version bytes are not written by the known command format.

## Checksum

For bytes `b[0]` through `b[n]`:

```text
b[0] XOR b[1] XOR ... XOR b[n] == 0
```

The encoder appends the XOR of all existing bytes. The decoder verifies the
complete frame before parsing any payload.

## Unknown fields and compatibility policy

Unknown packets and raw flag bits are data, not errors. They are retained where
safe and never overwritten casually. New writable behavior requires packet
captures or equivalent evidence from the exact hardware revision and regression
vectors in the protocol tests.

See [Adding models](adding-models.md).
