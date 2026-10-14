# Optional DeepSeek Harness integration (in progress)

The initial protocol boundary is disabled by default and performs **no** tool execution. `HarnessBridge.receive` accepts only bounded version-1 event envelopes. It does not authorize commands, browser access, plugin installation, network use or approvals. Alfred's existing protected execution policy must remain the sole authority.

## Status and limitations

This is a preparatory boundary, **not** a working Cordis/DeepSeek Harness adapter. No official SDK version, commit, checksum or native ARM64 support has yet been verified. Do not enable it in production. No subprocess is spawned and no backend is started by this code. The default Alfred route is unchanged.

Before enabling: pin and verify official SDK distribution; implement and validate the real SDK protocol against a local stub model; map canonical task/session IDs and observations; add timeout/cancellation/teardown; reject remote endpoints and tool spoofing; ensure all tool proposals go through Alfred's existing approval checks; run Python 3.10/3.12 and Jetson smoke tests. Rollback is to leave the flag disabled and use the existing Alfred path.
