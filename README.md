# WildCat Export Edition v1.02

A local batch-image workspace for ComfyUI, with optional AI prompt generation. This is a separate edition with its own configuration, guides, queues, images and ratings. It does not need PBI, Pinokio or another WildCat installation.

## Start on Windows

1. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/windows/). Enable **Add Python to PATH**.
2. Extract this folder somewhere writable. Double-click **QuickStart.bat**.
3. First start creates `.venv` and installs the pinned Pillow image library from PyPI. Subsequent starts reuse it. Internet is needed for that first install, not for local generation afterward.
4. Follow Setup: choose a prompt backend (or paste prompts), connect ComfyUI, and drop in API JSON workflows or original ComfyUI PNGs.
5. Make a small test batch before launching a large queue. Four prompts per MD and a 300-word minimum are the starting defaults. Counts are not limited to four guides or twenty total prompts.

Default control panel: `http://127.0.0.1:5194/`. If that port is taken, run `QuickStart.bat --port 5195`. Use `Stop.bat --port 5195` for an alternate port. Keep the launcher window open. Closing the browser page does not cancel jobs.

ComfyUI and the chosen prompt backend must already be installed. No AI models are downloaded or bundled. Start ComfyUI normally; automatic app launching is off by default. The optional advanced auto-launch feature supports LM Studio and an existing NVIDIA portable ComfyUI folder only. Other ComfyUI installations work through their running local API.

## Prompt backends

| Backend | Setup | GPU handoff |
|---|---|---|
| LM Studio | Enable local server, normally port 1234; discover installed models | Native load/unload API; unloading checked |
| Ollama | Start Ollama, normally port 11434; choose an installed model | Native `/api/generate` with `keep_alive: 0`, then `/api/ps` confirmation |
| llama.cpp | Recommended: choose llama-server executable and GGUF in managed mode; add mmproj for vision | Stops only the subprocess WildCat started. External router servers must implement and confirm `/models/unload` |
| Claude Code | Install recent CLI, sign in in a terminal, accept cloud-prompt consent | No local inference model; isolated CLI requests, tools/MCP disabled |
| Codex | Install recent CLI, sign in in a terminal, accept cloud-prompt consent | No local inference model; ephemeral read-only CLI requests, shell tools and user config disabled |

Claude/Codex use existing CLI authentication, not a copied login or an API credential entered into WildCat. Their provider terms, plan limits and charges still apply. A connection check only discovers the executable; it does **not** prove that you are signed in or have access to a chosen model. Actual prompt requests check that. Prompt text, guide contents and reference photos are sent to the cloud backend only after you give consent and start the job.

Local vision models are required for reference photos and visual comparisons. Ollama vision support is checked against the model's capabilities before image requests. For llama.cpp, use a vision model and its matching mmproj. Video references use a still selected/extracted in the browser, not a video sent to the model. Only this computer's services are supported; LAN/public API addresses are intentionally rejected in v1.01.

## Simple job controls

- **Pause / Resume**: interrupt the current image and retry it when resumed. Only the relevant button is shown.
- **Cancel job**: cancel this job, keeping saved images. Other queued jobs continue.
- **Cancel all jobs** (beside the queue): cancel current and waiting jobs, keeping saved images. This replaces the duplicate emergency-stop button.
- **Fix a prompt**: expandable section for Retry image, Rewrite prompt, and Skip prompt.
- **ComfyUI controls**: expandable section for Release GPU memory (keep ComfyUI open) or Close ComfyUI. Both cancel the current job and hold the waiting queue so another job cannot immediately reload the GPU. Resume/start a job explicitly when ready.

Four prompt requests can run in parallel. Each response remains attached to its originating guide even if it returns out of order. Image generation uses a single GPU lane. Failed unload confirmation stops the handoff; it is never treated as permission to load another model family.

## Workflows and guides

Drop multiple API JSON files or original ComfyUI PNGs onto the Setup importer or the generation/edit/upscale upload areas. A PNG must contain executable `prompt` metadata, not only the editor canvas. Screenshots and re-saved PNGs often lose that metadata. Regular editor JSON is rejected with export instructions.

Inspect the detected positive/negative prompt fields in Connections & workflow. Imported generation and edit/upscale workflows are separate. Use **Check installed node types** to find missing custom nodes. Checkpoint names, LoRA names, custom-node behavior and graph correctness still need to match your ComfyUI setup. WildCat does not install nodes, run scripts from an imported JSON, or fetch a workflow from a URL.

