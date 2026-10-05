# Security — WildCat Export Edition v1.01

## Trust model

This is a single-user desktop application, not a publicly hosted web service. It binds only to `127.0.0.1`. Do not expose it through port forwarding, tunnels, a shared network interface, or a reverse proxy. Processes running as your own OS user are already inside the trust boundary and can read your local settings and session page.

The HTTP boundary checks Host, Origin and Fetch Metadata to resist malicious websites and DNS rebinding. State-changing requests additionally require a random per-server-session authorization header supplied only by the local HTML page. JSON actions require JSON content types. The app has no permissive cross-origin API. CSP denies foreign scripts/connections and framing. Authorization tokens are not browser-localStorage credentials and are not forwarded to model servers.

## Models and subprocesses

Prompt-server and ComfyUI addresses must resolve to loopback, without credentials or paths in the URL. Local HTTP transports refuse redirects and bypass environment proxies. Claude Code/Codex are launched with argument arrays, never an interpolated shell command. Windows npm shims resolve to their Node entry points instead of invoking CMD. Requests run in temporary isolated working directories that are removed afterward.

Claude Code requires safe-mode/config-isolation flags, no built-in tools, no MCP tools, no skills and no session persistence. Admin-managed CLI policies still apply. Codex requires ephemeral read-only mode, no user configuration, disabled shell/unified-exec/apply-patch-freeform and web search. These flags reduce tool exposure, but a CLI is an independently maintained trusted binary, not a security boundary enforced by this Python application. Only install CLIs from official sources, and keep them current. Do not use enterprise-managed installations with unknown policy hooks for untrusted guides.

Managed llama.cpp terminates only its own tracked Popen process. It refuses to adopt an existing external server on that port. External single-model llama.cpp cannot be assumed unloadable: lack of confirmed unload blocks the next model family. ComfyUI must have an idle queue before it is freed for prompt generation. Verify you do not run other GPU tools concurrently outside WildCat.

## Files and workflows

Static/media paths and archive deletions are confined to their intended directories. Public settings cannot inject arbitrary workflow file paths or internal catalog entries. Reference updates must refer to an actual upload-tray entry. API workflow imports are size-limited; PNG imports use original metadata and are parsed as JSON, never Python or shell code. External-file deletion is disabled. Private files are never returned by the static file server.

ComfyUI workflow JSON can trigger installed **custom nodes** when submitted to ComfyUI. Those nodes can execute Python and access the filesystem. Import only trusted workflows/nodes; this application cannot sandbox another program's node implementations. A node-type check is compatibility feedback, not malware detection. Large media uploads use bounded sizes but can still consume substantial disk space; keep free disk space and avoid exposing this local server to untrusted local users.

## Credentials and cloud data

API tokens remain in your private local config and are redacted from responses, including nested objects and error strings. Local storage is not encrypted. Claude/Codex use their installed CLI authentication. Explicit consent is required for cloud prompting. Civitai posting is optional, requires your own key and an explicit user action; it is never exercised during build verification.

When a token may have been committed or shared, revoke/rotate it at the provider before removing it from repository history. `.gitignore` alone does not remove tracked secrets. Keep API token scopes minimal.

## Distribution

The release packager includes only source/docs/tests/starter guides through an allowlist, refuses symlinks and obvious credential patterns, and excludes all runtime data. Secret-pattern scanning is defense in depth, not proof that arbitrary source code contains no private information. Inspect the ZIP contents before publishing. Configure GitHub secret scanning/dependency alerts and use the supplied CI workflow. Choose a license separately.

## Reporting issues

Do not paste real tokens, private prompts/photos, or full config files into a public issue. Include the version, backend/CLI version, sanitized error text, and reproduction steps. Before publishing a repository, configure a private vulnerability-reporting channel in GitHub's Security settings. There is no fabricated maintainer contact address in this edition.
