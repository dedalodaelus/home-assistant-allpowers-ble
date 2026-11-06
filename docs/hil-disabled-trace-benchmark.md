# Disabled HIL tracing benchmark

Measured 2026-11-06 against `devel` (`369b8012bfd70f215aad2d3b2a6dda3805e6d17c`) and `sandbox2` (`a4f2887782ad10b322772d81d4cf76c184c9fb9d`). Both used Python 3.14.10, the same `.venv`, Home Assistant dependencies, deterministic fake GATT client and status frame. The HIL observer remained unset for every workload.

Each latency sample performs 10,000 operations. There were two independent process runs, each reporting the median of seven samples; the table reports the mean of those two run medians and their range. Client construction and setup are outside the timed section. The notification case processes a valid status frame. The write case exercises `_write_frame_unlocked` with a fake transport. The transaction case processes matching status telemetry against an awaiting output transaction. No physical Bluetooth connection is opened.

Latency was measured with `perf_counter_ns` and without allocation tracing. Separately, `tracemalloc` records peak live traced memory during one 10,000-operation run and retained traced bytes after two event-loop turns and garbage collection. Peak live memory is a transient-allocation high-water mark, not a total-allocation counter. The harness retains event-loop cancellation bookkeeping for the fake asynchronous write path.

| Hot path | `devel` median (run range) | `sandbox2` median (run range) | Difference | Peak live bytes (`devel` / `sandbox2`) | Net retained bytes after GC (`devel` / `sandbox2`) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Notification decode | 19.89 µs (19.84–19.94) | 20.04 µs (19.75–20.32) | +0.7% | 3,205,874 / 3,205,874 | 145 / 145 |
| GATT write | 3.25 µs (3.25–3.26) | 3.32 µs (3.30–3.35) | +2.1% | 2,336,608 / 2,336,608 | 169,848 / 169,848 |
| Transaction confirmation | 28.08 µs (28.03–28.14) | 29.06 µs (28.97–29.14) | +3.5% | 3,205,245 / 3,205,253 | -1,324 / -1,324 |

The notification result overlaps across process runs. The GATT write difference is about 70 ns, and the transaction confirmation difference is about 0.98 µs per matching status notification. A call-site guard was tried for the disabled transaction observer and did not improve the repeated result, so it was not retained. The measured transaction difference does not change transaction decisions or safety guards. Peak traced memory is unchanged at the displayed precision, with no retained growth after collection. This synthetic test does not establish end-to-end radio latency. HIL evidence remains private; protocol-aware public redaction/export is tracked separately in #231.

## Reproduction

From the repository root, with `requirements_test.txt` installed in `.venv`:

```bash
mkdir -p /tmp/allpowers-bench-devel
git archive devel | tar -x -C /tmp/allpowers-bench-devel
PYTHONPATH=/tmp/allpowers-bench-devel .venv/bin/python \
  scripts/hil/benchmark_disabled_trace.py
PYTHONPATH=. .venv/bin/python scripts/hil/benchmark_disabled_trace.py
```

The first command uses the checked-out script with the `devel` source selected through `PYTHONPATH`; the second selects the current checkout. Keep the interpreter, dependencies, power mode and background load equivalent. Run each command several times and compare medians rather than relying on one sample.
