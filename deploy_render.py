"""Create the Render Postgres database and web service for pantry-qr."""

import json
import os
import re
import sys
import time

import httpx

ENV = os.path.join(os.environ["LOCALAPPDATA"], "hermes", ".env")
_KEY_MATCH = re.search(r"^RENDER_API_KEY=(\S+)$", open(ENV, encoding="utf-8").read(), re.M)
if _KEY_MATCH is None:
    raise SystemExit("RENDER_API_KEY not found in the Hermes .env")
KEY = _KEY_MATCH.group(1)
OWNER = "tea-dbugqq67bikc739to44g"
REGION = "oregon"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_state.json")

c = httpx.Client(
    base_url="https://api.render.com/v1",
    headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json"},
    timeout=60,
)


def save(state):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    return {}


def call(method, path, **kw):
    r = c.request(method, path, **kw)
    if r.status_code >= 300:
        print(f"!! {method} {path} -> {r.status_code}\n{r.text[:600]}")
        sys.exit(1)
    return r.json() if r.text else {}


def create_db(state):
    if state.get("db_id"):
        return state
    body = {
        "name": "pantry-db",
        "ownerId": OWNER,
        "region": REGION,
        "plan": "free",
        "version": "16",
        "databaseName": "pantry",
        "user": "pantry",
    }
    data = call("POST", "/postgres", json=body)
    state["db_id"] = data["id"]
    save(state)
    print("created postgres:", data["id"], data.get("name"))
    return state


def wait_db(state):
    for i in range(100):
        data = call("GET", f"/postgres/{state['db_id']}")
        status = data.get("status")
        print(f"  db status: {status} ({i})")
        if status == "available":
            state["db_status"] = status
            save(state)
            return state
        if status in ("failed", "unavailable", "suspended"):
            print("db did not come up:", data)
            sys.exit(1)
        time.sleep(15)
    sys.exit("timed out waiting for postgres")


def conn_string(state):
    if state.get("db_url"):
        return state
    data = call("GET", f"/postgres/{state['db_id']}/connection-info?internal=true")
    state["db_url"] = data["internalConnectionString"]
    state["db_external_url"] = data.get("externalConnectionString", "")
    save(state)
    print("internal connection string captured")
    return state


def create_service(state):
    if state.get("service_id"):
        return state
    body = {
        "type": "web_service",
        "name": "pantry-qr",
        "ownerId": OWNER,
        "repo": "https://github.com/OldguyRick/pantry-qr",
        "branch": "main",
        "autoDeploy": "yes",
        "serviceDetails": {
            "env": "docker",
            "plan": "free",
            "region": REGION,
            "healthCheckPath": "/healthz",
            "numInstances": 1,
            "envSpecificDetails": {
                "dockerfilePath": "./Dockerfile",
                "dockerContext": ".",
            },
            "envVars": [
                {"key": "DATABASE_URL", "value": state["db_url"]},
                {"key": "SEED_DEMO", "value": "1"},
            ],
        },
    }
    data = call("POST", "/services", json=body)
    svc = data.get("service", data)
    state["service_id"] = svc["id"]
    state["service_url"] = svc.get("serviceDetails", {}).get("url") or svc.get("url")
    save(state)
    print("created service:", svc["id"], state.get("service_url"))
    return state


def wait_deploy(state):
    for i in range(120):
        deps = call("GET", f"/services/{state['service_id']}/deploys?limit=1")
        if deps:
            d = deps[0]["deploy"]
            status = d["status"]
            print(f"  deploy {d['id']}: {status} ({i})")
            state["deploy_status"] = status
            save(state)
            if status == "live":
                return state
            if status in ("build_failed", "update_failed", "canceled", "pre_deploy_failed"):
                log = call("GET", f"/services/{state['service_id']}/deploys/{d['id']}")
                print("FAILED:", json.dumps(log)[:1200])
                sys.exit(1)
        time.sleep(20)
    sys.exit("timed out waiting for deploy")


if __name__ == "__main__":
    st = load()
    st = create_db(st)
    st = wait_db(st)
    st = conn_string(st)
    st = create_service(st)
    st = wait_deploy(st)
    print("\nDONE:", json.dumps({k: st[k] for k in ("db_id", "service_id", "service_url", "deploy_status") if k in st}, indent=2))
