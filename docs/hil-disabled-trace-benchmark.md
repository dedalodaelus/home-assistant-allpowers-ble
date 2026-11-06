# Disabled HIL tracing benchmark

Re-measured 2026-11-06 against the pinned `devel` base `369b8012bfd70f215aad2d3b2a6dda3805e6d17c` and PR head `sandbox2` `a3b07255baca6fddb548c40161e5d4bde33648b9`. Both used Python 3.14.10, the same `.venv` dependencies, deterministic fake GATT client and status frame. The HIL observer remained unset for every workload.

Each process reports the median of seven latency samples, each sample performing 10,000 operations. Two independent processes were run per revision in alternating base/head order to reduce order effects. The table reports the mean of those two process medians and the full range across all 14 samples. Client construction and setup are outside the timed section. The notification case processes a valid status frame. The write case exercises `_write_frame_unlocked` with a fake transport. The transaction case processes matching status telemetry against an awaiting output transaction. No physical Bluetooth connection is opened.

The benchmark prints the source revision label, imported `client.py` path, runtime test helper path, and source root. It exits with an error unless both imported paths and the script itself resolve inside the same source root. The recorded output confirmed `/tmp/allpowers-bench-devel/...` for the base and `/tmp/allpowers-bench-sandbox2/...` for the PR head; neither run imported the other revision.

Latency was measured with `perf_counter_ns` and without allocation tracing. Separately, `tracemalloc` records peak live traced memory during one 10,000-operation run and retained traced bytes after two event-loop turns and garbage collection. Peak live memory is a transient-allocation high-water mark, not a total-allocation counter. The harness retains event-loop cancellation bookkeeping for the fake asynchronous write path.

| Hot path | `devel` median (full sample range) | PR head median (full sample range) | Difference | Peak live bytes (`devel` / PR) | Net retained bytes after GC (`devel` / PR) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Notification decode | 20.15 µs (20.02–21.75) | 19.87 µs (19.41–24.07) | -1.4% | 3,205,874 / 3,205,874 | 145 / 145 |
| GATT write | 3.22 µs (3.18–3.61) | 3.30 µs (3.21–3.66) | +2.4% | 2,336,608 / 2,336,608 | 169,848 / 169,848 |
| Transaction confirmation | 28.42 µs (28.16–31.91) | 28.92 µs (28.57–35.85) | +1.8% | 3,205,245 / 3,205,253 | -1,324 / -1,324 |

All latency ranges overlap. The PR-head medians are about 0.28 µs faster for decode, 0.08 µs slower for GATT write, and 0.50 µs slower for transaction confirmation. All ranges overlap, and the broad upper tails show scheduler noise in this shared host; these results do not establish a material regression. Peak traced memory is unchanged at the displayed precision, with no retained growth after collection. These synthetic software-path measurements do not establish end-to-end radio latency or total allocation counts. HIL evidence remains private; protocol-aware public redaction/export is tracked separately in #231.

## Reproduction

Run from the repository root with `requirements_test.txt` installed in `.venv`. The exact source snapshots are extracted to separate directories; the same updated benchmark harness is copied into each. Its source-path assertions verify that the imported integration code and test helpers come from the selected snapshot.

```bash
BASE=369b8012bfd70f215aad2d3b2a6dda3805e6d17c
HEAD=a3b07255baca6fddb548c40161e5d4bde33648b9
mkdir -p /tmp/allpowers-bench-devel /tmp/allpowers-bench-sandbox2
git archive "$BASE" | tar -x -C /tmp/allpowers-bench-devel
git archive "$HEAD" | tar -x -C /tmp/allpowers-bench-sandbox2
cp scripts/hil/benchmark_disabled_trace.py /tmp/allpowers-bench-devel/scripts/hil/
cp scripts/hil/benchmark_disabled_trace.py /tmp/allpowers-bench-sandbox2/scripts/hil/

BENCHMARK_SOURCE_REVISION="$BASE" PYTHONPATH=/tmp/allpowers-bench-devel \
  .venv/bin/python /tmp/allpowers-bench-devel/scripts/hil/benchmark_disabled_trace.py
BENCHMARK_SOURCE_REVISION="$HEAD" PYTHONPATH=/tmp/allpowers-bench-sandbox2 \
  .venv/bin/python /tmp/allpowers-bench-sandbox2/scripts/hil/benchmark_disabled_trace.py
BENCHMARK_SOURCE_REVISION="$BASE" PYTHONPATH=/tmp/allpowers-bench-devel \
  .venv/bin/python /tmp/allpowers-bench-devel/scripts/hil/benchmark_disabled_trace.py
BENCHMARK_SOURCE_REVISION="$HEAD" PYTHONPATH=/tmp/allpowers-bench-sandbox2 \
  .venv/bin/python /tmp/allpowers-bench-sandbox2/scripts/hil/benchmark_disabled_trace.py
```

Keep the interpreter, dependencies, power mode and background load equivalent. Compare multiple independent process runs and retain both the medians and sample ranges.
