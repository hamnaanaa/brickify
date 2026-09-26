#!/usr/bin/env python3
"""brickify: textured mesh (GLB) -> LEGO brick model made of real parts.

mesh -> voxels at LEGO resolution (8 mm studs, 3.2 mm plates) -> colour classes ->
bricks/plates packed per layer with staggered seams -> stud connectivity, collision and
centre-of-mass checks -> LDraw file (opens in BrickLink Studio / LDCad / LeoCAD),
parts list CSV, BrickLink wanted list, model.json for the viewer, report.json.

usage: python brickify.py --glb model.glb --height-mm 270 --out outdir --name muse
"""
import argparse, csv, json, math, os, sys, time
from collections import Counter, defaultdict, deque
import numpy as np
import trimesh
from scipy.spatial import cKDTree, ConvexHull, Delaunay
from scipy import ndimage

STUD_MM, PLATE_MM = 8.0, 3.2
LDU_STUD, LDU_PLATE = 20, 8

# LDraw colour codes
TAN, WHITE, BLACK, PINK, TBLUE, DBLUE = 19, 15, 0, 29, 43, 272
COLOUR_INFO = {  # code: (name, hex, alpha, bricklink id)
    TAN: ("Tan", "#D7BA8C", 1.0, 2), WHITE: ("White", "#F4F4F4", 1.0, 1),
    BLACK: ("Black", "#1B2A34", 1.0, 11), PINK: ("Bright Pink", "#FF9ECD", 1.0, 104),
    TBLUE: ("Trans-Light Blue", "#AEE9EF", 0.55, 15), DBLUE: ("Dark Blue", "#19325A", 1.0, 63),
}
PROTECTED = {BLACK, PINK}  # never smoothed away (eyes, cheeks)

# broad palette for --colour-mode palette (common, currently produced LEGO colours)
PALETTE_INFO = {
    0: ("Black", "#1B2A34", 1.0, 11), 15: ("White", "#F4F4F4", 1.0, 1), 4: ("Red", "#C91A09", 1.0, 5),
    1: ("Blue", "#1E5AA8", 1.0, 7), 14: ("Yellow", "#FAC80A", 1.0, 3), 2: ("Green", "#00852B", 1.0, 6),
    25: ("Orange", "#FE8A18", 1.0, 4), 19: ("Tan", "#D7BA8C", 1.0, 2), 28: ("Dark Tan", "#897D62", 1.0, 69),
    70: ("Reddish Brown", "#5F3109", 1.0, 88), 71: ("Light Bluish Gray", "#969696", 1.0, 86),
    72: ("Dark Bluish Gray", "#646464", 1.0, 85), 272: ("Dark Blue", "#19325A", 1.0, 63),
    288: ("Dark Green", "#00451A", 1.0, 80), 320: ("Dark Red", "#720012", 1.0, 59),
    321: ("Dark Azure", "#469BC3", 1.0, 153), 322: ("Medium Azure", "#68C3E2", 1.0, 156),
    29: ("Bright Pink", "#FF9ECD", 1.0, 104), 26: ("Magenta", "#901F76", 1.0, 71), 27: ("Lime", "#A5CA18", 1.0, 34),
    191: ("Bright Light Orange", "#FCAC00", 1.0, 110), 226: ("Bright Light Yellow", "#FFEC6C", 1.0, 103),
    212: ("Bright Light Blue", "#9FC3E9", 1.0, 105), 84: ("Medium Nougat", "#AA7D55", 1.0, 150),
    78: ("Light Nougat", "#FFC995", 1.0, 90), 308: ("Dark Brown", "#352100", 1.0, 120),
    85: ("Dark Purple", "#5F27AA", 1.0, 89), 30: ("Lavender", "#A06EB9", 1.0, 154), 73: ("Medium Blue", "#7396C8", 1.0, 42),
    10: ("Bright Green", "#4B9F4A", 1.0, 36), 3: ("Dark Turquoise", "#069D9F", 1.0, 39), 323: ("Light Aqua", "#D3F2EA", 1.0, 152),
    378: ("Sand Green", "#708E7C", 1.0, 48), 379: ("Sand Blue", "#70819A", 1.0, 55), 330: ("Olive Green", "#77774E", 1.0, 155),
    484: ("Dark Orange", "#91501C", 1.0, 68), 43: ("Trans-Light Blue", "#AEE9EF", 0.55, 15),
}


def load_ldconfig(path):
    """refresh palette hex values from LDConfig.ldr so renders use the library's own colours"""
    import re
    if not path or not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r"0 !COLOUR\s+(\S+)\s+CODE\s+(\d+)\s+VALUE\s+(#[0-9A-Fa-f]{6})", line)
        if m:
            code = int(m.group(2))
            for table in (PALETTE_INFO, COLOUR_INFO):
                if code in table:
                    name, _, alpha, bl = table[code]
                    table[code] = (name, m.group(3).upper(), alpha, bl)


def hex_to_lab(hexes):
    rgb = np.array([[int(h[i:i+2], 16) for i in (1, 3, 5)] for h in hexes], dtype=np.float64) / 255.0
    return rgb_to_lab(rgb)


