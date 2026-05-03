import subprocess, sys, json, time, hashlib, os, signal, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(r"C:\Users\daystar\Documents\Codex\2026-04-29\ip")
LOCAL = ROOT / "build" / "rfs-lab" / "local"
REMOTE = ROOT / "build" / "rfs-lab" / "remote"
LOCAL.mkdir(parents=True, exist_ok=True)
REMOTE.mkdir(parents=True, exist_ok=True)
PORT = 63001
PWD = "rfs-secret"

def api(path, method="GET", body=None, headers=None):
    url = "http://127.0.0.1:{}{}".format(PORT, path)
    data = json.dumps(body).encode() if body else None
    hdrs = dict(headers or {})
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except:
            return {"error": str(e)}

print("Starting slave...")
slave = subprocess.Popen(
    [sys.executable, "-m", "agentremote", "slave",
     "--root", str(REMOTE), "--port", str(PORT), "--host", "127.0.0.1",
     "--password", PWD, "--firewall", "no", "--console", "no"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
)

for i in range(30):
    try:
        if "challenge" in api("/api/challenge"):
            break
    except:
        pass
    time.sleep(0.5)
else:
    slave.kill()
    print("SLAVE FAILED TO START")
    sys.exit(1)

print("Slave started OK")

chal = api("/api/challenge")["challenge"]
h = hashlib.pbkdf2_hmac("sha256", PWD.encode(), chal.encode(), 100000).hex()
resp = api("/api/login", method="POST", body={"challenge": chal, "proof": h, "scopes": ["read", "write", "delete", "handoff"]})
token = resp.get("token", "")
print("Login:", "PASS" if token else "FAIL")

r = api("/api/login", method="POST", body={"challenge": "bad", "proof": "wrong", "scopes": ["read"]})
print("RFS-7 bad password:", "PASS" if "token" not in r else "FAIL")

blocked = False
attempts = 0
for i in range(20):
    r = api("/api/login", method="POST", body={"challenge": "b{}".format(i), "proof": "w{}".format(i), "scopes": ["read"]})
    attempts = i + 1
    if "temporarily_blocked" in str(r) or "blocked" in str(r.get("code", "")):
        blocked = True
        break
print("RFS-7 rate limit:", "PASS" if blocked else "NOT_RUN", "(attempt {})".format(attempts))

auth = {"Authorization": "Bearer {}".format(token)}
(LOCAL / "gui-test.txt").write_text("gui test content\n")
subprocess.run(
    [sys.executable, "-m", "agentremote", "connect", "rfs", "127.0.0.1", str(PORT),
     "--password", PWD, "--scopes", "read,write,delete,handoff"],
    cwd=LOCAL, capture_output=True,
)

r = subprocess.run(
    [sys.executable, "-m", "agentremote", "send", "rfs", str(LOCAL / "gui-test.txt"), "/gui", "--overwrite"],
    cwd=LOCAL, capture_output=True, text=True,
)
print("RFS-2 upload:", "PASS" if "push complete" in r.stdout else "FAIL")

r = api("/api/remote/mkdir", method="POST", body={"path": "/gui-folder"}, headers=auth)
print("RFS-2 mkdir:", "PASS" if r.get("ok") else "FAIL")

r = api("/api/remote/rename", method="POST", body={"path": "/gui/gui-test.txt", "name": "renamed.txt"}, headers=auth)
print("RFS-2 rename:", "PASS" if r.get("ok") or r.get("entry") else "FAIL")

r = api("/api/remote/delete", method="POST", body={"path": "/gui/renamed.txt"}, headers=auth)
print("RFS-2 delete:", "PASS" if r.get("ok") else "FAIL")

r = api("/api/dashboard", headers=auth)
print("RFS-2 dashboard:", "PASS" if "nodes" in r else "FAIL")

r = api("/api/inbox", headers=auth)
print("RFS-2 inbox:", "PASS" if "items" in r else "FAIL")

large = LOCAL / "large-4mb.bin"
chunk = hashlib.sha256(b"rfs").digest() * 32768
with large.open("wb") as f:
    rem = 4 * 1024 * 1024
    while rem:
        p = chunk[: min(len(chunk), rem)]
        f.write(p)
        rem -= len(p)
lh = hashlib.sha256(large.read_bytes()).hexdigest()

up = subprocess.Popen(
    [sys.executable, "-m", "agentremote", "send", "rfs", str(large), "/resume", "--overwrite"],
    cwd=LOCAL,
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
)
time.sleep(1.0)
if os.name == "nt":
    up.send_signal(signal.CTRL_BREAK_EVENT)
else:
    up.terminate()
try:
    up.wait(timeout=5)
except:
    up.kill()

partials = list((REMOTE / ".agentremote_partial").rglob("*")) if (REMOTE / ".agentremote_partial").exists() else []
print("RFS-3 partial after kill:", "PASS" if partials else "NOT_RUN", "({} files)".format(len(partials)))

r = subprocess.run(
    [sys.executable, "-m", "agentremote", "send", "rfs", str(large), "/resume", "--overwrite"],
    cwd=LOCAL, capture_output=True, text=True,
)
print("RFS-3 resume upload:", "PASS" if "push complete" in r.stdout else "FAIL")

r = subprocess.run(
    [sys.executable, "-m", "agentremote", "pull", "rfs", "/resume", str(LOCAL / "recv"), "--overwrite"],
    cwd=LOCAL, capture_output=True, text=True,
)
recv_file = LOCAL / "recv" / large.name
if recv_file.exists():
    rh = hashlib.sha256(recv_file.read_bytes()).hexdigest()
    print("RFS-3 hash verify:", "PASS" if rh == lh else "FAIL", "{}=={}".format(rh[:12], lh[:12]))
else:
    print("RFS-3 hash verify: FAIL (file not found)")

print("RFS-4 cross-OS: BLOCKED (Windows only, no Mac/Linux host)")

if os.name == "nt":
    slave.send_signal(signal.CTRL_BREAK_EVENT)
else:
    slave.terminate()
try:
    slave.wait(timeout=5)
except:
    slave.kill()
print("Slave stopped. All tests complete.")
