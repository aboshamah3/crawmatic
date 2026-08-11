"""Build the price_monitor egg in-container and register it with local scrapyd."""
import base64, glob, os, subprocess, urllib.request, uuid

os.chdir("/app/apps/scrapers")
subprocess.run(["python", "setup.py", "-q", "bdist_egg"], check=True)
egg = open(sorted(glob.glob("dist/*.egg"))[-1], "rb").read()

boundary = uuid.uuid4().hex
parts = []
for name, val in (("project", "price_monitor"), ("version", "r" + uuid.uuid4().hex[:8])):
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode()
    )
parts.append(
    f'--{boundary}\r\nContent-Disposition: form-data; name="egg"; filename="p.egg"\r\n'
    f"Content-Type: application/octet-stream\r\n\r\n".encode() + egg + b"\r\n"
)
parts.append(f"--{boundary}--\r\n".encode())
body = b"".join(parts)

auth = base64.b64encode(
    f"{os.environ['SCRAPYD_USERNAME']}:{os.environ['SCRAPYD_PASSWORD']}".encode()
).decode()
for endpoint in ("addversion.json",):
    req = urllib.request.Request(
        f"http://localhost:6800/{endpoint}",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Basic {auth}",
        },
    )
    print(urllib.request.urlopen(req).read().decode())

req = urllib.request.Request(
    "http://localhost:6800/listspiders.json?project=price_monitor",
    headers={"Authorization": f"Basic {auth}"},
)
print(urllib.request.urlopen(req).read().decode())
