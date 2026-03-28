# Security

This project is designed for local desktop usage with browser extension integration.

## Security Model

- Trust boundary: browser extension and desktop app communicate over a local bridge.
- Default deployment is localhost-only.
- Pairing token is required for privileged extension actions.

## Credential and Token Handling

- Sensitive values are encrypted before persistence.
- OS keyring integration is used where available.
- Tokens are rotated/revoked during re-pairing workflows.

## Filesystem Safety

- Save path boundary enforcement prevents path traversal.
- Unsafe or malformed filenames are normalized/sanitized.
- Temporary chunks are assembled safely before finalization.

## API and Bridge Protections

- Rate limiting protects local bridge endpoints.
- Input schema validation rejects malformed payloads.
- Health monitoring detects stalled bridge state.

## Dependency and Supply Chain Practices

- Pin or constrain dependency versions.
- Run CI checks for lint, type analysis, and tests on each PR.
- Prefer trusted package sources and review changelogs before upgrades.

## Disclosure Policy

If you discover a security issue, please do not open a public issue.
Report responsibly via private maintainer contact and include reproducible details.