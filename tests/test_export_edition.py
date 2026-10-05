import base64
import io
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin

from app.config import Config, DEFAULTS, validate_local_url
from app.integrations import IntegrationError
from app.providers import ExportServices, VRAMSafetyError, request_local
from app import export_server

GRAPH={"1":{"class_type":"CLIPTextEncode","inputs":{"text":"a cat","clip":["2",1]}},"2":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":"example.safetensors"}},"3":{"class_type":"SaveImage","inputs":{"images":["4",0],"filename_prefix":"test"}},"4":{"class_type":"KSampler","inputs":{"seed":12}}}


class ConfigTests(unittest.TestCase):
    def test_clean_defaults_have_no_private_settings(self):
        self.assertFalse(DEFAULTS["auto_start_apps"])
        self.assertEqual("",DEFAULTS["sibling_relay_url"])
        for key in ("lm_api_token","civitai_api_token","lm_studio_exe","comfy_path"):
            self.assertEqual("",DEFAULTS[key])

    def test_local_only_urls(self):
        self.assertEqual("http://127.0.0.1:1234",validate_local_url("http://127.0.0.1:1234/"))
        for value in ("http://8.8.8.8:80","http://169.254.169.254","http://127.0.0.1:80/v1","http://user:pass@127.0.0.1:80","file:///tmp/x","http://127.0.0.1/?token=secret","http://localhost.evil.example:80"):
            with self.subTest(value=value),self.assertRaises(ValueError):
                validate_local_url(value)

    def test_config_validation_and_secret_masking(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=Config(Path(temp)/"config.json")
            cfg.update({"lm_api_token":"test-private","civitai_api_token":"also-private"})
            self.assertEqual("",cfg.all(False)["lm_api_token"])
            self.assertEqual("",cfg.all(False)["civitai_api_token"])
            for changes in ({"provider":"unknown"},{"host":"0.0.0.0"},{"lm_context_length":20},{"auto_start_apps":"true"}):
                with self.assertRaises(ValueError): cfg.update(changes)


class SecurityTests(unittest.TestCase):
    def test_reference_upload_rejects_scripts_and_forces_safe_media_type(self):
        server=export_server.legacy.AppServer(("127.0.0.1",0),export_server.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        seen=[]
        def save_source(handler,parsed):
            seen.append(handler.headers["Content-Type"])
            handler._json(201,{"ok":True})
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with patch.object(export_server.legacy.Handler,"_reference_source_upload",save_source):
                for filename in ("photo.svg","page.html","script.js"):
                    request=urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/references/source?name={filename}",data=b"image",headers={"Content-Type":"text/html","X-Wildcat-Token":export_server.TOKEN})
                    with self.assertRaises(urllib.error.HTTPError) as result: opener.open(request)
                    self.assertEqual(409,result.exception.code)
                request=urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/references/source?name=photo.png",data=b"image",headers={"Content-Type":"text/html","X-Wildcat-Token":export_server.TOKEN})
                with opener.open(request) as response:self.assertEqual(201,response.status)
            self.assertEqual(["image/png"],seen)
        finally:server.shutdown();server.server_close();thread.join(timeout=3)

    def test_simple_controls_have_one_all_jobs_stop(self):
        root=Path(__file__).resolve().parents[1]
        html=(root/"app/static/index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="emergencyStop"',html)
        self.assertIn('id="stopEntireQueue" type="button" disabled>Cancel all jobs',html)
        self.assertIn('<details class="job-control-details" id="promptRecoveryControls">',html)
        self.assertIn('<details class="job-control-details" id="comfyShutdownControls">',html)
        script=(root/"app/static/app.js").read_text(encoding="utf-8")
        self.assertIn('$("#resumeBatch").hidden = !resumable',script)
        self.assertIn('$("#pauseBatch").hidden = resumable',script)
        self.assertIn('dialog.showModal()',script)
        self.assertIn('if (!await confirmJobAction("Cancel all jobs?"',script)

    def test_manual_comfy_handoff_holds_queue_before_action(self):
        server=export_server.legacy.AppServer(("127.0.0.1",0),export_server.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        calls=[]
        def legacy_action(handler):
            calls.append("action")
            handler._json(200,{"ok":True})
        try:
            with patch.object(export_server.legacy.jobs,"pause_queue",side_effect=lambda:calls.append("hold")),patch.object(export_server.legacy.Handler,"do_POST",legacy_action):
                for action in ("stop-comfy","exit-comfy"):
                    req=urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/actions/{action}",data=b"{}",headers={"Content-Type":"application/json","X-Wildcat-Token":export_server.TOKEN})
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as response:
                        self.assertEqual(200,response.status)
            self.assertEqual(["hold","action","hold","action"],calls)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

    def test_host_origin_and_session_token(self):
        valid={"Host":"127.0.0.1:5194","Origin":"http://127.0.0.1:5194","X-Wildcat-Token":export_server.TOKEN}
        self.assertTrue(export_server.valid_request(valid,5194,True))
        for key,value in (("Host","evil.example:5194"),("Origin","https://evil.example"),("X-Wildcat-Token","wrong"),("Sec-Fetch-Site","cross-site")):
            altered=dict(valid);altered[key]=value
            self.assertFalse(export_server.valid_request(altered,5194,True))

    def test_nested_secret_keys_are_removed(self):
        self.assertEqual({"config":{"civitai_api_token":"","lm_api_token":""}},export_server.redact({"config":{"civitai_api_token":"secret","lm_api_token":"other"}}))

    def test_live_http_security_boundary(self):
        server=export_server.legacy.AppServer(("127.0.0.1",0),export_server.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f"http://127.0.0.1:{server.server_port}"
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            health=json.loads(opener.open(base+"/api/health").read())
            self.assertEqual("1.02",health["version"])
            response=opener.open(base+"/")
            self.assertIn("frame-ancestors 'none'",response.headers["Content-Security-Policy"])
            self.assertIn(export_server.TOKEN,response.read().decode())
            for req in (urllib.request.Request(base+"/api/settings",data=b"{}",headers={"Content-Type":"application/json"}),urllib.request.Request(base+"/api/bootstrap",headers={"Origin":"https://evil.example"}),urllib.request.Request(base+"/api/health",headers={"Host":"evil.example:80"})):
                with self.assertRaises(urllib.error.HTTPError) as result: opener.open(req)
                self.assertEqual(403,result.exception.code)
            req=urllib.request.Request(base+"/api/recycle-bin/delete-file",data=b'{}',headers={"Content-Type":"application/json","X-Wildcat-Token":export_server.TOKEN})
            with self.assertRaises(urllib.error.HTTPError) as result: opener.open(req)
            self.assertEqual(403,result.exception.code)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

    def test_normal_canvas_json_is_rejected_with_help(self):
        with self.assertRaisesRegex(ValueError,"API"):
            export_server.workflow_content({"content":json.dumps({"nodes":[]})})

    def test_png_graph_import_is_deterministic(self):
        info=PngImagePlugin.PngInfo();info.add_text("prompt",json.dumps(GRAPH))
        buf=io.BytesIO();Image.new("RGB",(64,96),(40,80,120)).save(buf,format="PNG",pnginfo=info)
        body={"image_data_url":"data:image/png;base64,"+base64.b64encode(buf.getvalue()).decode()}
        self.assertEqual(GRAPH,json.loads(export_server.workflow_content(body)))
        self.assertEqual(export_server.workflow_content(body),export_server.workflow_content(body))

    def test_png_without_graph_is_rejected(self):
        buf=io.BytesIO();Image.new("RGB",(8,8)).save(buf,format="PNG")
        with self.assertRaisesRegex(ValueError,"no executable"):
            export_server.workflow_content({"image_data_url":"data:image/png;base64,"+base64.b64encode(buf.getvalue()).decode()})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.cfg=Config(Path(self.temp.name)/"config.json")
        self.service=ExportServices(self.cfg)

    def test_chat_routes_to_selected_backend(self):
        self.cfg.update({"provider":"ollama"})
        with patch.object(self.service,"_request",return_value={"message":{"content":"a workable prompt"}}) as request:
            self.assertEqual("a workable prompt",self.service.chat("example","system","user"))
            self.assertEqual("/api/chat",request.call_args.args[0])
            self.assertFalse(request.call_args.args[1]["stream"])

    def test_export_services_starts_a_job_without_sibling_backend(self):
        from app.orchestrator import JobManager
        from app.storage import Storage
        store=Storage(Path(self.temp.name)/"library.sqlite3")
        manager=JobManager(store,self.cfg,self.service)
        identity=store.create_batch({"source":"paste","pasted_prompts":"a cat","title":"verification"})
        with patch.object(manager,"_launch_locked") as launch:
            self.assertEqual({"queued":False,"active_batch_id":identity},manager.start(identity))
            launch.assert_called_once_with(identity)

    def test_ollama_unload_is_verified(self):
        self.cfg.update({"provider":"ollama"})
        with patch.object(self.service,"_request",side_effect=[{"models":[{"name":"vision"}]},{"done":True},{"models":[]}]) as request:
            self.assertEqual(["vision"],self.service.unload_all_lm())
            self.assertEqual(0,request.call_args_list[1].args[1]["keep_alive"])

    def test_single_model_external_llama_cannot_silently_handoff(self):
        self.cfg.update({"provider":"llamacpp"})
        with patch.object(self.service,"_request",side_effect=IntegrationError("unsupported")):
            with self.assertRaises(VRAMSafetyError): self.service.unload_all_lm()

    def test_managed_llama_stops_only_owned_process(self):
        self.cfg.update({"provider":"llamacpp","llamacpp_managed":True})
        owned=Mock();owned.poll.return_value=None;self.service._llama_process=owned
        self.service.unload_all_lm();owned.terminate.assert_called_once();owned.wait.assert_called_once()
        self.assertIsNone(self.service._llama_process)

    def test_managed_discovery_does_not_start_a_model(self):
        self.cfg.update({"provider":"llamacpp","llamacpp_managed":True,"llamacpp_model":"test.gguf"})
        with patch.object(self.service,"_start_llama") as start:
            self.assertEqual("local-gguf",self.service.lm_models()[0]["id"]);start.assert_not_called()

    def test_unconfirmed_loaded_backend_blocks_image_phase(self):
        self.cfg.update({"provider":"ollama"});self.service._loaded_by_us=True
        with patch.object(self.service,"_request",side_effect=IntegrationError("offline")):
            with self.assertRaises(VRAMSafetyError): self.service.unload_all_lm()

    def test_busy_comfy_blocks_prompt_backend_load(self):
        with patch.object(self.service,"comfy_queue",return_value={"queue_running":[[1]],"queue_pending":[]}):
            with self.assertRaises(VRAMSafetyError): self.service.free_comfy()

    def test_cli_requires_explicit_cloud_consent(self):
        self.cfg.update({"provider":"claude"})
        with self.assertRaisesRegex(IntegrationError,"consent"):
            self.service.chat("default","system","user")

    def test_claude_command_is_tool_free_and_prompt_is_stdin(self):
        self.cfg.update({"provider":"claude","cli_cloud_consent":True})
        help_text="--tools --setting-sources --strict-mcp-config --safe-mode"
        calls=[]
        def fake_run(args,**kw):
            calls.append((args,kw))
            return subprocess.CompletedProcess(args,0,help_text if "--help" in args else '{"result":"generated prompt"}',"")
        with patch("app.providers.cli_command",return_value=["claude.exe"]),patch("app.providers.subprocess.run",side_effect=fake_run):
            self.assertEqual("generated prompt",self.service.chat("default","system","private; & prompt"))
        args,kw=calls[-1]
        self.assertEqual("",args[args.index("--tools")+1]);self.assertIn("--safe-mode",args)
        self.assertNotIn("private; & prompt",args);self.assertIn("private; & prompt",kw["input"])
        self.assertFalse(kw.get("shell",False))

    def test_codex_command_is_ephemeral_readonly_without_user_config(self):
        self.cfg.update({"provider":"codex","cli_cloud_consent":True})
        calls=[]
        def fake_run(args,**kw):
            calls.append(args)
            if "--help" in args:return subprocess.CompletedProcess(args,0,"--ignore-user-config --ephemeral","")
            Path(args[args.index("--output-last-message")+1]).write_text("generated prompt",encoding="utf-8")
            return subprocess.CompletedProcess(args,0,"","")
        with patch("app.providers.cli_command",return_value=["codex.exe"]),patch("app.providers.subprocess.run",side_effect=fake_run):
            self.assertEqual("generated prompt",self.service.chat("default","system","user"))
        args=calls[-1]
        self.assertIn("--ephemeral",args);self.assertIn("--ignore-user-config",args);self.assertIn("features.shell_tool=false",args);self.assertIn("read-only",args)

    def test_live_ollama_wire_protocol(self):
        wire=[]
        class FakeOllama(BaseHTTPRequestHandler):
            loaded=True
            def log_message(self,*args): return None
            def do_GET(self):
                result={"models":[{"name":"example:latest"}]} if self.path=="/api/tags" or self.loaded else {"models":[]}
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(result).encode())
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])));wire.append((self.path,payload))
                if self.path=="/api/generate":FakeOllama.loaded=False;result={"done":True}
                else:result={"message":{"content":"1. A cat in soft window light."}}
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(result).encode())
        server=ThreadingHTTPServer(("127.0.0.1",0),FakeOllama);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.cfg.update({"provider":"ollama","ollama_url":f"http://127.0.0.1:{server.server_port}"})
            self.assertEqual("example:latest",self.service.lm_models()[0]["id"])
            self.assertIn("soft window light",self.service.chat("example:latest","system","user"))
            self.assertEqual(["example:latest"],self.service.unload_all_lm())
            self.assertEqual(["/api/chat","/api/generate"],[item[0] for item in wire])
        finally: server.shutdown();server.server_close();thread.join(timeout=3)

    def test_network_redirect_is_refused(self):
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self,*args): return None
            def do_GET(self): self.send_response(302);self.send_header("Location","https://example.com");self.end_headers()
        server=ThreadingHTTPServer(("127.0.0.1",0),Redirect);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(IntegrationError):request_local(f"http://127.0.0.1:{server.server_port}","/models")
        finally:server.shutdown();server.server_close();thread.join(timeout=3)

    def test_context_errors_remain_retryable_without_leaking_provider_body(self):
        class ContextError(BaseHTTPRequestHandler):
            def log_message(self,*args): return None
            def do_GET(self):
                self.send_response(400);self.end_headers()
                self.wfile.write(b'{"error":"request exceeds the available context size", "private_prompt":"DO-NOT-EXPOSE-ME"}')
        server=ThreadingHTTPServer(("127.0.0.1",0),ContextError);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(IntegrationError) as result:
                request_local(f"http://127.0.0.1:{server.server_port}","/models")
            self.assertIn("context window",str(result.exception))
            self.assertNotIn("DO-NOT-EXPOSE-ME",str(result.exception))
        finally:server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=="__main__":
    unittest.main()
