"""Benchmark normal BLE hot paths with the optional HIL observer disabled.

Run with the repository's test environment and a checkout selected by PYTHONPATH:

    PYTHONPATH=/path/to/revision .venv/bin/python \
        scripts/hil/benchmark_disabled_trace.py
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
from statistics import median
import sys
import tracemalloc
from time import perf_counter_ns
from pathlib import Path

from custom_components.allpowers_ble import client as client_module
from custom_components.allpowers_ble.client import (
    PendingOutputTransaction,
    TransactionPhase,
)
from custom_components.allpowers_ble.protocol.codec import append_xor_checksum
from tests.ha_stubs import FakeCharacteristic
from tests.test_client_runtime import connect_fake, make_client

REPEATS = 7
ITERATIONS = 10_000
PAYLOAD = bytes((0x13, 73, 0x01, 0x2C, 0x00, 0x96, 0x00, 0x78))
FRAME = append_xor_checksum(
    bytes((0xA5, 0x65, 0x00, 0x00, 0x00, len(PAYLOAD), 0x01)) + PAYLOAD
)


def _new_client():
    client = make_client()
    client._loop = asyncio.get_running_loop()
    transport = connect_fake(client)
    return client, transport


def _notification_decode(client) -> None:
    characteristic = FakeCharacteristic()
    data = bytearray(FRAME)
    for _ in range(ITERATIONS):
        client._notification_handler(characteristic, data)


async def _gatt_write(client) -> None:
    for _ in range(ITERATIONS):
        await client._write_frame_unlocked(FRAME)


def _transaction_confirmation(client) -> None:
    characteristic = FakeCharacteristic()
    data = bytearray(FRAME)
    client._notification_handler(characteristic, data)
    status = client._status
    assert status is not None
    loop = asyncio.get_running_loop()
    for _ in range(ITERATIONS):
        now = loop.time()
        client._pending_output_transaction = PendingOutputTransaction(
            session_generation=1,
            source_version=client._status_version,
            target_dc=status.dc_enabled,
            target_ac=status.ac_enabled,
            target_light=status.light_enabled,
            sent_monotonic=now,
            confirm_deadline_monotonic=now + 30,
            phase=TransactionPhase.AWAITING_CONFIRMATION,
        )
        client._notification_handler(characteristic, data)


async def _run_case(name: str, fn) -> dict[str, object]:
    elapsed: list[float] = []
    for _ in range(REPEATS):
        gc.collect()
        client, _ = _new_client()
        if name == "transaction_confirmation":
            client._notification_handler(FakeCharacteristic(), bytearray(FRAME))
        started = perf_counter_ns()
        result = fn(client)
        if asyncio.iscoroutine(result):
            await result
        elapsed.append((perf_counter_ns() - started) / ITERATIONS)

    gc.collect()
    client, _ = _new_client()
    if name == "transaction_confirmation":
        client._notification_handler(FakeCharacteristic(), bytearray(FRAME))
    result = fn(client)
    if asyncio.iscoroutine(result):
        await result
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()

    tracemalloc.start()
    memory_client, _ = _new_client()
    if name == "transaction_confirmation":
        memory_client._notification_handler(FakeCharacteristic(), bytearray(FRAME))
    baseline, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    result = fn(memory_client)
    if asyncio.iscoroutine(result):
        await result
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "path": name,
        "iterations_per_repeat": ITERATIONS,
        "latency_repeats": REPEATS,
        "median_ns_per_operation": round(median(elapsed), 1),
        "min_ns_per_operation": round(min(elapsed), 1),
        "max_ns_per_operation": round(max(elapsed), 1),
        "retained_bytes_after_gc": current - baseline,
        "peak_traced_bytes_above_baseline": peak - baseline,
    }


async def main() -> None:
    source_root = Path(client_module.__file__).resolve().parents[2]
    script_root = Path(__file__).resolve().parents[2]
    runtime_tests = Path(make_client.__code__.co_filename).resolve()
    if source_root != script_root or not runtime_tests.is_relative_to(source_root):
        raise RuntimeError(
            "benchmark source selection is inconsistent: "
            f"script={script_root}, client={source_root}, tests={runtime_tests}"
        )
    cases = (
        ("notification_decode", _notification_decode),
        ("gatt_write", _gatt_write),
        ("transaction_confirmation", _transaction_confirmation),
    )
    results = [await _run_case(name, fn) for name, fn in cases]
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "source_revision": os.environ.get(
                    "BENCHMARK_SOURCE_REVISION", "unlabelled"
                ),
                "source_root": str(source_root),
                "client_module": str(Path(client_module.__file__).resolve()),
                "runtime_test_module": str(runtime_tests),
                "results": results,
            },
            indent=2,
        )
    )


asyncio.run(main())
