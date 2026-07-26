# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/).

## [0.2.0](https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare/0.1.6...0.2.0) (2026-07-26)


### Features

* **config-entry:** add baseline migration to schema 1.1 ([#81](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/81)) ([5f6e28a](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/5f6e28a698a3a1bccfa920c7ee790762d5483184))
* **config-flow:** section options flow and field-level errors ([#85](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/85)) ([cf67f40](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/cf67f40e4247432628fe076be013e2622ceab3bc))


### Bug Fixes

* **coordinator:** refresh device registry metadata from valid settings ([#86](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/86)) ([1f645cd](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/1f645cd639fd5f84b155fb41d8e5d72dc7ebc7d1)), closes [#44](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/44)
* **docs:** align safety guarantees and user workflows ([#84](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/84)) ([9cde9b2](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/9cde9b2f4c2d3da345c205da6926a61eacbc0caa))
* **entities:** correct input/output binary sensor semantics ([#82](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/82)) ([45ff32b](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/45ff32be04f7caf9b56711f7ab333775c9829e43))
* **entities:** normalize BLE command failures into translated HA errors ([#79](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/79)) ([35378ba](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/35378ba5306a39d934d0c5e1a16a41c598293422)), closes [#38](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/38)
* **security:** redact bluetooth identifiers recursively in diagnostics ([#80](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/80)) ([3aae1e2](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/3aae1e233d1a239d4616ca3b40af632f118a1e9c))

## [0.1.6](https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare/0.1.5...0.1.6) (2026-07-26)


### Bug Fixes

* **metrics:** split parser discarded bytes from frame error counters ([#73](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/73)) ([9c69cf9](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/9c69cf97dfd7c58c4ec2f55115484e64cc800319)), closes [#36](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/36)
* **model-support:** enforce read-only capability profiles for experimental devices ([#74](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/74)) ([fe76ebf](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/fe76ebf1abdbf96888f193903602297d1654f309)), closes [#31](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/31)
* **protocol:** enforce semantic profile validation and safe output writes ([#75](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/75)) ([54fcb3e](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/54fcb3e583326184c4034b6cc5e1cde360fd4d53)), closes [#32](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/32) [#33](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/33)
* **protocol:** sanitize device names and harden stream decoder limits ([#72](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/72)) ([49a937f](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/49a937f239776876f1d026913b8569633d0fc21a)), closes [#34](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/34) [#35](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/35)

## [0.1.5](https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare/0.1.4...0.1.5) (2026-07-25)


### Bug Fixes

* add reconnect jitter and RSSI debounce ([#68](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/68)) ([559e9ed](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/559e9edb36108438e07e4b78538c2e06695f1bb9)), closes [#28](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/28) [#29](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/29)
* enforce end-to-end probe timeout and map stage failures ([ede2084](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/ede2084fcd9ed7cf7106446034fef7a72176432d))
* split telemetry and transport watchdog resets ([6d81fc2](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/6d81fc2fc9025b299af4d69b13bdc975038b80e0))


### Performance Improvements

* **client:** schedule maintenance loop by deadline wakeups ([#69](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/69)) ([f63d72f](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/f63d72f7c069b57e27a24d07e19595584940eb6f)), closes [#30](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/30)

## [0.1.4](https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare/0.1.3...0.1.4) (2026-07-25)


### Bug Fixes

* keep settings keepalive activity scoped to settings writes ([347aa84](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/347aa84d0b63074f64bccfe1d1008c7287bc4780))
* keep settings keepalive activity scoped to settings writes ([e764ce3](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/e764ce3a20fbe6fc275524b170094f40ffd2a371))
* replace write shadows with versioned command transactions ([2b4dad3](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/2b4dad3c7d5e88a88ccf22f4ede67ac27695527f))
* replace write shadows with versioned command transactions ([0d9610f](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/0d9610f92bbc14a4595c570200e895384432427b))
* scope BLE callbacks and readiness to active session generation ([f46ecbb](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/f46ecbbe2422830e1169f46a0af98ae331066b96))
* scope BLE callbacks to active session generation ([4d12044](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/4d120443994d85f2bd221a7f92ec1cf586fdce77))
* serialize BLE lifecycle operations with command writes ([91e2a99](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/91e2a99e71697b818f290927ce5578091b6856c7))
* serialize BLE lifecycle operations with writes ([55d4d86](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/55d4d867361e2b4d45c2505cc18b88dde940af13))

## [0.1.3](https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare/0.1.2...0.1.3) (2026-07-25)


### Bug Fixes

* **release:** trigger patch release for GA test ([#18](https://github.com/dedalodaelus/home-assistant-allpowers-ble/issues/18)) ([1d5d21c](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/1d5d21c55f652b4df09d7bd312f51abd5c3e2def))

## [0.1.2](https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare/0.1.1...0.1.2) (2026-07-25)


### Bug Fixes

* Simplify error message for release tag validation ([b555d4f](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/b555d4ff9092781b78bbeac97ff16fea04cd6bcd))

## [0.1.1](https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare/0.1.0...0.1.1) (2026-07-25)


### Bug Fixes

* correct manifest key alphabetical order ([724e5d1](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/724e5d1cb4723c2768e15b00b746ec38cea2d3c0))
* remove unused already_configured from Spanish translation ([78c4bb4](https://github.com/dedalodaelus/home-assistant-allpowers-ble/commit/78c4bb42a107d41d6aa9cce81a31b4f1f43c1c27))

## [Unreleased]

## [0.1.0] - 2026-07-23

### Added

- HACS-compatible Home Assistant integration for ALLPOWERS BLE power stations.
- Automatic Bluetooth discovery, active GATT probing, and config flow.
- Home Assistant Bluetooth Proxy and local-adapter transport.
- R600 telemetry and safe output/settings controls.
- Sensors, binary sensors, switches, selects, buttons, and number entities.
- Reconnection, watchdog, state freshness, optional settings keepalive, and
  diagnostics.
- Independent protocol codec with incremental stream recovery.
- English and Spanish translations.
- Unit, transport, flow, entity, and diagnostics tests with branch coverage.
- GitHub Actions for CI, Hassfest, HACS, CodeQL, dependency review, conventional
  commits, and automated semantic releases.

[Unreleased]: https://github.com/dedalodaelus/home-assistant-allpowers-ble/compare/0.1.0...HEAD
[0.1.0]: https://github.com/dedalodaelus/home-assistant-allpowers-ble/releases/tag/0.1.0
