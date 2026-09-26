#!/usr/bin/env python3
"""Expand LDraw part files (.dat) into triangle soups. Recursive, cached, no BFC handling
(the viewer renders double-sided with per-face normals)."""
import os, re, sys
import numpy as np

class LDrawLib:
    def __init__(self, root):
        self.root = root
        self.cache = {}
        self.index = {}
        for sub in ("parts", "p", "parts/s", "p/48", "p/8", "models"):
            d = os.path.join(root, sub)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.lower().endswith(".dat"):
                    key = os.path.join(sub, f).lower().replace("\\", "/")
                    self.index[key] = os.path.join(d, f)
                    self.index.setdefault(f.lower(), os.path.join(d, f))

    def resolve(self, name):
        n = name.strip().replace("\\", "/").lower()
        for cand in (n, "parts/" + n, "p/" + n, "parts/s/" + n.split("/")[-1], "p/48/" + n.split("/")[-1]):
            if cand in self.index:
                return self.index[cand]
        base = n.split("/")[-1]
        return self.index.get(base)

    def triangles(self, name, depth=0):
        """returns (n,3,3) float32 array of triangles in the part's own coordinates"""
        key = name.strip().replace("\\", "/").lower()
        if key in self.cache:
            return self.cache[key]
        path = self.resolve(name)
        if path is None or depth > 40:
            if depth == 0:
                print("missing", name, file=sys.stderr)
            self.cache[key] = np.zeros((0, 3, 3), np.float32)
            return self.cache[key]
        tris = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                t = line.split()
                if not t:
                    continue
                if t[0] == "3" and len(t) >= 11:
                    v = np.array(t[2:11], dtype=np.float64).reshape(3, 3)
                    tris.append(v)
                elif t[0] == "4" and len(t) >= 14:
                    v = np.array(t[2:14], dtype=np.float64).reshape(4, 3)
                    tris.append(v[[0, 1, 2]]); tris.append(v[[0, 2, 3]])
                elif t[0] == "1" and len(t) >= 15:
                    x, y, z = map(float, t[2:5])
                    a, b, c, d, e, f_, g, h, i = map(float, t[5:14])
                    M = np.array([[a, b, c], [d, e, f_], [g, h, i]])
                    sub = self.triangles(" ".join(t[14:]), depth + 1)
                    if len(sub):
                        tris.append((sub.reshape(-1, 3) @ M.T + np.array([x, y, z])).reshape(-1, 3, 3))
        if tris:
            flat = [tr.reshape(-1, 3, 3) if tr.ndim == 3 else tr.reshape(1, 3, 3) for tr in tris]
            out = np.concatenate(flat).astype(np.float32)
        else:
            out = np.zeros((0, 3, 3), np.float32)
        self.cache[key] = out
        return out

if __name__ == "__main__":
    lib = LDrawLib(sys.argv[1])
    for p in sys.argv[2:]:
        tr = lib.triangles(p + ".dat")
        print(p, len(tr), "tris", tr.reshape(-1, 3).min(0), tr.reshape(-1, 3).max(0))
