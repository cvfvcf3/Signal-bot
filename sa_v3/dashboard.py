import json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from . import journal

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Dashboard</title>
<style>body{font-family:sans-serif;background:#0b0e11;color:#e8ecef;padding:20px;}</style></head>
<body><h1>Signal Advisor Dashboard</h1><div id="s"></div>
<script>
async function r(){
  let s=await(await fetch('/api/summary')).json();
  document.getElementById('s').innerHTML = "<pre>"+JSON.stringify(s,null,2)+"</pre>";
}
r(); setInterval(r, 5000);
</script></body></html>"""

def serve(config, port=5000):
    db = config["paper"]["journal_db"]
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path == "/":
                self.send_response(200); self.send_header("Content-type","text/html"); self.end_headers(); self.wfile.write(HTML.encode())
            elif self.path == "/api/summary":
                self.send_response(200); self.send_header("Content-type","application/json"); self.end_headers()
                self.wfile.write(json.dumps(journal.get_summary(db)).encode())
            else:
                self.send_response(404); self.end_headers()
    print(f"Dashboard: http://localhost:{port}")
    HTTPServer(("0.0.0.0", port), H).serve_forever()