Six starter MD guides are supplied for a fresh installation. Add, replace or delete guides in the Run screen. Reference prompting is optional and collapsible. Workflow provenance, prompts, image dimensions and ratings remain available in the image library and gallery details. Editing/upscaling results remain linked to their original images.

### Included prompt enhancers

The original `01-cinematic.md` and `02-editorial.md` were written for WildCat as generic composition and editorial-clarity starters. They were not imported from PBI, supplied by a model vendor, or tested for one checkpoint.

v1.02 adds four WildCat-authored, documentation-informed guides:

| Guide | Intended use | Source grounding |
|---|---|---|
| Z-Image-Turbo-enhancer.md | Coherent scene fidelity, lighting, surfaces, literal lettering; Turbo rather than base/Edit | [Official model card](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) |
| Krea-2-enhancer.md | Faithful natural-language art direction; supports short exploration or detailed briefs | [Official prompting notes](https://github.com/krea-ai/krea-2/blob/main/docs/prompting.md), [expander instructions](https://github.com/krea-ai/krea-2/blob/main/docs/expansion.txt) |
| Qwen-Image-2.1-enhancer.md | Precise layout, requested text, task-specific edits/transparency wording | [Official image-model card](https://huggingface.co/Qwen/Qwen-Image-2.1) |
| Ideogram-4.5-enhancer.md | Design hierarchy, quoted lettering, targeted editing briefs | [4.5 API overview](https://developer.ideogram.ai/ideogram-api/api-overview), [4.0 typography guidance](https://ideogram.ai/blog/ideogram-4-json-prompting/) (clearly identified as 4.0) |

These are not official vendor system prompts or benchmark-proven optimizations. Sources, scope, limitations, and prompt-backend instructions are in each MD. No vendor model code or weights are included. The guide picker has an expandable explanation.

Select **one or more** guides. Each selected MD produces its own prompt set, but all use the batch's chosen ComfyUI workflow. **Selecting a guide does not switch the checkpoint or automatically connect a cloud image service.** Choose the corresponding workflow yourself. Ideogram has no direct generation backend in this edition; use a compatible ComfyUI workflow or copy the generated prompts. Edit/transparency capabilities also require appropriate downstream inputs and output nodes.

The default word minimum remains **300** as requested. Lower it, or set it to **0** for no forced minimum, when a concise brief is desired. Longer text is not automatically higher quality. To update an existing library, use **Add MD files** to import the four files from `example-guides`; startup will not overwrite customized guides or resurrect ones you deliberately deleted. This local installation received the four additions without changing the generic guides.

## Job controls and privacy

Pause safely finishes the current ComfyUI image, then waits. Cancel/stop controls interrupt the queue. After abrupt closure, use Resume deliberately: the edition does not automatically restart old work on startup. Queued work advances normally once you explicitly start/resume the queue.

The edition can unload ComfyUI through its API; Windows process-exit controls require a verified ComfyUI process inside the configured local folder. It never kills an arbitrary process by name. External original files are never deleted by the export edition's recycle importer; only archived library copies can be removed. Recycled pixels still build fixed-resolution collages.

All private runtime files live in `config.json` and `data/` inside this folder. API tokens are stored locally, not encrypted; use a private OS account/folder. Back up those files privately if you want to migrate your own library. Do not share them.

## Preparing for GitHub

Use **Package Release.bat**. It creates a source-only ZIP in `dist/` using an explicit allowlist and a potential-secret check. It excludes settings, images, databases, backups, `.venv`, logs and verification evidence. **Do not upload your entire working folder.** `.gitignore` is provided, but ignored files that were already tracked by Git need to be removed from Git history separately.

No repository is initialized or published automatically. Choose a project license before inviting redistribution; this edition deliberately does not assume a license on your behalf. Read [SECURITY.md](SECURITY.md) and [AUDIT_REPORT.md](AUDIT_REPORT.md) before publishing. The security pass is not a penetration test or a guarantee against hostile ComfyUI custom nodes.

## Maintainer commands

```powershell
.\.venv\Scripts\python.exe -m app.export_server --no-browser
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check app/static/app.js
node --check app/static/export.js
py -3 scripts/package_release.py
```

Provider contracts were checked against the [Codex CLI reference](https://developers.openai.com/codex/cli/reference), [Codex configuration reference](https://developers.openai.com/codex/config-reference), [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference), [Ollama API](https://docs.ollama.com/api), and [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md). Older CLI/server versions may not implement the required isolation or unloading features; update them if Setup or a job reports that limitation.
