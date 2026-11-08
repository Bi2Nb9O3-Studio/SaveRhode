#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为 BeyondHome 的 Lua 生成更精细的树苗散点模型与贴图。

设定依据（BeyondHome 本地化文本）：
  * LuaSapling / "Tree Analysis"：
    "Due to terraforming, saplings have been able to grow on Lua. They are common
     in lowlands, but they are endangered because of the halted progress with the
     terraforming."
    => 幼树形态：细高、分枝少、树冠稀疏下垂；集中在低地、密度低。
  * Lua 本体：部分地球化（约 30%），大气极稀薄，地表为暗草地/岩石，
    因此树皮偏灰白并带苔藓，叶片为耐弱光的深青绿。

输出：
  GameData/SaveRhode/Models/LuaSapling/     saplingTrunkLOD{0,1,2}.obj/mtl
                                            saplingCanopyLOD{0,1}.obj
  GameData/SaveRhode/Textures/LuaSapling/   saplingBark/nrm、saplingLeaves/nrm、
                                            saplingBillboard/nrm（DDS + mipmap）
  tools/preview/lua_sapling_preview.png     三视图预览

用法：
    python tools/make_lua_sapling.py
"""

from __future__ import annotations

import math
import os
import random
import struct
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ----------------------------------------------------------------------
# 全局参数
# ----------------------------------------------------------------------

SEED = 20261107
TREE_HEIGHT = 2.85          # 米，树苗尺度
TRUNK_BASE_R = 0.062
TRUNK_TIP_R = 0.0065

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "GameData" / "SaveRhode" / "Models" / "LuaSapling"
TEX_DIR = ROOT / "GameData" / "SaveRhode" / "Textures" / "LuaSapling"
PREVIEW_DIR = ROOT / "tools" / "preview"

BARK_COLOR = np.array([0.60, 0.56, 0.47], np.float32)
LEAF_COLOR = np.array([0.30, 0.44, 0.33], np.float32)

# ----------------------------------------------------------------------
# 基础几何工具
# ----------------------------------------------------------------------


def normalize(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else v


def frame(tangent, prev_right=None):
    """给定轴向，返回与之垂直的 (right, up)，用平行传输避免扭曲。"""
    t = normalize(tangent)
    if prev_right is None:
        ref = np.array([0.0, 1.0, 0.0]) if abs(t[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
        right = normalize(np.cross(ref, t))
    else:
        right = prev_right - t * np.dot(prev_right, t)
        if np.linalg.norm(right) < 1e-6:
            ref = np.array([0.0, 1.0, 0.0]) if abs(t[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
            right = normalize(np.cross(ref, t))
        else:
            right = normalize(right)
    up = normalize(np.cross(t, right))
    return right, up


class Mesh:
    def __init__(self):
        self.verts = []
        self.uvs = []
        self.norms = []
        self.faces = []

    def add(self, verts, uvs, norms, faces):
        base = len(self.verts)
        self.verts.extend(verts)
        self.uvs.extend(uvs)
        self.norms.extend(norms)
        self.faces.extend([(a + base, b + base, c + base) for a, b, c in faces])

    def bounds(self):
        v = np.asarray(self.verts)
        return v.min(axis=0), v.max(axis=0)

    def tri_count(self):
        return len(self.faces)


def tube(points, radii, sides, uv_scale=0.38, cap_end=True, cap_start=False,
         wobble=0.0, phase=0.0):
    """沿折线生成管状网格，返回 (verts, uvs, norms, faces)。"""
    n = len(points)
    verts, uvs, norms, faces = [], [], [], []
    right = None
    acc = 0.0
    ring_index = []
    for i in range(n):
        p = points[i]
        if i == 0:
            t = points[1] - points[0]
        elif i == n - 1:
            t = points[-1] - points[-2]
        else:
            t = points[i + 1] - points[i - 1]
        t = normalize(t)
        right, up = frame(t, right)

        for k in range(sides):
            a = 2.0 * math.pi * k / sides
            r = radii[i]
            if wobble:
                r *= 1.0 + wobble * 0.5 * (
                    math.sin(a * 3.0 + i * 1.7 + phase)
                    + 0.5 * math.sin(a * 5.0 - i * 2.1 + phase)
                )
            off = math.cos(a) * right + math.sin(a) * up
            verts.append(p + off * r)
            norms.append(normalize(off))
            uvs.append((k / sides, 0.0))

        if i > 0:
            acc += float(np.linalg.norm(points[i] - points[i - 1]))
        ring_index.append(acc / uv_scale)

    for i in range(n):
        for k in range(sides):
            uvs[i * sides + k] = (k / sides, ring_index[i])

    for i in range(n - 1):
        for k in range(sides):
            a = i * sides + k
            b = i * sides + (k + 1) % sides
            c = (i + 1) * sides + (k + 1) % sides
            d = (i + 1) * sides + k
            faces.append((a, b, c))
            faces.append((a, c, d))

    if cap_start:
        center = len(verts)
        verts.append(points[0])
        norms.append(normalize(points[0] - points[1]))
        uvs.append((0.5, 0.0))
        for k in range(sides):
            a = k
            b = (k + 1) % sides
            faces.append((center, b, a))

    if cap_end:
        center = len(verts)
        verts.append(points[-1])
        norms.append(normalize(points[-1] - points[-2]))
        uvs.append((0.5, ring_index[-1]))
        base = (n - 1) * sides
        for k in range(sides):
            a = base + k
            b = base + (k + 1) % sides
            faces.append((center, a, b))

    return verts, uvs, norms, faces


def orient_faces(verts, faces, norms):
    """保证三角形法线与顶点法线同向（外翻）。"""
    v = np.asarray(verts)
    n = np.asarray(norms)
    fixed = []
    for (a, b, c) in faces:
        fn = np.cross(v[b] - v[a], v[c] - v[a])
        avg = n[a] + n[b] + n[c]
        fixed.append((a, c, b) if np.dot(fn, avg) < 0 else (a, b, c))
    return fixed


# ----------------------------------------------------------------------
# 树骨架
# ----------------------------------------------------------------------


def build_skeleton(rng):
    """返回 (trunk_points, trunk_radii, branches, clusters)。"""
    height = TREE_HEIGHT * rng.uniform(0.92, 1.08)

    # 主干：轻微弯曲、向上收细
    n = 16
    trunk = []
    lean = rng.uniform(0.05, 0.13)
    lean_dir = rng.uniform(0, 2 * math.pi)
    for i in range(n):
        t = i / (n - 1)
        x = lean * math.cos(lean_dir) * t ** 1.6 + 0.035 * math.sin(t * 2.4) * t
        z = lean * math.sin(lean_dir) * t ** 1.6 + 0.028 * math.sin(t * 3.1 + 1.3) * t
        trunk.append(np.array([x, height * t, z], np.float64))

    radii = [TRUNK_BASE_R * (1.0 - (i / (n - 1)) ** 0.92) ** 1.35 + TRUNK_TIP_R
             for i in range(n)]
    radii = [r * rng.uniform(0.94, 1.06) for r in radii]

    branches = []
    clusters = []

    def radius_at(t):
        return TRUNK_BASE_R * (1.0 - t) ** 1.4 + TRUNK_TIP_R

    n_branch = rng.randint(7, 9)
    for b in range(n_branch):
        t = 0.30 + 0.60 * (b + rng.random()) / n_branch
        idx = min(n - 2, int(t * (n - 1)))
        base = trunk[idx]
        az = rng.uniform(0, 2 * math.pi) + b * 2.39996  # 黄金角分布
        elev = math.radians(rng.uniform(34, 63))
        length = height * (0.36 + 0.16 * rng.random()) * (1.15 - 0.75 * t)

        d = np.array([math.sin(elev) * math.cos(az),
                      math.cos(elev),
                      math.sin(elev) * math.sin(az)])
        d = normalize(d)

        seg = 7
        side = normalize(np.cross(d, np.array([0.0, 1.0, 0.0])))
        if np.linalg.norm(side) < 1e-6:
            side = np.array([1.0, 0.0, 0.0])
        bend = rng.uniform(-0.10, 0.10) * length
        pts = []
        for s in range(seg):
            u = s / (seg - 1)
            p = base + d * (length * u)
            p = p + side * (bend * math.sin(u * 2.6))                # 侧向弯曲
            p = p + np.array([0.0, 0.22 * length * u * u, 0.0])      # 向上翘
            pts.append(p)
        r0 = radius_at(t) * 0.62
        branches.append({"pts": pts, "r0": r0, "tip": 0.0045, "level": 0})

        tips = [pts[-1]]
        # 二级枝
        for s in range(rng.randint(3, 4)):
            u = rng.uniform(0.40, 0.80)
            si = int(u * (seg - 1))
            pb = pts[si]
            d2 = normalize(np.array([
                d[0] + rng.uniform(-0.7, 0.7),
                rng.uniform(-0.15, 0.55),
                d[2] + rng.uniform(-0.7, 0.7),
            ]))
            l2 = length * rng.uniform(0.30, 0.45)
            p2 = []
            for k in range(5):
                v = k / 4
                q = pb + d2 * (l2 * v) + np.array([0.0, 0.18 * l2 * v * v, 0.0])
                p2.append(q)
            branches.append({"pts": p2, "r0": r0 * 0.55, "tip": 0.003, "level": 1})
            tips.append(p2[-1])

        for tip in tips:
            clusters.append({"p": tip + np.array([0.0, -0.02, 0.0]),
                             "size": rng.uniform(0.28, 0.44)})
        # 主枝中段也挂一簇，避免树冠只集中在末梢
        clusters.append({"p": pts[int(seg * 0.6)] + np.array([0.0, -0.03, 0.0]),
                         "size": rng.uniform(0.24, 0.34)})

    # 树顶比枝尖更细
    return trunk, radii, branches, clusters, height


# ----------------------------------------------------------------------
# 网格生成（不同 LOD）
# ----------------------------------------------------------------------


def build_tree_mesh(detail=0):
    """detail: 0 = LOD0，1 = LOD1。返回 (trunk_mesh, leaf_mesh, height)。"""
    rng = random.Random(SEED + detail)
    trunk, radii, branches, clusters, height = build_skeleton(rng)

    sides = 16 if detail == 0 else 8
    seg = 18 if detail == 0 else 9
    bseg = 8 if detail == 0 else 4
    cards = 7 if detail == 0 else 3

    t_mesh = Mesh()
    # 主干重采样
    src = np.asarray(trunk)
    idx = np.linspace(0, len(src) - 1, seg).astype(int)
    pts = [src[i] for i in idx]
    rad = [radii[i] for i in idx]
    v, u, nrm, f = tube(pts, rad, sides, uv_scale=0.42, wobble=0.06, phase=0.7)
    t_mesh.add(v, u, nrm, orient_faces(v, f, nrm))

    for br in branches:
        p = np.asarray(br["pts"])
        src_len = len(p)
        idx = np.linspace(0, src_len - 1, bseg).astype(int)
        pts = [p[i] for i in idx]
        rad = [br["r0"] * (1.0 - (i / (bseg - 1)) ** 0.9) * 0.92 + br["tip"]
               for i in range(bseg)]
        v, u, nrm, f = tube(pts, rad, max(4, sides - 3), uv_scale=0.32,
                            cap_end=True, wobble=0.05, phase=br["r0"] * 50)
        t_mesh.add(v, u, nrm, orient_faces(v, f, nrm))

    # 叶子（十字片）
    l_mesh = Mesh()
    for cluster in clusters:
        size = cluster["size"]
        for c in range(cards):
            yaw = rng.uniform(0, 2 * math.pi)
            pitch = rng.uniform(-0.28, 0.34)
            nd = np.array([math.cos(yaw) * math.cos(pitch),
                           math.sin(pitch),
                           math.sin(yaw) * math.cos(pitch)])
            nd = normalize(nd)
            up = normalize(np.array([0.0, 1.0, 0.0]) - nd * nd[1] * 0.85)
            right = normalize(np.cross(up, nd))
            w = size * rng.uniform(0.85, 1.15)
            h = w * rng.uniform(0.8, 1.0)
            center = cluster["p"] + right * rng.uniform(-0.06, 0.06) \
                + np.array([0.0, rng.uniform(-0.08, 0.05), 0.0])
            corners = [center - right * w / 2 - up * h / 2,
                       center + right * w / 2 - up * h / 2,
                       center + right * w / 2 + up * h / 2,
                       center - right * w / 2 + up * h / 2]
            base = len(l_mesh.verts)
            for i, p in enumerate(corners):
                l_mesh.verts.append(p)
                l_mesh.uvs.append([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)][i])
                l_mesh.norms.append(nd)
            l_mesh.faces.append((base, base + 1, base + 2))
            l_mesh.faces.append((base, base + 2, base + 3))

    # 让叶子法线朝向相机一侧，双面渲染由材质 TWO_SIDED 处理
    l_mesh.faces = orient_faces(l_mesh.verts, l_mesh.faces, l_mesh.norms)
    return t_mesh, l_mesh, height


def build_billboard(height):
    """LOD2：面向 +Z 的单片四边形，pivot 在根部。"""
    v0, v1 = np.asarray([-0.42, 0.0, 0.0]), np.asarray([0.42, 0.0, 0.0])
    v2, v3 = np.asarray([0.42, height * 1.02, 0.0]), np.asarray([-0.42, height * 1.02, 0.0])
    mesh = Mesh()
    n = np.array([0.0, 0.0, 1.0])
    mesh.verts = [v0, v1, v2, v3]
    mesh.uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    mesh.norms = [n, n, n, n]
    mesh.faces = [(0, 1, 2), (0, 2, 3)]
    return mesh


# ----------------------------------------------------------------------
# OBJ 输出
# ----------------------------------------------------------------------


def write_obj(path, name, material, mesh, mtl=None):
    lines = []
    if mtl:
        lines.append("mtllib {}".format(mtl))
    lines.append("o {}".format(name))
    lines.append("usemtl {}".format(material))
    for v in mesh.verts:
        lines.append("v {:.6f} {:.6f} {:.6f}".format(*v))
    for uv in mesh.uvs:
        lines.append("vt {:.6f} {:.6f}".format(*uv))
    for n in mesh.norms:
        lines.append("vn {:.6f} {:.6f} {:.6f}".format(*n))
    for (a, b, c) in mesh.faces:
        lines.append("f {0}/{0}/{0} {1}/{1}/{1} {2}/{2}/{2}".format(a + 1, b + 1, c + 1))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(mesh.verts), len(mesh.faces)


def write_mtl(path, materials):
    lines = []
    for name, tex, normal in materials:
        lines += [
            "newmtl {}".format(name),
            "Ka 1 1 1",
            "Kd 1 1 1",
            "Ks 0 0 0",
            "d 1",
            "illum 2",
        ]
        if tex:
            lines.append("map_Kd {}".format(tex))
        if normal:
            lines.append("map_Bump {}".format(normal))
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------
# 贴图
# ----------------------------------------------------------------------


def tile_noise(rng, size, freq, octaves=4, persistence=0.5):
    """可无缝平铺的值噪声。"""
    out = np.zeros((size, size), np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        f = max(2, freq * (2 ** o))
        base = rng.random((f, f)).astype(np.float32)
        big = np.tile(base, (3, 3))
        img = Image.fromarray((big * 255).astype(np.uint8), "L")
        img = img.resize((size * 3, size * 3), Image.BICUBIC)
        arr = np.asarray(img, np.float32) / 255.0
        arr = arr[size:2 * size, size:2 * size]
        out += amp * arr
        total += amp
        amp *= persistence
    return out / total


def smoothstep(edge0, edge1, x):
    t = np.clip((x - edge0) / (edge1 - edge0 + 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


def normal_from_height(height, strength=2.5):
    gy, gx = np.gradient(height.astype(np.float32))
    nx = -gx * strength
    ny = -gy * strength
    nz = np.ones_like(height)
    length = np.sqrt(nx * nx + ny * ny + nz * nz)
    rgb = np.stack([nx / length, ny / length, nz / length], axis=-1)
    return ((rgb * 0.5 + 0.5) * 255).astype(np.uint8)


def make_bark(size=512):
    rng = np.random.default_rng(SEED)
    n1 = tile_noise(rng, size, 6, 5)
    n2 = tile_noise(rng, size, 24, 4)
    n3 = tile_noise(rng, size, 64, 3)

    x = np.linspace(0, 1, size, endpoint=False)[None, :]
    streak = 0.5 + 0.5 * np.sin(2 * np.pi * (x * 9 + 0.45 * n1))
    crack = (1.0 - streak) ** 5
    crack = smoothstep(0.25, 0.95, crack)

    col = BARK_COLOR[None, None, :] * (0.68 + 0.55 * n2[..., None])
    col *= (1.0 - 0.52 * crack[..., None])

    lichen = smoothstep(0.62, 0.86, n1) * smoothstep(0.35, 0.7, n3)
    lichen_color = np.array([0.36, 0.46, 0.33], np.float32)
    col = col * (1 - 0.55 * lichen[..., None]) + lichen_color[None, None, :] * (0.55 * lichen[..., None])
    col = np.clip(col, 0, 1)

    height = 0.55 - 0.42 * crack + 0.22 * n3
    return (col * 255).astype(np.uint8), normal_from_height(height, 3.2)


def make_leaves(size=1024):
    rng = random.Random(SEED + 7)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy = size / 2, size / 2
    count = 82
    for i in range(count):
        ang = rng.uniform(0, 2 * math.pi)
        rad = abs(rng.gauss(0, 0.30)) * size
        px = cx + math.cos(ang) * rad * 0.9
        py = cy + math.sin(ang) * rad * 0.9
        if math.hypot(px - cx, py - cy) > size * 0.46:
            continue
        w = rng.uniform(size * 0.10, size * 0.19)
        h = w * rng.uniform(0.42, 0.62)
        rot = rng.uniform(0, 2 * math.pi)
        tint = rng.uniform(0.78, 1.22)
        base = np.array([58, 92, 66], np.float32) * tint
        color = tuple(int(min(255, c)) for c in base) + (255,)

        pts = []
        steps = 14
        for k in range(steps):
            t = 2 * math.pi * k / steps
            # 叶形：两端尖、中间宽
            rx = w / 2 * math.cos(t)
            ry = h / 2 * math.sin(t) * (0.55 + 0.45 * abs(math.cos(t)))
            x = px + rx * math.cos(rot) - ry * math.sin(rot)
            y = py + rx * math.sin(rot) + ry * math.cos(rot)
            pts.append((x, y))
        draw.polygon(pts, fill=color)

        # 主叶脉
        vx = math.cos(rot) * w * 0.42
        vy = math.sin(rot) * w * 0.42
        draw.line([(px - vx, py - vy), (px + vx, py + vy)],
                  fill=(int(base[0] * 0.72), int(base[1] * 0.72), int(base[2] * 0.72), 255),
                  width=max(1, size // 340))

    img = img.filter(ImageFilter.GaussianBlur(0.6))
    arr = np.asarray(img, np.float32) / 255.0
    alpha = arr[..., 3]
    height = Image.fromarray((alpha * 255).astype(np.uint8), "L")
    height = height.filter(ImageFilter.GaussianBlur(size / 180))
    height_arr = np.asarray(height, np.float32) / 255.0
    normal = normal_from_height(height_arr, 1.1)
    return np.asarray(img, np.uint8), normal


# ----------------------------------------------------------------------
# 简易光栅化（预览 / 公告板）
# ----------------------------------------------------------------------


def rasterize(parts, size, yaw_deg=0.0, alpha_cut=0.3, bg=(0.05, 0.06, 0.07)):
    """parts: [(mesh, texture_rgba or None, base_color)]，正交投影。"""
    yaw = math.radians(yaw_deg)
    rot = np.array([[math.cos(yaw), 0, -math.sin(yaw)],
                    [0, 1, 0],
                    [math.sin(yaw), 0, math.cos(yaw)]], np.float32)

    all_v = []
    for mesh, _, _ in parts:
        all_v.append(np.asarray(mesh.verts, np.float32) @ rot.T)
    all_v = np.concatenate(all_v)
    lo = all_v.min(axis=0)
    hi = all_v.max(axis=0)
    span = max(hi[0] - lo[0], hi[1] - lo[1]) * 1.25
    scale = (size * 0.92) / span
    ox = size / 2 - (lo[0] + hi[0]) / 2 * scale
    oy = size * 0.96 - lo[1] * scale

    color = np.zeros((size, size, 3), np.float32)
    color[:] = bg
    alpha = np.ones((size, size), np.float32)
    zbuf = np.full((size, size), -1e9, np.float32)

    light = normalize(np.array([-0.45, 0.75, 0.55], np.float32))

    for mesh, tex, base_color in parts:
        v = np.asarray(mesh.verts, np.float32) @ rot.T
        vn = np.asarray(mesh.norms, np.float32) @ rot.T
        uvs = np.asarray(mesh.uvs, np.float32)
        tex_arr = np.asarray(tex, np.float32) / 255.0 if tex is not None else None
        th, tw = tex_arr.shape[:2] if tex_arr is not None else (1, 1)

        for (a, b, c) in mesh.faces:
            p = np.stack([v[a], v[b], v[c]])
            uv = np.stack([uvs[a], uvs[b], uvs[c]])
            sx = p[:, 0] * scale + ox
            sy = oy - p[:, 1] * scale
            sz = p[:, 2]

            minx = int(max(0, math.floor(sx.min())))
            maxx = int(min(size - 1, math.ceil(sx.max())))
            miny = int(max(0, math.floor(sy.min())))
            maxy = int(min(size - 1, math.ceil(sy.max())))
            if minx > maxx or miny > maxy:
                continue

            xs = np.arange(minx, maxx + 1) + 0.5
            ys = np.arange(miny, maxy + 1) + 0.5
            gx, gy = np.meshgrid(xs, ys)

            d = (sy[1] - sy[2]) * (sx[0] - sx[2]) + (sx[2] - sx[1]) * (sy[0] - sy[2])
            if abs(d) < 1e-9:
                continue
            w0 = ((sy[1] - sy[2]) * (gx - sx[2]) + (sx[2] - sx[1]) * (gy - sy[2])) / d
            w1 = ((sy[2] - sy[0]) * (gx - sx[2]) + (sx[0] - sx[2]) * (gy - sy[2])) / d
            w2 = 1.0 - w0 - w1
            inside = (w0 >= -1e-5) & (w1 >= -1e-5) & (w2 >= -1e-5)
            if not inside.any():
                continue

            depth = w0 * sz[0] + w1 * sz[1] + w2 * sz[2]
            sub = zbuf[miny:maxy + 1, minx:maxx + 1]
            mask = inside & (depth > sub)
            if not mask.any():
                continue

            fn = np.cross(p[1] - p[0], p[2] - p[0])
            if np.linalg.norm(fn) < 1e-12:
                continue
            fn = normalize(fn)
            shade = 0.35 + 0.75 * abs(float(np.dot(fn, light)))

            u = w0 * uv[0, 0] + w1 * uv[1, 0] + w2 * uv[2, 0]
            vv = w0 * uv[0, 1] + w1 * uv[1, 1] + w2 * uv[2, 1]

            if tex_arr is not None:
                tx = np.clip((u[mask] % 1.0) * (tw - 1), 0, tw - 1).astype(int)
                ty = np.clip(((1.0 - vv[mask]) % 1.0) * (th - 1), 0, th - 1).astype(int)
                rgba = tex_arr[ty, tx]
                if tex_arr.shape[2] == 4:
                    keep = rgba[:, 3] > alpha_cut
                    if not keep.any():
                        continue
                    rgb = rgba[:, :3] * shade * base_color[None, :]
                    patch_col = np.zeros((mask.sum(), 3), np.float32)
                    patch_col[:] = color[miny:maxy + 1, minx:maxx + 1][mask]
                    patch_col[keep] = rgb[keep]
                    color[miny:maxy + 1, minx:maxx + 1][mask] = patch_col
                    patch_a = alpha[miny:maxy + 1, minx:maxx + 1][mask]
                    patch_a[keep] = 1.0
                    alpha[miny:maxy + 1, minx:maxx + 1][mask] = patch_a
                else:
                    color[miny:maxy + 1, minx:maxx + 1][mask] = rgba[:, :3] * shade * base_color[None, :]
                    alpha[miny:maxy + 1, minx:maxx + 1][mask] = 1.0
            else:
                color[miny:maxy + 1, minx:maxx + 1][mask] = shade * base_color[None, :]
                alpha[miny:maxy + 1, minx:maxx + 1][mask] = 1.0

            zbuf[miny:maxy + 1, minx:maxx + 1][mask] = depth[mask]

    rgba = np.concatenate([np.clip(color, 0, 1) * 255, (alpha * 255)[..., None]], axis=2)
    return Image.fromarray(rgba.astype(np.uint8), "RGBA")


# ----------------------------------------------------------------------
# DDS 写入（DXT5，带 mipmap）
# ----------------------------------------------------------------------


def _dds_header(w, h, mip_count, fourcc, payload_size):
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000
    caps = 0x1000 | 0x8 | 0x400000
    block_size = 8 if fourcc == b"DXT1" else 16
    header = struct.pack("<I", 124) + struct.pack("<I", flags)
    header += struct.pack("<I", h) + struct.pack("<I", w)
    header += struct.pack("<I", max(1, w // 4) * max(1, h // 4) * block_size)
    header += struct.pack("<I", 0) + struct.pack("<I", mip_count)
    header += b"\x00" * 44
    header += struct.pack("<I", 32) + struct.pack("<I", 0x4)
    header += fourcc + b"\x00" * 20
    header += struct.pack("<I", caps) + struct.pack("<I", 0)
    header += struct.pack("<I", 0) + struct.pack("<I", 0) + struct.pack("<I", 0)
    assert len(header) == 124, len(header)
    return b"DDS " + header


def save_dds(img, path, fmt="DXT5"):
    """把 RGBA 图按 DXT5 逐级压缩并拼成带 mipmap 的 DDS。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = img.size
    mips = [img]
    while w > 4 or h > 4:
        w, h = max(1, w // 2), max(1, h // 2)
        mips.append(mips[-1].resize((w, h), Image.LANCZOS))

    payload = b""
    with tempfile.TemporaryDirectory() as tmp:
        for i, mip in enumerate(mips):
            tmp_path = os.path.join(tmp, "m{}.dds".format(i))
            mip.convert("RGBA").save(tmp_path, format="DDS", pixel_format=fmt)
            with open(tmp_path, "rb") as f:
                data = f.read()
            payload += data[128:]

    header = _dds_header(img.size[0], img.size[1], len(mips),
                         b"DXT1" if fmt == "DXT1" else b"DXT5", len(payload))
    path.write_bytes(header + payload)
    return path


def save_dds_normal(rgb, path):
    """法线图同样用 DXT5，避免 DXT1 的色块影响法线质量。"""
    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    return save_dds(img, path, fmt="DXT5")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def main():
    for d in (MODEL_DIR, TEX_DIR, PREVIEW_DIR):
        d.mkdir(parents=True, exist_ok=True)

    print("生成骨架与网格…")
    t0, l0, height = build_tree_mesh(detail=0)
    t1, l1, _ = build_tree_mesh(detail=1)
    bb = build_billboard(height)

    print("  LOD0  树干 {:,} 面 / {:,} 顶点".format(t0.tri_count(), len(t0.verts)))
    print("  LOD0  叶片 {:,} 面".format(l0.tri_count()))
    print("  LOD1  树干 {:,} 面".format(t1.tri_count()))
    print("  LOD1  叶片 {:,} 面".format(l1.tri_count()))

    write_mtl(MODEL_DIR / "saplingTrunkLOD0.mtl",
              [("saplingBark", "SaveRhode/Textures/LuaSapling/saplingBark.dds",
                "SaveRhode/Textures/LuaSapling/saplingBarknrm.dds")])
    write_obj(MODEL_DIR / "saplingTrunkLOD0.obj", "LuaSaplingTrunkLOD0", "saplingBark",
              t0, mtl="saplingTrunkLOD0.mtl")
    write_obj(MODEL_DIR / "saplingTrunkLOD1.obj", "LuaSaplingTrunkLOD1", "saplingBark", t1)
    write_obj(MODEL_DIR / "saplingTrunkLOD2.obj", "LuaSaplingTrunkLOD2", "saplingBillboard", bb)
    write_obj(MODEL_DIR / "saplingCanopyLOD0.obj", "LuaSaplingCanopyLOD0", "saplingLeaves", l0)
    write_obj(MODEL_DIR / "saplingCanopyLOD1.obj", "LuaSaplingCanopyLOD1", "saplingLeaves", l1)

    print("生成贴图…")
    bark, bark_nrm = make_bark(512)
    leaves, leaves_nrm = make_leaves(1024)

    print("渲染公告板与预览…")
    billboard = rasterize([(t1, None, BARK_COLOR.astype(np.float32)),
                           (l1, leaves, np.ones(3, np.float32))], 512, alpha_cut=0.25)
    bb_rgb = billboard.convert("RGB")
    bb_nrm = normal_from_height(
        np.asarray(billboard.split()[3], np.float32) / 255.0, 0.6)
    # 公告板三角形在 0..1 UV 上采样，转成图像坐标
    save_dds(billboard, TEX_DIR / "saplingBillboard.dds")
    save_dds_normal(bb_nrm, TEX_DIR / "saplingBillboardnrm.dds")

    save_dds(Image.fromarray(bark, "RGB").convert("RGBA"), TEX_DIR / "saplingBark.dds")
    save_dds_normal(bark_nrm, TEX_DIR / "saplingBarknrm.dds")
    save_dds(Image.fromarray(leaves, "RGBA"), TEX_DIR / "saplingLeaves.dds")
    save_dds_normal(leaves_nrm, TEX_DIR / "saplingLeavesnrm.dds")

    # 三视图预览
    views = []
    for yaw in (0, 40, 90):
        views.append(rasterize(
            [(t0, None, BARK_COLOR.astype(np.float32)),
             (l0, leaves, np.ones(3, np.float32))], 420, yaw_deg=yaw, alpha_cut=0.25))
    sheet = Image.new("RGBA", (420 * 3, 420), (16, 18, 20, 255))
    for i, v in enumerate(views):
        sheet.alpha_composite(v, (i * 420, 0))
    sheet.save(PREVIEW_DIR / "lua_sapling_preview.png")

    print("完成：")
    for f in sorted(MODEL_DIR.glob("*.obj")) + sorted(TEX_DIR.glob("*.dds")):
        print("  {}  ({:,} 字节)".format(f.relative_to(ROOT), f.stat().st_size))
    print("  预览: {}".format((PREVIEW_DIR / "lua_sapling_preview.png").relative_to(ROOT)))


if __name__ == "__main__":
    main()
