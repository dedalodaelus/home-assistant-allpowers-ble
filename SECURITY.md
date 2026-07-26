# Security policy

## Supported versions

Security fixes are applied to the latest released version. Older releases may be
supported when a safe backport is small and does not change protocol behavior.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability that could expose Home
Assistant data, execute untrusted code, or cause unsafe device control.

Use GitHub's private **Report a vulnerability** function in the repository Security
section. Include:

- affected version and Home Assistant version;
- installation method;
- a minimal reproduction;
- expected and observed behavior;
- relevant logs with Bluetooth addresses, tokens, and personal data removed;
- an assessment of impact and whether physical access is required.

A report will be acknowledged after it can be reviewed. Valid issues are handled
privately until a fix and release are available.

## Scope and design boundaries

The integration communicates only over the local Home Assistant Bluetooth stack.
It stores no cloud credentials and does not expose an HTTP endpoint. Device-control
commands are nevertheless physically consequential: test changes with loads
removed when reverse-engineering a new model.

The diagnostics implementation redacts the configured Bluetooth address and omits
the stored advertised name. Contributors must not add packet captures, addresses,
serial numbers, location data, or Home Assistant diagnostics to the repository.

## BLE trust model and authentication boundaries

### What is authenticated

- **Device identification**: The integration uses active protocol probe to verify that a device exposes the expected GATT service, characteristics, and valid protocol frames. This confirms the presence of the ALLPOWERS protocol implementation, not the physical identity.
- **Protocol compliance**: Incoming notifications are validated for correct headers, lengths, checksums, and payload structure. Unknown or malformed frames are rejected.
- **Capability-based write authorization**: Only verified hardware profiles are permitted to accept output and settings writes. Experimental profiles operate in read-only mode regardless of name or discovery advertisement.

### What is NOT authenticated

- **Cryptographic device authentication**: Bluetooth LE does not employ cryptographic signatures for the protocol commands implemented by this integration. BLE pairing/bonding is not used.
- **Command authenticity**: Any device on the local network that exposes the FFF0 service and a matching protocol can respond to commands. There is no mechanism to cryptographically prove that responses originate from the intended device.
- **Physical identity**: Bluetooth advertisements can be spoofed by other devices on the same local network. The integration relies on proximity and service discovery, not cryptographic proof.

### Trust assumptions

The security model depends on:

1. **Physical proximity**: Only devices within Bluetooth range of Home Assistant or a proxy can be discovered or receive commands. This creates an implicit local-network boundary.
2. **Trusted network**: Home Assistant's Bluetooth stack is assumed to route only to trusted adapters and proxies within the local network.
3. **Profile verification**: Write capabilities are enabled only after an active probe confirms matching the exact hardware revision and protocol implementation.
4. **Home Assistant access control**: Only users with Home Assistant configuration.yaml access or entity control permissions can issue commands.

### Mitigations and limitations

- **Read-only for unverified devices**: Any device that cannot be matched to a verified profile operates in read-only mode, regardless of protocol validity or appearance.
- **Semantic write validation**: Verified profiles use state-based validation to reject writes when unknown flag bits or semantic inconsistencies are detected in device status.
- **Address redaction**: Diagnostics do not expose Bluetooth addresses or advertised names, limiting information leakage in shared logs.
- **No credentials**: The protocol and integration store no shared secrets, tokens, or keys that could be compromised and reused remotely.

### Attack scenarios within scope

- **Local spoofing**: An attacker with physical proximity could advertise a BLE device with matching name and service UUID to perform reconnaissance or attempt protocol commands. Mitigation: Active probe validation and strict profile matching limit exposure. Writes are rejected for unverified profiles.
- **Man-in-the-middle on Bluetooth link**: An attacker between Home Assistant and the device could observe telemetry or attempt to intercept commands. Mitigation: Use a secure Bluetooth adapter or ESPHome proxy in a trusted location.
- **Replay within connection lifetime**: Captured packets could be replayed if intercepted during an active connection. Mitigation: Commands include freshness requirements (e.g., status snapshot read-modify-write pattern). Long-lived static connections are not assumed.

### Out of scope

- **Remote attacks**: The integration does not expose cloud endpoints or accept commands from outside the local network.
- **Home Assistant compromise**: If Home Assistant itself is compromised, any protection boundary fails. Assume Home Assistant and its configuration are managed by trusted administrators.
- **Bluetooth driver vulnerabilities**: Bugs in the underlying BLE implementation are outside the integration's control.
