# Focused security and release review — v1.01

Date: 2026-10-04. Scope: new Export Edition fork only. This is a focused engineering review and automated regression check, not a penetration-test certification or a guarantee against malicious custom nodes.

## Addressed

- Private source-app settings, photos, databases, workflow catalogs and API keys were not copied into the fork.
- Public control panel is loopback-only, with strict Host/Origin/Fetch Metadata checks and a per-session header for writes. CSP prohibits foreign scripts/connections and framing; inline script handlers were removed from batch links.
- Service URLs allow localhost/literal loopback only; redirects and environment proxies are disabled for local AI transport. Private tokens are redacted in responses. Response and import sizes are bounded.
- Public settings allow only supported connection fields; arbitrary workflow file paths/internal catalog entries cannot be injected through settings.
- Reference media excludes HTML/SVG/scripts on the actual POST upload path, and saved MIME types are derived from the safe extension rather than untrusted client headers. Processed-reference updates must point to an existing tray entry. PNG workflow imports use JSON metadata, never executable source.
- Arbitrary external-file deletion is disabled. Library/static/media paths retain resolved-directory containment checks.
- CLI requests use argument arrays and stdin, isolated temporary directories, no interpolated shell commands, tool/config isolation flags, and explicit cloud consent. npm shims resolve to the Node entry point rather than running CMD.
- Managed llama.cpp stops only its tracked process. An external server without confirmed unloading blocks the GPU handoff. Busy ComfyUI blocks prompt-model loading.
- Manual ComfyUI memory release/exit now holds the queue before cancelling the active job, preventing a queued job from immediately reloading models.
- Source ZIP uses a file allowlist, excludes runtime data/backups/environment/evidence and one-off verification scripts, and scans obvious secret patterns. GitHub CI has read-only repository permissions.

## Verification

- Final run: 103 regression/security tests passed in 11.736 seconds; JavaScript syntax checks passed. Context-overflow errors retain a retryable, sanitized explanation without exposing private provider response bodies. Upload tests rejected SVG/HTML/JS and forced safe image MIME despite a misleading client header.
- Live HTTP tests denied unauthorized writes, foreign Origin/Host requests and external-file deletion; tested the queue-hold ordering for both ComfyUI manual controls.
- Local deterministic LM/Comfy test services exercised real app queue/start/resume HTTP/UI paths: pasted batch 2/2 outputs; two guide batches 4/4 outputs each, with FIFO automatic continuation and unload/free events.
- Test-service outputs are solid-color fixture PNGs, not AI-generated images. No cloud account calls or real GPU generations were used.
- Pause/Resume, in-page confirmation dismissal/acceptance, queued-job cancellation and ComfyUI memory release were click-through tested. Default and 800-pixel/light-theme layouts were inspected with screenshots.
- Source ZIP was extracted into a new folder; QuickStart created a fresh virtual environment, installed pinned Pillow and served v1.01 on port 5195. HTTP checks confirmed empty jobs/workflows, two starter guides, blank keys and incomplete setup. Verification server then shut down cleanly.

## Residual risks / release conditions

- Installed ComfyUI custom nodes and selected CLIs are trusted programs. This app cannot sandbox arbitrary custom-node Python or admin-managed CLI hooks/policies.
- Optional Civitai networking/posting and authenticated cloud inference were not live-tested. Local-provider protocols/CLI argument construction were tested, but actual generation on every backend still needs a user-owned model/account acceptance test.
- Single-user local app only: other processes running as the same OS user can read local settings/session pages. Config tokens are not encrypted at rest. Do not expose the app through tunnels, port forwarding or a public reverse proxy.
- Secret-pattern scans cannot prove arbitrary source is free of sensitive information. Inspect the release ZIP before uploading, enable GitHub secret scanning, choose a license and configure a private security-reporting channel.
- Pinning Pillow fixes the tested dependency version; future updates still need normal dependency/security maintenance.
