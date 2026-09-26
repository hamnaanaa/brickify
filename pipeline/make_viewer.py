#!/usr/bin/env python3
"""model.json (from brickify) + LDraw library -> self-contained three.js assembly viewer HTML."""
import argparse, base64, json, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from ldraw_mesh import LDrawLib

TEMPLATE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
html,body{margin:0;height:100%;background:#b9dcf3;overflow:hidden;font-family:-apple-system,system-ui,sans-serif}
#wrap{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
canvas{display:block;max-width:100vw;max-height:100vh}
#ui{position:fixed;left:14px;bottom:14px;display:flex;gap:8px;align-items:center;opacity:.9}
#ui button{font:14px system-ui;padding:8px 14px;border:0;border-radius:8px;background:#1b2a34;color:#fff;cursor:pointer}
#ui button:hover{background:#34495e}
#info{position:fixed;right:18px;top:16px;font:600 22px/1.2 system-ui;color:#1b2a34;opacity:0;letter-spacing:.2px}
#hint{position:fixed;left:14px;top:14px;font:13px system-ui;color:#1b2a34;opacity:.55}
#ui.hide,#hint.hide{display:none}
</style>
<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}</script>
</head><body>
<div id="wrap"><canvas id="c"></canvas></div>
<div id="hint">space play/pause · r restart · i counter · h hide ui · drag to orbit</div>
<div id="ui"><button id="play">replay</button><button id="rec">record 1080p</button><span id="status" style="font:13px system-ui;color:#1b2a34"></span></div>
<div id="info"></div>
<script id="model" type="application/json">__MODEL__</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const M = JSON.parse(document.getElementById('model').textContent);
const SIZE = 1080;
const canvas = document.getElementById('c');
const renderer = new THREE.WebGLRenderer({canvas, antialias:true, preserveDrawingBuffer:true});
renderer.setPixelRatio(1); renderer.setSize(SIZE, SIZE, false);
renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.0;
renderer.outputColorSpace = THREE.SRGBColorSpace;
function fit(){ const s = Math.min(innerWidth, innerHeight); canvas.style.width = s+'px'; canvas.style.height = s+'px'; }
addEventListener('resize', fit); fit();

const scene = new THREE.Scene();
scene.background = new THREE.Color('#b9dcf3');
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(renderer), 0.04).texture;

const STUD = M.stud_ldu, PLATE = M.plate_ldu;
const [NX, NY, NZ] = M.grid;
const center = new THREE.Vector3(NX*STUD/2, NY*PLATE/2, -NZ*STUD/2);
const height = NY*PLATE;

const camera = new THREE.PerspectiveCamera(28, 1, 10, 20000);
const R = height*parseFloat(new URLSearchParams(location.search).get('dist') || '__DIST__');
const Q0 = new URLSearchParams(location.search);
const AZ0 = parseFloat(Q0.get('az0') || '__AZ0__'), EL0 = parseFloat(Q0.get('el0') || '__EL0__'), ORBIT = __ORBIT__;  // az=PI faces the front
const UP = parseFloat(Q0.get('up') || '__UP__');   // raise the look-at point by this fraction of the height: model sits lower in frame
const look = new THREE.Vector3(center.x, center.y + height*UP, center.z);
camera.position.set(look.x + R*Math.sin(AZ0), look.y + height*EL0, look.z + R*Math.cos(AZ0));
const controls = new OrbitControls(camera, canvas);
controls.target.copy(look); controls.enableDamping = true; controls.dampingFactor = 0.08;
controls.autoRotate = true; controls.autoRotateSpeed = 0.7; controls.update();

scene.add(new THREE.HemisphereLight(0xffffff, 0xb9dcf3, 0.55));
const sun = new THREE.DirectionalLight(0xfff4e6, 2.9);
sun.position.set(center.x - height*1.2, center.y + height*2.2, center.z + height*1.6);
sun.target.position.copy(center); scene.add(sun.target);
sun.castShadow = true; sun.shadow.mapSize.set(4096, 4096);
const sc = sun.shadow.camera; sc.left = sc.bottom = -height*1.1; sc.right = sc.top = height*1.1; sc.near = 10; sc.far = height*8;
sun.shadow.bias = -0.00035; sun.shadow.normalBias = 1.5; scene.add(sun);
const fill = new THREE.DirectionalLight(0xffffff, 0.6); fill.position.set(center.x + height, center.y + height*0.6, center.z - height); scene.add(fill);

const ground = new THREE.Mesh(new THREE.PlaneGeometry(height*40, height*40), new THREE.ShadowMaterial({color:0x0b2a44, opacity:0.30}));
ground.rotation.x = -Math.PI/2; ground.position.set(center.x, 0, center.z); ground.receiveShadow = true; scene.add(ground);

