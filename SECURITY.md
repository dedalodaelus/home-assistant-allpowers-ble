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
