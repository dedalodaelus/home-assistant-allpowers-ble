"""Tests for incremental notification recovery."""

from __future__ import annotations

from custom_components.allpowers_ble.protocol import (
    NotificationStreamDecoder,
    SettingsData,
    StatusData,
)


def test_fragmented_frame(status_frame: bytes) -> None:
    decoder = NotificationStreamDecoder()

    assert decoder.feed(status_frame[:3]) == []
    assert decoder.feed(status_frame[3:9]) == []
    packets = decoder.feed(status_frame[9:])

    assert len(packets) == 1
    assert isinstance(packets[0], StatusData)
    assert decoder.buffered_bytes == 0


def test_concatenated_frames(status_frame: bytes, settings_frame: bytes) -> None:
    decoder = NotificationStreamDecoder()

    packets = decoder.feed(status_frame + settings_frame)

    assert [type(packet) for packet in packets] == [StatusData, SettingsData]


def test_leading_noise_is_ignored(status_frame: bytes) -> None:
    decoder = NotificationStreamDecoder()

    packets = decoder.feed(b"\x00\xFFnoise" + status_frame)

    assert len(packets) == 1
    assert decoder.buffered_bytes == 0


def test_partial_header_survives_between_feeds(status_frame: bytes) -> None:
    decoder = NotificationStreamDecoder()

    assert decoder.feed(b"garbage\xA5") == []
    packets = decoder.feed(status_frame[1:])

    assert len(packets) == 1


def test_invalid_checksum_is_discarded_and_next_frame_recovers(
    status_frame: bytes,
    settings_frame: bytes,
) -> None:
    decoder = NotificationStreamDecoder()
    invalid = bytearray(status_frame)
    invalid[-1] ^= 1

    packets = decoder.feed(invalid + settings_frame)

    assert len(packets) == 1
    assert isinstance(packets[0], SettingsData)
    assert decoder.discarded_frames == 1


def test_invalid_payload_is_discarded(
    notification_builder,
    status_frame: bytes,
) -> None:
    decoder = NotificationStreamDecoder()
    invalid_battery = notification_builder(
        0x01,
        bytes((0, 255, 0, 0, 0, 0, 0, 0)),
    )

    packets = decoder.feed(invalid_battery + status_frame)

    assert len(packets) == 1
    assert decoder.discarded_frames == 1


def test_oversized_payload_length_resynchronizes(status_frame: bytes) -> None:
    decoder = NotificationStreamDecoder()
    invalid_prefix = bytes((0xA5, 0x65, 0, 0, 0, 200, 1))

    packets = decoder.feed(invalid_prefix + status_frame)

    assert len(packets) == 1
    assert decoder.discarded_frames == 1


def test_no_header_clears_buffer() -> None:
    decoder = NotificationStreamDecoder()

    assert decoder.feed(b"not a frame") == []
    assert decoder.buffered_bytes == 0


def test_reset(status_frame: bytes) -> None:
    decoder = NotificationStreamDecoder()
    decoder.feed(status_frame[:4])

    decoder.reset()

    assert decoder.buffered_bytes == 0
    assert decoder.discarded_frames == 0
