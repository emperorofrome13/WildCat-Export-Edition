# WildCat Export Edition v1.02

Standalone fork. Never edit WildCat-Harness. Preserve batch queues, guides, reference prompts, galleries, image editing/upscaling, ratings, recycling and metadata tools.

Build: portable settings; five provider adapters; guided setup; JSON/PNG workflow drops; sequential GPU unloading; secured loopback HTTP; source-only packaging; tested launchers; regression/security tests; UI evidence; HANDOFF.

New files: app/providers.py, app/export_server.py, app/static/export.js, app/static/export.css, scripts/launcher.py, scripts/package_release.py, tests/test_export_edition.py, README.md, SECURITY.md, AUDIT_REPORT.md, HANDOFF.md.

No Pinokio, no model downloads, no copied private data, no cloud calls during smoke tests, no publication. CLI backends use existing logins and may consume paid usage. Loopback only. Fail closed on unconfirmed VRAM unloading.

v1.01 completed: consolidate duplicate all-job stops; show only applicable Pause/Resume; expandable prompt recovery and ComfyUI controls; hold queue during manual GPU release; in-page confirmations; refresh image-library details when reopening the tab; clear queue-for-later feedback; sanitized retryable context errors; safe reference-upload validation/MIME enforcement. 103 tests, UI click-through and clean-extraction launcher verification passed. Remaining acceptance limits are documented in HANDOFF and AUDIT_REPORT.

v1.02 scope: four documentation-informed model MD enhancers (Z-Image Turbo, Krea 2, Qwen-Image 2.1, Ideogram 4.5); preserve both generic starters; allow one guide through HTTP validation and prompt orchestration; explain provenance and workflow limitations beside the picker; retain 4 prompts/guide and 300-word defaults; verify guide loading, single-guide batches, UI and source package. No automatic checkpoint switching, new image-service API, or real-model quality benchmark.