def rgb_to_lab(rgb):
    rgb = np.asarray(rgb, dtype=np.float64)
    if rgb.max() > 1.0:
        rgb = rgb / 255.0
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    L = 116 * f[:, 1] - 16; a = 500 * (f[:, 0] - f[:, 1]); b = 200 * (f[:, 1] - f[:, 2])
    return np.stack([L, a, b], axis=1)


def palette_colours(grid, surface, srgb, sidx, min_frac=0.01, passes=2, exclude=(), contrast_max=32.0, gain=1.0):
    """nearest LEGO colour in Lab for every surface cell, rare colours folded into the nearest kept one,
    then a majority filter to remove speckle."""
    codes_all = [c for c in PALETTE_INFO if c not in exclude]
    lab_pal = hex_to_lab([PALETTE_INFO[c][1] for c in codes_all])
    lab = rgb_to_lab(np.clip(srgb.astype(np.float64) * gain, 0, 255))
    d = ((lab[:, None, :] - lab_pal[None, :, :]) ** 2).sum(axis=2)
    pick = np.array(codes_all)[d.argmin(axis=1)]
    cnt = Counter(pick.tolist()); n = len(pick)
    keep = [c for c, k in cnt.items() if k >= min_frac * n]
    if keep and len(keep) < len(codes_all):
        keep_lab = hex_to_lab([PALETTE_INFO[c][1] for c in keep])
        d2 = ((lab[:, None, :] - keep_lab[None, :, :]) ** 2).sum(axis=2)
        pick = np.array(keep)[d2.argmin(axis=1)]
    grid[tuple(sidx.T)] = pick
    nx, ny, nz = grid.shape
    lab_of = {c: hex_to_lab([PALETTE_INFO[c][1]])[0] for c in PALETTE_INFO}
    for _ in range(passes):
        g = grid.copy()
        for x, y, z in sidx:
            c = grid[x, y, z]
            x0, x1 = max(0, x-1), min(nx, x+2); y0, y1 = max(0, y-1), min(ny, y+2); z0, z1 = max(0, z-1), min(nz, z+2)
            block = grid[x0:x1, y0:y1, z0:z1]; sb = surface[x0:x1, y0:y1, z0:z1]
            vals = block[sb & (block >= 0)]
            if len(vals) < 6:
                continue
            k = Counter(vals.tolist()); k[c] -= 1
            best, nb = max(k.items(), key=lambda kv: kv[1])
            # only fold a speckle into its surroundings when the colours are close; high-contrast dots are features
            if best != c and k.get(c, 0) <= 2 and nb >= 8 and np.linalg.norm(lab_of[c] - lab_of[best]) < contrast_max:
                g[x, y, z] = best
        grid = g
    used = Counter(grid[surface].tolist())
    return grid, used

# real parts: (long, short) studs -> LDraw part number. long side is X in the .dat file
BRICKS = {(1,1):"3005",(2,1):"3004",(3,1):"3622",(4,1):"3010",(6,1):"3009",(8,1):"3008",
          (2,2):"3003",(3,2):"3002",(4,2):"3001",(6,2):"2456",(8,2):"3007"}
PLATES = {(1,1):"3024",(2,1):"3023",(3,1):"3623",(4,1):"3710",(6,1):"3666",(8,1):"3460",
          (2,2):"3022",(3,2):"3021",(4,2):"3020",(6,2):"3795",(8,2):"3034",
          (4,4):"3031",(6,4):"3032",(8,4):"3035",(6,6):"3958",(8,6):"3036",(8,8):"41539"}
PART_NAMES = {"3005":"Brick 1x1","3004":"Brick 1x2","3622":"Brick 1x3","3010":"Brick 1x4","3009":"Brick 1x6",
  "3008":"Brick 1x8","3003":"Brick 2x2","3002":"Brick 2x3","3001":"Brick 2x4","2456":"Brick 2x6","3007":"Brick 2x8",
  "3024":"Plate 1x1","3023":"Plate 1x2","3623":"Plate 1x3","3710":"Plate 1x4","3666":"Plate 1x6","3460":"Plate 1x8",
  "3022":"Plate 2x2","3021":"Plate 2x3","3020":"Plate 2x4","3795":"Plate 2x6","3034":"Plate 2x8","3031":"Plate 4x4",
  "3032":"Plate 4x6","3035":"Plate 4x8","3958":"Plate 6x6","3036":"Plate 6x8","41539":"Plate 8x8"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------- colours
def rgb_to_hsv(rgb):
    r, g, b = (rgb / 255.0).T
    mx, mn = np.max(rgb, axis=1) / 255.0, np.min(rgb, axis=1) / 255.0
    v = mx
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-9), 0)
    d = np.maximum(mx - mn, 1e-9)
    h = np.zeros_like(mx)
    m_r = (mx == r); m_g = (mx == g) & ~m_r; m_b = ~(m_r | m_g)
    h[m_r] = ((g - b) / d)[m_r] % 6
    h[m_g] = ((b - r) / d)[m_g] + 2
    h[m_b] = ((r - g) / d)[m_b] + 4
    h = (h * 60) % 360
    return h, s, v