// geometry per part number, LDraw -> three: (x, -y, -z)
function b64f32(s){ const bin = atob(s); const buf = new ArrayBuffer(bin.length); const u = new Uint8Array(buf); for (let i=0;i<bin.length;i++) u[i] = bin.charCodeAt(i); return new Float32Array(buf); }
const geoms = {};
for (const [num, b64] of Object.entries(M.geometry)) {
  const pos = b64f32(b64);
  for (let i=0;i<pos.length;i+=3){ pos[i+1] = -pos[i+1]; pos[i+2] = -pos[i+2]; }
  const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(pos, 3)); g.computeVertexNormals();
  geoms[num] = g;
}
const mats = {};
function matFor(code){
  if (mats[code]) return mats[code];
  const c = M.colours[code];
  const m = new THREE.MeshStandardMaterial({color: new THREE.Color(c.hex), roughness: 0.42, metalness: 0.0, envMapIntensity: 0.5, side: THREE.DoubleSide});
  if (c.alpha < 1) { m.transparent = true; m.opacity = 0.62; m.roughness = 0.15; m.envMapIntensity = 1.2; m.depthWrite = false; }
  mats[code] = m; return m;
}
// group instances by (part, colour)
const groups = new Map();
M.parts.forEach((p, i) => { const k = p.p + '|' + p.c; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(i); });
const inst = []; // per part: {mesh, index}
const meshes = [];
for (const [k, ids] of groups) {
  const [num, code] = k.split('|');
  const mesh = new THREE.InstancedMesh(geoms[num], matFor(code), ids.length);
  mesh.castShadow = true; mesh.receiveShadow = true; mesh.frustumCulled = false;
  if (M.colours[code].alpha < 1) mesh.renderOrder = 10;
  ids.forEach((pid, j) => { inst[pid] = {mesh, j}; });
  scene.add(mesh); meshes.push(mesh);
}
// targets and starts
const parts = M.parts;
const rnd = (() => { let s = 1234567; return () => { s = (s * 1664525 + 1013904223) % 4294967296; return s / 4294967296; }; })();
const target = [], start = [], qEnd = [], qStart = [], appear = [];
const maxStep = Math.max(...parts.map(p => p.step));
const BUILD_SECONDS = __BUILD_SECONDS__, FLY = 0.6, HOLD = __HOLD_SECONDS__;
const stepDur = (BUILD_SECONDS - FLY) / maxStep;
const inStep = {};
parts.forEach((p, i) => {
  const cx = (p.x0 + p.w/2)*STUD, cz = -(p.z0 + p.d/2)*STUD, cy = (p.y0 + p.h)*PLATE;
  target[i] = new THREE.Vector3(cx, cy, cz);
  const dir = new THREE.Vector3(cx - center.x, 0, cz - center.z); if (dir.lengthSq() < 1) dir.set(1,0,0); dir.normalize();
  start[i] = new THREE.Vector3(cx + dir.x*(height*0.22 + rnd()*height*0.15), cy + height*0.28 + rnd()*height*0.18, cz + dir.z*(height*0.22 + rnd()*height*0.15));
  qEnd[i] = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0,1,0), p.rot ? Math.PI/2 : 0);
  const tilt = new THREE.Quaternion().setFromEuler(new THREE.Euler((rnd()-0.5)*0.9, (rnd()-0.5)*0.9, (rnd()-0.5)*0.9));
  qStart[i] = tilt.multiply(qEnd[i]);
  inStep[p.step] = (inStep[p.step] || 0) + 1;
  appear[i] = (p.step - 1) * stepDur + (inStep[p.step] - 1) * 0.035;
});
const total = maxStep * stepDur + FLY + HOLD;
const hidden = new THREE.Matrix4().makeScale(0,0,0);
const tmpM = new THREE.Matrix4(), tmpP = new THREE.Vector3(), tmpQ = new THREE.Quaternion(), one = new THREE.Vector3(1,1,1);
const ease = t => 1 - Math.pow(1 - t, 3);
const Q = new URLSearchParams(location.search);
let t = parseFloat(Q.get('t') || '0'), playing = Q.get('play') !== '0', last = performance.now(), placed = 0;
if (Q.get('rotate') === '0') controls.autoRotate = false;
if (Q.get('az')) { const az = parseFloat(Q.get('az')), el = parseFloat(Q.get('el') || '0.42'); camera.position.set(look.x + R*Math.sin(az), look.y + height*el, look.z + R*Math.cos(az)); controls.update(); }
if (Q.get('ui') === '0') { document.getElementById('ui').classList.add('hide'); document.getElementById('hint').classList.add('hide'); }
function update(){
  placed = 0;
  for (let i=0;i<parts.length;i++){
    const {mesh, j} = inst[i];
    const u = (t - appear[i]) / FLY;
    if (u <= 0) { mesh.setMatrixAt(j, hidden); continue; }
    const e = ease(Math.min(u, 1)); if (u >= 1) placed++;
    tmpP.lerpVectors(start[i], target[i], e);
    tmpQ.slerpQuaternions(qStart[i], qEnd[i], e);
    tmpM.compose(tmpP, tmpQ, one); mesh.setMatrixAt(j, tmpM);
  }
  for (const m of meshes) m.instanceMatrix.needsUpdate = true;
}
const info = document.getElementById('info'), status = document.getElementById('status');
let showInfo = false;
const DET = Q.get('det') === '1';
window.__total = 0; window.__setTime = (x) => { t = x; update();
  if (DET) { const az = AZ0 + ORBIT * (x / total); camera.position.set(look.x + R*Math.sin(az), look.y + height*EL0, look.z + R*Math.cos(az)); camera.lookAt(look); }
  else controls.update();
  renderer.render(scene, camera); };
