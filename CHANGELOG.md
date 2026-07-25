# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/).

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