def classify(rgb, th):
    """rgb (n,3) uint8 -> LDraw colour codes (n,)"""
    h, s, v = rgb_to_hsv(rgb.astype(np.float64))
    out = np.full(len(rgb), TAN, dtype=np.int32)
    blue = (s > th["blue_s"]) & (h > 170) & (h < 265)
    pink = (s > th["pink_s"]) & ((h >= 320) | (h <= 15)) & (v > 0.5)
    white = (v > th["white_v"]) & (s < th["white_s"])
    black = v < th["black_v"]
    out[white] = WHITE
    out[pink] = PINK
    out[blue] = TBLUE
    out[black] = BLACK
    return out


# ----------------------------------------------------------------------------- mesh -> voxels
def load_and_voxelize(glb, height_mm, keep_components=1):
    mesh = trimesh.load(glb, force="mesh")
    log(f"loaded {len(mesh.vertices)} verts, {len(mesh.faces)} faces, extents mm-free {np.round(mesh.extents,3)}")
    vcol = np.asarray(mesh.visual.to_color().vertex_colors)[:, :3].astype(np.uint8)
    # geometry copy with seam vertices merged, largest components only
    geo = mesh.copy(); geo.visual = trimesh.visual.ColorVisuals()
    geo.merge_vertices(merge_tex=True, merge_norm=True)
    comps = sorted(geo.split(only_watertight=False), key=lambda c: len(c.faces), reverse=True)
    log(f"components after seam merge: {len(comps)} sizes {[len(c.faces) for c in comps[:4]]}")
    body = trimesh.util.concatenate(comps[:keep_components]) if keep_components > 1 else comps[0]
    scale = height_mm / body.extents[1]
    S = np.diag([scale / STUD_MM, scale / PLATE_MM, scale / STUD_MM, 1.0])
    T = np.eye(4); T[:3, 3] = -body.bounds[0]
    M = S @ T
    body.apply_transform(M)
    log(f"scaled to voxel units: extents {np.round(body.extents,1)} (studs, plates, studs)")
    t = time.time()
    vg = body.voxelized(pitch=1.0)
    vg = vg.fill()
    log(f"voxelized: {vg.filled_count} filled cells in {time.time()-t:.1f}s, matrix {vg.matrix.shape}")
    mat = np.asarray(vg.matrix, dtype=bool)
    idx = np.argwhere(mat)
    centers = vg.indices_to_points(idx)
    # colour lookup: nearest original vertex (original vertices transformed by the same M)
    verts = trimesh.transform_points(mesh.vertices, M)
    tree = cKDTree(verts)
    return mat, idx, centers, tree, vcol


def build_grid(mat, idx, centers, tree, vcol, th):
    nx, ny, nz = mat.shape
    grid = np.full(mat.shape, -1, dtype=np.int32)
    # surface cells: at least one empty 6-neighbour
    padded = np.pad(mat, 1)
    nb = np.zeros(mat.shape, dtype=np.int8)
    for dx, dy, dz in [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]:
        sl = padded[1+dx:1+dx+nx, 1+dy:1+dy+ny, 1+dz:1+dz+nz]
        nb += (~sl).astype(np.int8)
    surface = mat & (nb > 0)
    sidx = np.argwhere(surface)
    scent = centers[np.where(surface[tuple(idx.T)])[0]] if False else None
    # centres for surface cells: recompute from indices using the same mapping as centers
    # (centers correspond to idx rows) -> map idx -> centre
    cmap = {tuple(i): c for i, c in zip(map(tuple, idx), centers)}
    scent = np.array([cmap[tuple(i)] for i in sidx])
    _, vi = tree.query(scent, k=1)
    codes = classify(vcol[vi], th)
    grid[tuple(sidx.T)] = codes
    grid[tuple(np.argwhere(mat & ~surface).T)] = -2  # interior, coloured later
    return grid, surface, vcol[vi], sidx


def fill_interior(grid, surface, mode="nearest"):
    """hidden cells: nearest surface colour, or the dominant opaque colour (bigger bricks inside);
    cells nearest to a transparent surface always take that transparent colour so glass stays glass."""
    interior = grid == -2
    iidx = np.argwhere(interior); sidx = np.argwhere(surface)
    if len(iidx):
        stree = cKDTree(sidx.astype(np.float64))
        _, si = stree.query(iidx.astype(np.float64), k=1)
        near = grid[tuple(sidx[si].T)]
        if mode == "dominant":
            opaque = [c for c in grid[surface].tolist() if colour_info(c)[2] >= 1.0]
            dom = Counter(opaque).most_common(1)[0][0]
            trans = np.array([colour_info(int(c))[2] < 1.0 for c in near])
            near = np.where(trans, near, dom)
        grid[tuple(iidx.T)] = near
    return grid


