#!/usr/bin/env python3
"""Step 1: one image -> one GLB mesh through fal.ai's queue API (no SDK needed).

usage: FAL_KEY=... python image_to_3d.py --image photo.png --out out/ [--model hunyuan3d-v3|trellis|hunyuan3d-v2]
Prices when this was written (Sept 2026): hunyuan3d-v3 $0.375 (textured, best for characters and scenes),
trellis $0.02 (fast, fine for simple shapes), hunyuan3d-v2 $0.16 (white mesh, no texture).
"""
import argparse, base64, json, mimetypes, os, sys, time, urllib.error, urllib.request

QUEUE = "https://queue.fal.run"
STORAGE_INITIATE = "https://rest.alpha.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3"
MODELS = {
    "hunyuan3d-v3": dict(id="fal-ai/hunyuan3d-v3/image-to-3d", field="input_image_url", args={"generate_type": "Normal"}, mesh="model_glb"),
    "trellis": dict(id="fal-ai/trellis", field="image_url", args={}, mesh="model_mesh"),
    "hunyuan3d-v2": dict(id="fal-ai/hunyuan3d/v2", field="input_image_url", args={"textured_mesh": False}, mesh="model_mesh"),
}


def http(method, url, data=None, headers=None, timeout=600):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upload(path, auth):
    ctype = mimetypes.guess_type(path)[0] or "image/png"
    data = open(path, "rb").read()
    st, resp = http("POST", STORAGE_INITIATE, json.dumps({"content_type": ctype, "file_name": os.path.basename(path)}).encode(),
                    {**auth, "Content-Type": "application/json", "Accept": "application/json"})
    if st == 200:
        j = json.loads(resp)
        st2, _ = http("PUT", j["upload_url"], data, {"Content-Type": ctype})
        if st2 in (200, 201, 204):
            return j["file_url"]
    return f"data:{ctype};base64,{base64.b64encode(data).decode()}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--model", choices=MODELS, default="hunyuan3d-v3")
    a = ap.parse_args()
    key = os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")
    if not key:
        sys.exit("set FAL_KEY (https://fal.ai/dashboard/keys)")
    auth = {"Authorization": f"Key {key}"}
    m = MODELS[a.model]
    os.makedirs(a.out, exist_ok=True)
    image_url = upload(a.image, auth)
    st, body = http("POST", f"{QUEUE}/{m['id']}", json.dumps({m["field"]: image_url, **m["args"]}).encode(),
                    {**auth, "Content-Type": "application/json"})
    if st not in (200, 202):
        sys.exit(f"submit failed HTTP {st}: {body[:500]!r}")
    sub = json.loads(body)
    print("submitted", sub.get("request_id"))
    t0 = time.time()
    while True:
        st, body = http("GET", sub["status_url"], headers=auth)
        s = json.loads(body); state = s.get("status")
        if state == "COMPLETED":
            break
        if state not in ("IN_QUEUE", "IN_PROGRESS"):
            sys.exit(f"unexpected status: {body[:500]!r}")
        if time.time() - t0 > 1800:
            sys.exit("timeout")
        time.sleep(3)
    st, body = http("GET", sub["response_url"], headers=auth)
    out = json.loads(body)
    node = out.get(m["mesh"]) or {}
    url = node.get("url") if isinstance(node, dict) else node
    if not url:
        sys.exit(f"no mesh url in response: {json.dumps(out)[:500]}")
    st, data = http("GET", url)
    name = os.path.splitext(os.path.basename(a.image))[0]
    dest = os.path.join(a.out, f"{name}.glb")
    open(dest, "wb").write(data)
    json.dump(out, open(os.path.join(a.out, f"{name}_fal_response.json"), "w"), indent=2)
    print(f"saved {dest} ({len(data)/1e6:.1f} MB) in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