if (DET) controls.autoRotate = false;
window.__ready = true;
function frame(now){
  const dt = Math.min((now - last)/1000, 0.1); last = now;
  if (DET) { window.__total = total; requestAnimationFrame(frame); return; }
  if (playing) t += dt;
  if (t > total) { t = total; if (recording) stopRec(); }
  update(); controls.update(); renderer.render(scene, camera);
  if (showInfo) info.textContent = placed.toLocaleString() + ' / ' + parts.length.toLocaleString() + ' parts';
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
function restart(){ t = 0; playing = true; }
document.getElementById('play').onclick = restart;
addEventListener('keydown', e => {
  if (e.key === ' ') { playing = !playing; e.preventDefault(); }
  if (e.key === 'r') restart();
  if (e.key === 'i') { showInfo = !showInfo; info.style.opacity = showInfo ? 1 : 0; }
  if (e.key === 'h') { document.getElementById('ui').classList.toggle('hide'); document.getElementById('hint').classList.toggle('hide'); }
});
// recording (webm from the 1080x1080 canvas)
let recorder = null, chunks = [], recording = false;
function stopRec(){ if (recorder && recorder.state !== 'inactive') recorder.stop(); }
document.getElementById('rec').onclick = () => {
  if (recording) { stopRec(); return; }
  const stream = canvas.captureStream(60);
  const mime = MediaRecorder.isTypeSupported('video/webm;codecs=vp9') ? 'video/webm;codecs=vp9' : 'video/webm';
  recorder = new MediaRecorder(stream, {mimeType: mime, videoBitsPerSecond: 28e6});
  chunks = []; recorder.ondataavailable = e => chunks.push(e.data);
  recorder.onstop = () => { recording = false; status.textContent = 'saved'; const blob = new Blob(chunks, {type: 'video/webm'}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = '__NAME__-lego-assembly.webm'; a.click(); };
  restart(); recorder.start(100); recording = true; status.textContent = 'recording…';
};
</script></body></html>
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True); ap.add_argument("--ldraw", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="LEGO assembly"); ap.add_argument("--build-seconds", type=float, default=22)
    ap.add_argument("--hold-seconds", type=float, default=5); ap.add_argument("--orbit", type=float, default=0.95)
    ap.add_argument("--distance", type=float, default=2.35); ap.add_argument("--target-up", type=float, default=0.0); ap.add_argument("--az0", type=float, default=2.35); ap.add_argument("--el0", type=float, default=0.40)
    a = ap.parse_args()
    M = json.load(open(a.model))
    lib = LDrawLib(a.ldraw)
    nums = sorted(set(p["p"] for p in M["parts"]))
    geometry = {}
    total_tris = 0
    for n in nums:
        tri = lib.triangles(n + ".dat")
        total_tris += len(tri)
        geometry[n] = base64.b64encode(tri.astype(np.float32).tobytes()).decode()
    M["geometry"] = geometry
    html = (TEMPLATE.replace("__MODEL__", json.dumps(M, separators=(",", ":")))
            .replace("__TITLE__", a.title).replace("__NAME__", M.get("name", "model"))
            .replace("__BUILD_SECONDS__", str(a.build_seconds)).replace("__ORBIT__", str(a.orbit)).replace("__AZ0__", str(a.az0)).replace("__DIST__", str(a.distance)).replace("__UP__", str(a.target_up)).replace("__EL0__", str(a.el0)).replace("__HOLD_SECONDS__", str(a.hold_seconds)))
    open(a.out, "w").write(html)
    print(f"wrote {a.out}: {len(nums)} part types, {total_tris} unique triangles, {len(M['parts'])} instances, {os.path.getsize(a.out)/1e6:.1f} MB")

if __name__ == "__main__":
    main()