def apply_region_rules(grid, surface, srgb, sidx, r):
    """Muse-style rules. The face is a rounded oval on the front of the head anchored on the detected eyes:
    white inside it, black where the texture is dark (eyes, mouth), pink on two cheek discs. The bubble is
    the blue region at chest height. Everything else is body colour."""
    nx, ny, nz = grid.shape
    codes = grid[tuple(sidx.T)]
    xs, ys, zs = sidx[:, 0], sidx[:, 1], sidx[:, 2]
    h, s, v = rgb_to_hsv(srgb.astype(np.float64))
    zc = zs.mean()
    blue_cells = sidx[codes == TBLUE]
    front_sign = 1.0 if (len(blue_cells) == 0 or blue_cells[:, 2].mean() > zc) else -1.0
    front = (zs - zc) * front_sign > r["front_min"] * nz
    dark_top = (v < r["eye_v"]) & (ys > 0.55 * ny) & front
    if dark_top.sum() >= 2:
        eye_y = int(np.median(ys[dark_top])); cx = float(xs[dark_top].mean())
    else:
        eye_y = int(r["eye_frac"] * ny); cx = (nx - 1) / 2
    ymid = eye_y + (r["face_above"] - r["face_below"]) / 2.0
    ry = (r["face_above"] + r["face_below"]) / 2.0
    n = r["face_exp"]
    dy = np.abs(ys - ymid) / ry
    inside_y = dy < 1
    half_w = np.zeros_like(dy)
    half_w[inside_y] = r["face_rx"] * (1 - dy[inside_y] ** n) ** (1.0 / n)
    face = front & inside_y & (np.abs(xs - cx) <= half_w)
    new = np.full(len(codes), TAN, dtype=np.int32)
    new[face] = WHITE
    # eyes: two symmetric black cells, one stud wide and eye_h plates tall; mouth: a short black line below
    cxi = int(round(cx))
    for sx in (-1, 1):
        ex = cxi + sx * int(round(r["eye_dx"]))
        eye = face & (xs == ex) & (ys >= eye_y - r["eye_h"] + 1) & (ys <= eye_y)
        new[eye] = BLACK
    mouth = face & (np.abs(xs - cx) <= r["mouth_w"] / 2.0) & (ys == eye_y - int(r["mouth_dy"]))
    new[mouth] = BLACK
    # cheeks: two small discs below and outside the eyes
    for sx in (-1, 1):
        ccx, ccy = cx + sx * r["cheek_dx"], eye_y - r["cheek_dy"]
        disc = face & (((xs - ccx) / r["cheek_rx"]) ** 2 + ((ys - ccy) / r["cheek_ry"]) ** 2 <= 1) & (new != BLACK)
        new[disc] = PINK
    # the bubble: blue texture at chest height
    bubble = (codes == TBLUE) & (ys >= r["bubble_lo"] * ny) & (ys <= r["bubble_hi"] * ny)
    new[bubble] = TBLUE
    grid[tuple(sidx.T)] = new
    log(f"face oval: eye level {eye_y}, centre x {cx:.1f}, {int(face.sum())} face cells, {int((new==BLACK).sum())} black, {int((new==PINK).sum())} pink, {int(bubble.sum())} bubble")
    return grid, eye_y


def smooth_colours(grid, surface, passes=1):
    """no-op kept for the CLI: the region rules already produce clean colour areas."""
    return grid


# ----------------------------------------------------------------------------- packing
class Part:
    __slots__ = ("id", "num", "x0", "z0", "w", "d", "y0", "h", "colour", "rot", "step", "section")
    def __init__(self, id, num, x0, z0, w, d, y0, h, colour, rot):
        self.id, self.num, self.x0, self.z0, self.w, self.d, self.y0, self.h, self.colour, self.rot = id, num, x0, z0, w, d, y0, h, colour, rot
        self.step = self.section = 0
    def cells(self):
        for x in range(self.x0, self.x0 + self.w):
            for z in range(self.z0, self.z0 + self.d):
                yield x, z
    def volume(self):
        return self.w * self.d * self.h


def candidate_sizes(kind, parity):
    table = BRICKS if kind == "brick" else PLATES
    cands = []
    for (L, s), num in table.items():
        cands.append((L, s, num, 0))          # long along X
        if L != s:
            cands.append((s, L, num, 1))      # long along Z (rotated)
    # larger area first; tie: preferred orientation for this layer parity
    cands.sort(key=lambda c: (-(c[0] * c[1]), 0 if (c[3] == parity) else 1))
    return cands


