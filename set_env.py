"""Set the service env vars (creation-time envVars were ignored) and wait for the deploy."""

import json
import os
import re
import sys
import time

import httpx

ENV = os.path.join(os.environ["LOCALAPPDATA"], "hermes", ".env")
KEY = re.search(r"^RENDER_API_KEY=(\S+)$", open(ENV, encoding="utf-8").read(), re.M).group(1)
state = json.load(open("render_state.json", encoding="utf-8"))
sid = state["service_id"]

c = httpx.Client(
    base_url="https://api.render.com/v1",
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    timeout=60,
)

body = [
    {"key": "DATABASE_URL", "value": state["db_url"]},
    {"key": "SEED_DEMO", "value": "1"},
]
r = c.put(f"/services/{sid}/env-vars", content=json.dumps(body))
print("PUT env-vars:", r.status_code, r.text[:200])

time.sleep(5)
print("now:", [(e.get("envVar", e).get("key"), (e.get("envVar", e).get("value") or "")[:38]) for e in c.get(f"/services/{sid}/env-vars").json()])

for i in range(90):
    deps = c.get(f"/services/{sid}/deploys?limit=1").json()
    if deps:
        d = deps[0]["deploy"]
        print(f"  deploy {d['id']}: {d['status']} ({i})", flush=True)
        if d["status"] == "live":
            print("LIVE")
            sys.exit(0)
        if d["status"] in ("build_failed", "update_failed", "canceled", "pre_deploy_failed"):
            print("FAILED", d["status"])
            sys.exit(1)
    time.sleep(15)
sys.exit("timeout")
