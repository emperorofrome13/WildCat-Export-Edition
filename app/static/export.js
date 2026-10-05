"use strict";

// All requests stay same-origin. Private session authorization never reaches AI services.
const exportNativeFetch = window.fetch.bind(window);
window.fetch = (input, options = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url, location.href);
  if (url.origin !== location.origin) return Promise.reject(new Error("WildCat only makes browser requests to its own local server."));
  const headers = new Headers(options.headers || (input instanceof Request ? input.headers : {}));
  headers.set("X-Wildcat-Token", document.querySelector('meta[name="wildcat-token"]')?.content || "");
  return exportNativeFetch(input, { ...options, headers });
};

async function exportWorkflowFile(file) {
  if (file.size > 30 * 1024 * 1024) throw new Error("Workflow file exceeds 30 MB. Use a smaller PNG or API JSON export.");
  if (/\.png$/i.test(file.name)) {
    const image_data_url = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error("Could not read that workflow PNG.")); reader.readAsDataURL(file); });
    return { filename: file.name, image_data_url };
  }
  if (!/\.json$/i.test(file.name)) throw new Error("Choose API JSON or an original ComfyUI PNG.");
  return { filename: file.name, content: await file.text() };
}

document.addEventListener("DOMContentLoaded", async () => {
  document.addEventListener("click",(event)=> {const link=event.target.closest("a[data-batch-id]");if(link){event.preventDefault();openBatch(link.dataset.batchId);}});
  const el = (id) => document.getElementById(id);
  const setup = el("exportSetup");
  let step = 0, cfg = {}, models = [];
  const urls = { lmstudio: "http://127.0.0.1:1234", ollama: "http://127.0.0.1:11434", llamacpp: "http://127.0.0.1:8080" };
  const urlKeys = { lmstudio: "lm_url", ollama: "ollama_url", llamacpp: "llamacpp_url" };
  const notes = {
    lmstudio: "Start LM Studio's local server from its Developer screen. This backend keeps everything on your computer and supports automatic model unloading.",
    ollama: "Start Ollama, then choose an installed model. Use a vision-capable model for photo prompts. WildCat unloads Ollama models before ComfyUI runs.",
    llamacpp: "Managed mode starts your chosen llama-server only when needed and stops it before image generation. An external server must support router /models/unload; a standard externally started single-model server cannot safely hand off VRAM.",
    claude: "Install a recent Claude Code CLI and sign in from a terminal first. WildCat runs isolated, non-interactive prompt requests with built-in tools and MCP disabled. Nothing is sent until you run a prompt job.",
    codex: "Install a recent Codex CLI and sign in from a terminal first. WildCat uses non-interactive, ephemeral read-only requests with shell tools and user configuration disabled. Account usage still applies.",
  };
  async function call(path, body) {
    const response = await fetch(path, body ? { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) } : {});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not complete this action.");
    return data;
  }
  function message(id, text, error=false) { el(id).textContent=text; el(id).classList.toggle("is-error", error); }
  function showStep() {
    document.querySelectorAll("[data-setup-page]").forEach((page) => { page.hidden = Number(page.dataset.setupPage) !== step; });
    document.querySelectorAll("[data-setup-step]").forEach((item) => item.classList.toggle("is-current", Number(item.dataset.setupStep)===step));
    el("setupBack").disabled = step===0;
    el("setupNext").hidden = step===3;
    el("setupStepLabel").textContent = `Step ${step+1} of 4`;
    if (step===3) {
      const box=el("setupSummary"); box.replaceChildren();
      for (const line of [`Prompts: ${el("setupPasteOnly").checked ? "Pasted by you" : el("setupProvider").selectedOptions[0].textContent}`, `ComfyUI: ${el("setupComfyUrl").value}`, `Generation workflows: ${state.workflows.length}`, "Separate private library · Local-only control panel · One GPU lane"]) { const p=document.createElement("p"); p.textContent=line; box.append(p); }
    }
  }
  function providerChanged() {
    const provider = el("setupProvider").value;
    const cloud = ["claude","codex"].includes(provider);
    el("setupProviderHelp").textContent = notes[provider];
    el("setupServerFields").hidden = cloud;
    el("setupCliFields").hidden = !cloud;
    el("setupLlamaFields").hidden = provider!=="llamacpp";
    el("setupManagedPaths").hidden = !el("setupLlamaManaged").checked;
    el("setupUrl").value = cfg[urlKeys[provider]] || urls[provider] || "";
    el("setupCliExe").value = cfg[`${provider}_exe`] || "";
    models=[]; renderModelChoices();
  }
  function renderModelChoices(selected) {
    const select=el("setupModel"); select.replaceChildren();
    select.add(new Option(models.length ? "Choose a model…" : "Check backend to find models", ""));
    for(const model of models) select.add(new Option(model.name + (model.vision && !model.vision_unverified ? " · image support" : ""), model.id));
    if (selected && models.some((m)=>m.id===selected)) select.value=selected;
  }
  async function save() {
    const provider=el("setupProvider").value;
    const changes={provider,comfy_url:el("setupComfyUrl").value.trim(),comfy_path:el("setupComfyPath").value.trim(),llamacpp_managed:el("setupLlamaManaged").checked,llamacpp_exe:el("setupLlamaExe").value.trim(),llamacpp_model:el("setupLlamaModel").value.trim(),llamacpp_mmproj:el("setupLlamaMmproj").value.trim(),cli_model:el("setupCliModel").value.trim(),cli_cloud_consent:el("setupCloudConsent").checked};
    if(urlKeys[provider]) changes[urlKeys[provider]]=el("setupUrl").value.trim();
    if(["claude","codex"].includes(provider)) changes[`${provider}_exe`]=el("setupCliExe").value.trim();
    const model=el("setupModel").value || (["claude","codex"].includes(provider) ? changes.cli_model || "default" : "");
    if(model) Object.assign(changes,{prompt_model:model,qa_model:model,comparison_model:model});
    else if(provider!==cfg.provider) Object.assign(changes,{prompt_model:"",qa_model:"",comparison_model:""});
    const payload=await call("/api/settings",changes); cfg=payload.config; state.config=cfg; populateSettings();
  }
  async function busy(id, target, action) {
    const button=typeof id === "string" ? el(id) : id, old=button.textContent; button.disabled=true; button.textContent="Checking…";
    try { await action(); } catch(error) { message(target,error.message,true); } finally {button.disabled=false; button.textContent=old;}
  }
  el("setupProvider").addEventListener("change",providerChanged);
  el("setupPasteOnly").addEventListener("change",()=>{el("setupPromptFields").hidden=el("setupPasteOnly").checked;});
  el("setupLlamaManaged").addEventListener("change",()=>{el("setupManagedPaths").hidden=!el("setupLlamaManaged").checked;});
  el("openSetup").addEventListener("click",async()=> {try{const p=await call("/api/bootstrap"); cfg=p.config; fill(); setup.hidden=false; step=0; showStep(); setup.scrollIntoView({behavior:"smooth"});}catch(error){toast(error.message,true);}});
  el("closeSetup").addEventListener("click",()=>{setup.hidden=true; document.body.classList.remove("setup-required");});
  el("setupBack").addEventListener("click",()=>{step=Math.max(0,step-1);showStep();});
  el("setupNext").addEventListener("click",()=>busy("setupNext","setupMessage",async()=>{await save(); message("setupMessage",""); step=Math.min(3,step+1);showStep();}));
  el("testPromptBackend").addEventListener("click",()=>busy("testPromptBackend","setupPromptResult",async()=>{await save(); const result=await call("/api/setup/test",{service:"prompt"}); models=result.models||[]; renderModelChoices(cfg.prompt_model); message("setupPromptResult",result.message); state.models=models; $("#promptModel").innerHTML=modelOptions(el("setupModel").value); }));
  el("testComfyBackend").addEventListener("click",()=>busy("testComfyBackend","setupComfyResult",async()=>{await save();const result=await call("/api/setup/test",{service:"comfy"});message("setupComfyResult",result.message);}));
  el("checkWorkflowNodes").addEventListener("click",()=>busy("checkWorkflowNodes","setupWorkflowResult",async()=>{const result=await call("/api/setup/workflow-check",{workflow_id:state.config.active_workflow_id}); message("setupWorkflowResult",result.message,Boolean(result.missing_nodes.length));}));
  el("setupMapping").addEventListener("click",()=>{setup.hidden=true;document.body.classList.remove("setup-required");switchTab("settings");el("workflowSummary").scrollIntoView({behavior:"smooth"});});
  el("finishSetup").addEventListener("click",()=>busy("finishSetup","setupMessage",async()=>{await save();const result=await call("/api/settings",{setup_complete:true}); cfg=result.config;state.config=cfg; setup.hidden=true;document.body.classList.remove("setup-required");if(el("setupPasteOnly").checked){const radio=document.querySelector('input[name=source][value=paste]');radio.checked=true;radio.dispatchEvent(new Event("change",{bubbles:true}));}await loadModels();toast("Setup saved. Start with a small test batch.");}));
  for(const button of document.querySelectorAll("[data-browse]")) button.addEventListener("click",()=>busy(button,"setupMessage",async()=>{message("setupMessage","Choose a file in the system dialog…");const result=await call("/api/setup/browse",{kind:button.dataset.kind});if(result.path)el(button.dataset.browse).value=result.path;message("setupMessage",result.path?"Path selected.":"No file selected.");}));
  async function importFiles(files) {
    const list=[...files]; if(!list.length)return;
    let count=0; message("setupWorkflowResult",`Importing ${list.length} workflow(s)…`);
    for(const file of list) {try {const payload=await call("/api/workflow",await exportWorkflowFile(file)); state.workflows=payload.workflows;state.config.active_workflow_id=payload.active_workflow_id;state.config.workflow_name=payload.workflow_entry.name;state.config.workflow_positive_fields=payload.workflow_entry.positive_fields;state.config.workflow_negative_fields=payload.workflow_entry.negative_fields; renderWorkflowChoices(payload.active_workflow_id);renderWorkflow(payload.workflow);count++;message("setupWorkflowResult",`${count} of ${list.length} imported. Confirm prompt mapping in Connections & workflow.`);} catch(error){message("setupWorkflowResult",`${count} imported; ${file.name}: ${error.message}`,true);return;}}
  }
  el("setupWorkflowFiles").addEventListener("change",async(event)=>{await importFiles(event.target.files);event.target.value="";});
  function dropZone(zone, action) {zone.addEventListener("dragover",(e)=>{e.preventDefault();zone.classList.add("is-dragging");});zone.addEventListener("dragleave",()=>zone.classList.remove("is-dragging"));zone.addEventListener("drop",(e)=>{e.preventDefault();zone.classList.remove("is-dragging");action(e.dataTransfer.files);});}
  dropZone(el("setupWorkflowDrop"),importFiles);
  for(const inputId of ["workflowFile","imageToolWorkflowFile"]) {const input=el(inputId);dropZone(input.closest(".upload-zone"),(files)=>{const target={files,value:""};(inputId==="workflowFile"?uploadWorkflow:uploadImageToolWorkflows)({target});});}
  function fill() {
    el("setupProvider").value=cfg.provider||"lmstudio";el("setupComfyUrl").value=cfg.comfy_url||"http://127.0.0.1:8188";el("setupComfyPath").value=cfg.comfy_path||"";
    el("setupLlamaManaged").checked=Boolean(cfg.llamacpp_managed);el("setupLlamaExe").value=cfg.llamacpp_exe||"";el("setupLlamaModel").value=cfg.llamacpp_model||"";el("setupLlamaMmproj").value=cfg.llamacpp_mmproj||"";el("setupCliModel").value=cfg.cli_model||"";el("setupCloudConsent").checked=Boolean(cfg.cli_cloud_consent);providerChanged();
  }
  const theme=localStorage.getItem("wildcatExportTheme")||"dark";document.documentElement.dataset.theme=theme;el("exportTheme").value=theme;
  el("exportTheme").addEventListener("change",()=>{document.documentElement.dataset.theme=el("exportTheme").value;localStorage.setItem("wildcatExportTheme",el("exportTheme").value);});
  try {const payload=await call("/api/bootstrap");cfg=payload.config;fill();setup.hidden=Boolean(cfg.setup_complete);document.body.classList.toggle("setup-required",!cfg.setup_complete);showStep();}catch(error){setup.hidden=false;message("setupMessage",error.message,true);}
});