def find_bridges_layer(y, occ2d, colour, fmask, surf, taken, grid):
    """plates across every floating/grounded boundary in layer y. surface cells keep their colour,
    hidden interior cells may take the bridge colour. returns list of part specs (y,1,x0,z0,w,d,num,rot,col)."""
    nx, nz = occ2d.shape
    cands = [(L, s, num, 0) for (L, s), num in PLATES.items()] + [(s, L, num, 1) for (L, s), num in PLATES.items() if L != s]
    cands.sort(key=lambda c: -(c[0] * c[1]))
    pairs = []
    for x in range(nx):
        for z in range(nz):
            if not (occ2d[x, z] and fmask[x, z] and not taken[x, z]):
                continue
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                xb, zb = x + dx, z + dz
                if 0 <= xb < nx and 0 <= zb < nz and occ2d[xb, zb] and not fmask[xb, zb]:
                    pairs.append((x, z, xb, zb))
    out = []
    owner = {}
    for spec in pack_sheet.fixed:
        if spec[0] == y:
            for xx in range(spec[2], spec[2] + spec[4]):
                for zz in range(spec[3], spec[3] + spec[5]):
                    owner[(xx, zz)] = spec
    for (xa, za, xb, zb) in pairs:
        if taken[xa, za]:
            continue
        if taken[xb, zb]:
            old = owner.get((xb, zb))
            if old is None or old not in pack_sheet.fixed:
                continue
            # release the old bridge so a new one can cover both the floating cell and this boundary
            pack_sheet.fixed.remove(old)
            for xx in range(old[2], old[2] + old[4]):
                for zz in range(old[3], old[3] + old[5]):
                    taken[xx, zz] = False; owner.pop((xx, zz), None)
        col = int(colour[xa, za]) if surf[xa, za] else int(colour[xb, zb])
        best, best_score = None, -1e9
        for (w, d, num, rot) in cands:
            for x0 in range(max(0, max(xa, xb) - w + 1), min(xa, xb) + 1):
                for z0 in range(max(0, max(za, zb) - d + 1), min(za, zb) + 1):
                    if x0 + w > nx or z0 + d > nz:
                        continue
                    if not occ2d[x0:x0+w, z0:z0+d].all() or taken[x0:x0+w, z0:z0+d].any():
                        continue
                    cb = colour[x0:x0+w, z0:z0+d]; sb = surf[x0:x0+w, z0:z0+d]
                    if ((cb != col) & sb).any():
                        continue
                    fb = fmask[x0:x0+w, z0:z0+d]
                    side = min(int(fb.sum()), int((~fb).sum()))
                    score = w * d + 4 * side - 2 * int(((cb != col) & ~sb).sum())
                    if score > best_score:
                        best, best_score = (x0, z0, w, d, num, rot), score
        if best is None:
            continue
        x0, z0, w, d, num, rot = best
        taken[x0:x0+w, z0:z0+d] = True
        grid[x0:x0+w, y, z0:z0+d] = col
        spec = (y, 1, x0, z0, w, d, num, rot, col)
        out.append(spec)
        for xx in range(x0, x0 + w):
            for zz in range(z0, z0 + d):
                owner[(xx, zz)] = spec
    return out


def place_fixed(mask, colour, y0, h, pid3d, parts, placed, grid):
    n = 0
    for (yy, hh, x0, z0, w, d, num, rot, col) in pack_sheet.fixed:
        if yy != y0 or hh != h:
            continue
        if not mask[x0:x0+w, z0:z0+d].all() or (placed[x0:x0+w, z0:z0+d] >= 0).any():
            continue
        p = Part(len(parts), num, x0, z0, w, d, y0, h, col, rot)
        parts.append(p)
        placed[x0:x0+w, z0:z0+d] = p.id
        pid3d[x0:x0+w, y0:y0+h, z0:z0+d] = p.id
        n += 1
    return n


def pack_sheet(mask, colour, y0, h, pid3d, parts, parity, seam_w=0.35, fmask=None, surf=None, grid=None):
    nx, nz = mask.shape
    placed = np.full(mask.shape, -1, dtype=np.int64)
    below = pid3d[:, y0 - 1, :] if y0 > 0 else None
    cands = candidate_sizes("brick" if h == 3 else "plate", parity)
    if grid is not None:
        pack_sheet.kept += place_fixed(mask, colour, y0, h, pid3d, parts, placed, grid)
    for x in range(nx):
        for z in range(nz):
            if not mask[x, z] or placed[x, z] >= 0:
                continue
            col = colour[x, z]
            best, best_score = None, -1e9
            for (w, d, num, rot) in cands:
                if x + w > nx or z + d > nz:
                    continue
                blk_m = mask[x:x+w, z:z+d]
                if not blk_m.all():
                    continue
                if (placed[x:x+w, z:z+d] >= 0).any():
                    continue
                if (colour[x:x+w, z:z+d] != col).any():
                    continue
                score = w * d
                if below is not None and seam_w > 0:
                    b = below[x:x+w, z:z+d]
                    # seams of this part that coincide with seams below (weak line): count boundary edges
                    aligned = 0
                    if x > 0: aligned += int(np.sum(below[x-1, z:z+d] != b[0, :]))
                    if x + w < nx: aligned += int(np.sum(below[x+w, z:z+d] != b[-1, :]))
                    if z > 0: aligned += int(np.sum(below[x:x+w, z-1] != b[:, 0]))
                    if z + d < nz: aligned += int(np.sum(below[x:x+w, z+d] != b[:, -1]))
                    score -= seam_w * aligned
                if score > best_score:
                    best, best_score = (w, d, num, rot), score
            w, d, num, rot = best
            p = Part(len(parts), num, x, z, w, d, y0, h, int(col), rot)
            parts.append(p)
            placed[x:x+w, z:z+d] = p.id
            pid3d[x:x+w, y0:y0+h, z:z+d] = p.id
    return placed


