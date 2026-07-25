# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/).

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