def pack(grid, fmask3d=None, surface=None):
    nx, ny, nz = grid.shape
    occ = grid >= 0
    pack_sheet.bridges = 0; pack_sheet.kept = 0
    if not hasattr(pack_sheet, "fixed"):
        pack_sheet.fixed = []
    forced = np.zeros(grid.shape, dtype=bool)
    for (yy, hh, x0, z0, w, d, num, rot, col) in pack_sheet.fixed:
        forced[x0:x0+w, yy, z0:z0+d] = True
    if fmask3d is not None:
        for y in range(ny):
            if not fmask3d[:, y, :].any():
                continue
            taken = forced[:, y, :].copy()
            new = find_bridges_layer(y, occ[:, y, :], grid[:, y, :], fmask3d[:, y, :], surface[:, y, :], taken, grid)
            for spec in new:
                pack_sheet.fixed.append(spec)
            pack_sheet.bridges += len(new)
        forced[:] = False
        for (yy, hh, x0, z0, w, d, num, rot, col) in pack_sheet.fixed:
            forced[x0:x0+w, yy, z0:z0+d] = True
        occ = grid >= 0
    # choose the brick phase that covers the most cells with full-height bricks
    best_p, best_cnt = 0, -1
    for p in range(3):
        cnt = 0
        for y0 in range(p, ny - 2, 3):
            full = occ[:, y0] & occ[:, y0+1] & occ[:, y0+2] & (grid[:, y0] == grid[:, y0+1]) & (grid[:, y0] == grid[:, y0+2])
            cnt += int(full.sum())
        if cnt > best_cnt:
            best_p, best_cnt = p, cnt
    log(f"brick phase {best_p}: {best_cnt} cells brick-able of {int(occ.sum())}")
    # sheets ordered by y0
    sheets = []  # (y0, h, mask)
    covered = np.zeros_like(occ)
    for y0 in range(best_p, ny - 2, 3):
        full = occ[:, y0] & occ[:, y0+1] & occ[:, y0+2] & (grid[:, y0] == grid[:, y0+1]) & (grid[:, y0] == grid[:, y0+2])
        full &= ~(forced[:, y0] | forced[:, y0+1] | forced[:, y0+2])
        if full.any():
            sheets.append((y0, 3, full))
            for k in range(3):
                covered[:, y0+k] |= full
    for y in range(ny):
        rest = occ[:, y] & ~covered[:, y]
        if rest.any():
            sheets.append((y, 1, rest))
    sheets.sort(key=lambda s: (s[0], -s[1]))
    parts = []
    pid3d = np.full(grid.shape, -1, dtype=np.int64)
    for (y0, h, mask) in sheets:
        fm = None
        surf = surface[:, y0:y0+h, :].any(axis=1) if surface is not None else None
        if fmask3d is not None:
            fm = fmask3d[:, y0:y0+h, :].any(axis=1)
        pack_sheet(mask, grid[:, y0, :], y0, h, pid3d, parts, parity=(y0 // 3) % 2, fmask=fm, surf=surf, grid=grid)
    return parts, pid3d


# ----------------------------------------------------------------------------- checks
def connectivity(parts, pid3d):
    nx, ny, nz = pid3d.shape
    adj = defaultdict(set)
    for p in parts:
        ytop = p.y0 + p.h
        if ytop < ny:
            for x, z in p.cells():
                q = pid3d[x, ytop, z]
                if q >= 0:
                    adj[p.id].add(int(q)); adj[int(q)].add(p.id)
    ground = [p.id for p in parts if p.y0 == 0]
    seen = set(ground); dq = deque(ground)
    while dq:
        u = dq.popleft()
        for v in adj[u]:
            if v not in seen:
                seen.add(v); dq.append(v)
    floating = [p for p in parts if p.id not in seen]
    n_conn = sum(len(v) for v in adj.values()) // 2
    return floating, n_conn, adj


def repair_floating(grid, floating, max_gap=6):
    """drop support columns under floating parts down to the next occupied cell (same colour as the part)."""
    added = 0
    nx, ny, nz = grid.shape
    for p in floating:
        for x, z in p.cells():
            y = p.y0 - 1
            gap = 0
            while y >= 0 and grid[x, y, z] < 0 and gap < max_gap:
                y -= 1; gap += 1
            if y >= 0 and grid[x, y, z] >= 0 and gap > 0:
                grid[x, y+1:p.y0, z] = p.colour; added += gap
            elif y < 0 and gap > 0 and p.y0 <= max_gap:
                grid[x, 0:p.y0, z] = p.colour; added += p.y0
    return added


def centre_of_mass(parts, pid3d):
    m = 0.0; cx = cz = cy = 0.0
    for p in parts:
        v = p.volume()
        m += v; cx += v * (p.x0 + p.w / 2); cz += v * (p.z0 + p.d / 2); cy += v * (p.y0 + p.h / 2)
    cx, cz, cy = cx / m, cz / m, cy / m
    base = np.argwhere((pid3d[:, 0:3, :] >= 0).any(axis=1)).astype(np.float64) + 0.5
    inside, margin = False, 0.0
    if len(base) >= 3:
        hull = ConvexHull(base)
        tri = Delaunay(base[hull.vertices])
        inside = bool(tri.find_simplex(np.array([[cx, cz]])) >= 0)
        # distance to hull edges
        pts = base[hull.vertices]; dmin = 1e9
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            ab = b - a; t = np.clip(np.dot([cx, cz] - a, ab) / max(np.dot(ab, ab), 1e-9), 0, 1)
            dmin = min(dmin, float(np.linalg.norm([cx, cz] - (a + t * ab))))
        margin = dmin
    return dict(x_studs=cx, z_studs=cz, y_plates=cy, inside_footprint=inside, margin_studs=margin, base_cells=int(len(base)))


# ----------------------------------------------------------------------------- steps + export
def assign_steps(parts, per_step=8, section_plates=12):
    parts.sort(key=lambda p: (p.y0, p.h == 1, p.z0, p.x0))
    step = 0; last_key = None; n_in_step = 0
    for p in parts:
        key = (p.y0, p.h)
        if key != last_key or n_in_step >= per_step:
            step += 1; n_in_step = 0; last_key = key
        p.step = step; n_in_step += 1
        p.section = p.y0 // section_plates + 1
    return step


def rot_matrix(rot):
    return "1 0 0 0 1 0 0 0 1" if rot == 0 else "0 0 1 0 1 0 -1 0 0"


def write_ldraw(parts, path, name, author):
    lines = [f"0 FILE {name}.ldr", f"0 {name}, a LEGO brick model", f"0 Name: {name}.ldr", f"0 Author: {author}",
             "0 !LDRAW_ORG Unofficial_Model", "0 BFC CERTIFY CCW", ""]
    cur = None
    for p in parts:
        if cur is not None and p.step != cur:
            lines.append("0 STEP")
        cur = p.step
        cx = (p.x0 + p.w / 2) * LDU_STUD; cz = (p.z0 + p.d / 2) * LDU_STUD; y = -(p.y0 + p.h) * LDU_PLATE
        lines.append(f"1 {p.colour} {cx:g} {y:g} {cz:g} {rot_matrix(p.rot)} {p.num}.dat")
    lines.append("0 STEP"); lines.append("")
    open(path, "w").write("\n".join(lines))


def colour_info(code):
    return COLOUR_INFO.get(code) or PALETTE_INFO[code]


def write_lists(parts, outdir, name):
    cnt = Counter((p.num, p.colour) for p in parts)
    rows = sorted(cnt.items(), key=lambda kv: (kv[0][1], PART_NAMES.get(kv[0][0], kv[0][0])))
    with open(os.path.join(outdir, f"{name}_parts_list.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["part", "name", "ldraw_colour", "colour_name", "bricklink_colour_id", "qty"])
        for (num, col), n in rows:
            w.writerow([num, PART_NAMES.get(num, num), col, colour_info(col)[0], colour_info(col)[3], n])
    xml = ["<INVENTORY>"]
    for (num, col), n in rows:
        xml.append(f"  <ITEM><ITEMTYPE>P</ITEMTYPE><ITEMID>{num}</ITEMID><COLOR>{colour_info(col)[3]}</COLOR><MINQTY>{n}</MINQTY></ITEM>")
    xml.append("</INVENTORY>")
    open(os.path.join(outdir, f"{name}_bricklink_wanted.xml"), "w").write("\n".join(xml))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glb", required=True); ap.add_argument("--height-mm", type=float, default=270)
    ap.add_argument("--out", required=True); ap.add_argument("--name", default="model")
    ap.add_argument("--author", default="Hamudi Naanaa + Claude"); ap.add_argument("--components", type=int, default=1)
    ap.add_argument("--blue-s", type=float, default=0.18); ap.add_argument("--pink-s", type=float, default=0.16)
    ap.add_argument("--white-v", type=float, default=0.84); ap.add_argument("--white-s", type=float, default=0.16)
    ap.add_argument("--black-v", type=float, default=0.25); ap.add_argument("--smooth", type=int, default=0)
    ap.add_argument("--per-step", type=int, default=8)
    ap.add_argument("--colour-mode", choices=["muse", "palette"], default="muse")
    ap.add_argument("--palette-min-frac", type=float, default=0.01); ap.add_argument("--palette-passes", type=int, default=2)
    ap.add_argument("--ldconfig", default=""); ap.add_argument("--palette-contrast", type=float, default=32.0)
    ap.add_argument("--gain", type=float, default=1.0); ap.add_argument("--exclude", default="", help="LDraw colour codes to leave out, comma separated"); ap.add_argument("--interior", choices=["nearest", "dominant"], default="nearest")
    ap.add_argument("--eye-frac", type=float, default=0.72); ap.add_argument("--front-min", type=float, default=0.05)
    ap.add_argument("--face-below", type=int, default=16); ap.add_argument("--face-above", type=int, default=11)
    ap.add_argument("--face-rx", type=float, default=5.6); ap.add_argument("--face-exp", type=float, default=2.6)
    ap.add_argument("--eye-v", type=float, default=0.35)
    ap.add_argument("--cheek-dx", type=float, default=3.7); ap.add_argument("--cheek-dy", type=float, default=3.5)
    ap.add_argument("--cheek-rx", type=float, default=1.15); ap.add_argument("--cheek-ry", type=float, default=2.6)
    ap.add_argument("--eye-dx", type=float, default=2.0); ap.add_argument("--eye-h", type=int, default=3)
    ap.add_argument("--mouth-w", type=float, default=1.0); ap.add_argument("--mouth-dy", type=int, default=4)
    ap.add_argument("--bubble-lo", type=float, default=0.22); ap.add_argument("--bubble-hi", type=float, default=0.62)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    th = dict(blue_s=a.blue_s, pink_s=a.pink_s, white_v=a.white_v, white_s=a.white_s, black_v=a.black_v)
    t0 = time.time()
    mat, idx, centers, tree, vcol = load_and_voxelize(a.glb, a.height_mm, a.components)
    grid, surface, srgb, sidx = build_grid(mat, idx, centers, tree, vcol, th)
    load_ldconfig(a.ldconfig)
    eye_y = -1
    if a.colour_mode == "palette":
        excl = tuple(int(c) for c in a.exclude.split(",") if c.strip())
        grid, used = palette_colours(grid, surface, srgb, sidx, min_frac=a.palette_min_frac, passes=a.palette_passes, contrast_max=a.palette_contrast, gain=a.gain, exclude=excl)
        log("palette colours used:", {PALETTE_INFO[k][0]: int(v) for k, v in used.most_common()})
    else:
      log("surface colours raw:", {COLOUR_INFO[k][0]: int(v) for k, v in Counter(grid[surface].tolist()).items()})
      rules = dict(eye_frac=a.eye_frac, front_min=a.front_min, face_below=a.face_below, face_above=a.face_above,
                 face_rx=a.face_rx, face_exp=a.face_exp, eye_v=a.eye_v, cheek_dx=a.cheek_dx, cheek_dy=a.cheek_dy,
                 cheek_rx=a.cheek_rx, cheek_ry=a.cheek_ry, bubble_lo=a.bubble_lo, bubble_hi=a.bubble_hi,
                 eye_dx=a.eye_dx, eye_h=a.eye_h, mouth_w=a.mouth_w, mouth_dy=a.mouth_dy)
      grid, eye_y = apply_region_rules(grid, surface, srgb, sidx, rules)
      log(f"eye level plate {eye_y} of {grid.shape[1]}; surface colours after rules:", {COLOUR_INFO[k][0]: int(v) for k, v in Counter(grid[surface].tolist()).items()})
    grid = fill_interior(grid, surface, mode=a.interior)
    # trim empty layers at the bottom (fill() may leave none; be safe)
    occ_y = np.where((grid >= 0).any(axis=(0, 2)))[0]
    occ_x = np.where((grid >= 0).any(axis=(1, 2)))[0]; occ_z = np.where((grid >= 0).any(axis=(0, 1)))[0]
    sl = (slice(occ_x.min(), occ_x.max() + 1), slice(occ_y.min(), occ_y.max() + 1), slice(occ_z.min(), occ_z.max() + 1))
    grid = grid[sl]; surface = surface[sl]
    log(f"grid {grid.shape} (studs x plates x studs), {int((grid>=0).sum())} cells")
    fmask3d = None; bridges_total = 0
    pack_sheet.fixed = []
    best = None
    for it in range(14):
        parts, pid3d = pack(grid, fmask3d, surface)
        bridges_total += pack_sheet.bridges
        floating, n_conn, adj = connectivity(parts, pid3d)
        log(f"pack {it}: {len(parts)} parts, {n_conn} stud connections, {len(floating)} floating, {pack_sheet.bridges} new bridges, {pack_sheet.kept} kept")
        if best is None or len(floating) < len(best[2]):
            best = (parts, pid3d, floating, n_conn)
        if not floating:
            break
        fmask3d = np.zeros(grid.shape, dtype=bool)
        for p in floating:
            fmask3d[p.x0:p.x0+p.w, p.y0:p.y0+p.h, p.z0:p.z0+p.d] = True
        if it >= 3:
            added = repair_floating(grid, floating)
            log(f"  support columns: added {added} cells")
    parts, pid3d, floating, n_conn = best
    log(f"best packing: {len(parts)} parts, {len(floating)} floating")
    com = centre_of_mass(parts, pid3d)
    nsteps = assign_steps(parts, per_step=a.per_step)
    write_ldraw(parts, os.path.join(a.out, f"{a.name}.ldr"), a.name, a.author)
    rows = write_lists(parts, a.out, a.name)
    colours = Counter(p.colour for p in parts)
    nx, ny, nz = grid.shape
    report = dict(
        name=a.name, height_mm=a.height_mm, grid_studs=[int(nx), int(nz)], grid_plates=int(ny),
        size_mm=[round(nx * STUD_MM), round(ny * PLATE_MM), round(nz * STUD_MM)],
        cells=int((grid >= 0).sum()), parts=len(parts), part_colour_lines=len(rows), steps=nsteps,
        sections=max(p.section for p in parts), stud_connections=n_conn, floating_after_repair=len(floating),
        colours={colour_info(k)[0]: v for k, v in colours.most_common()},
        bricks=sum(1 for p in parts if p.h == 3), plates=sum(1 for p in parts if p.h == 1),
        centre_of_mass=com, bridges=bridges_total, eye_level_plate=eye_y, seconds=round(time.time() - t0, 1),
    )
    json.dump(report, open(os.path.join(a.out, f"{a.name}_report.json"), "w"), indent=2)
    model = dict(
        name=a.name, colours={str(k): dict(name=v[0], hex=v[1], alpha=v[2]) for k, v in list(PALETTE_INFO.items()) + list(COLOUR_INFO.items())},
        grid=[int(nx), int(ny), int(nz)], stud_ldu=LDU_STUD, plate_ldu=LDU_PLATE,
        parts=[dict(id=p.id, p=p.num, c=p.colour, x0=p.x0, z0=p.z0, w=p.w, d=p.d, y0=p.y0, h=p.h, rot=p.rot, step=p.step, section=p.section) for p in parts],
    )
    json.dump(model, open(os.path.join(a.out, f"{a.name}_model.json"), "w"))
    log(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
