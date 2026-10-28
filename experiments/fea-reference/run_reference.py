#!/usr/bin/env python3
"""Prepare reproducible CalculiX input decks for the OCRE axial-bar reference.

Only Python's standard library is required. The structured mesh is emitted
directly as CalculiX C3D8 elements so preparation remains possible without
Gmsh. A solver result is never inferred from the analytical reference values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
L, B, H, E, NU, DELTA = 100.0, 10.0, 10.0, 210000.0, 0.3, 0.01
SIZES = (5.0, 2.5, 1.25)


def gmsh_geo(size: float) -> str:
    """Build the second-order hex-dominant box and named end surfaces."""
    return f'''SetFactory("OpenCASCADE");
Mesh.Algorithm3D = 1;
Mesh.ElementOrder = 2;
Mesh.SaveGroupsOfNodes = 1;
Mesh.SaveGroupsOfElements = -1000;
Mesh.CharacteristicLengthMin = {size};
Mesh.CharacteristicLengthMax = {size};
Box(1) = {{0, {-B/2}, {-H/2}, {L}, {B}, {H}}};
Physical Volume("VOLUME") = {{1}};
Physical Surface("X_FIXED") = Surface In BoundingBox {{-1e-7, {-B/2-1e-7}, {-H/2-1e-7}, 1e-7, {B/2+1e-7}, {H/2+1e-7}}};
Physical Surface("X_DISPLACED") = Surface In BoundingBox {{{L-1e-7}, {-B/2-1e-7}, {-H/2-1e-7}, {L+1e-7}, {B/2+1e-7}, {H/2+1e-7}}};
'''


def inspect_gmsh_inp(path: Path) -> dict:
    """Validate C3D10 topology, group mapping, geometry, and case pin coordinates."""
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, tuple[int, ...]] = {}
    element_sets: dict[str, set[int]] = {}
    nsets: dict[str, set[int]] = {}
    section = ""
    current_set = ""
    for raw in path.read_text(encoding="ascii").splitlines():
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            fields = [x.strip() for x in line[1:].split(",")]
            section = fields[0].upper()
            opts = {k.strip().upper(): v.strip() for f in fields[1:] if "=" in f for k, v in [f.split("=", 1)]}
            if section == "ELEMENT":
                if opts.get("TYPE", "").upper() != "C3D10":
                    raise ValueError(f"Gmsh INP contains unexpected element type: {line}")
                current_set = opts.get("ELSET", "").upper()
            elif section == "NSET":
                current_set = opts.get("NSET", "").upper()
                nsets.setdefault(current_set, set())
            elif section == "ELSET":
                current_set = opts.get("ELSET", "").upper()
                element_sets.setdefault(current_set, set())
            else:
                current_set = ""
            continue
        vals = [v.strip() for v in line.split(",") if v.strip()]
        if section == "NODE":
            if len(vals) != 4:
                raise ValueError(f"Malformed node row: {line}")
            nid = int(vals[0])
            if nid in nodes:
                raise ValueError(f"Duplicate node ID {nid}")
            nodes[nid] = tuple(float(v) for v in vals[1:])
        elif section == "ELEMENT":
            eid, conn = int(vals[0]), tuple(int(v) for v in vals[1:])
            if eid in elements:
                raise ValueError(f"Duplicate element ID {eid}")
            if len(conn) != 10 or len(set(conn)) != 10:
                raise ValueError(f"Element {eid} does not have 10 distinct C3D10 nodes")
            elements[eid] = conn
            if current_set:
                element_sets.setdefault(current_set, set()).add(eid)
        elif section == "ELSET" and current_set:
            element_sets[current_set].update(int(v) for v in vals)
        elif section == "NSET" and current_set:
            nsets[current_set].update(int(v) for v in vals)
    if not nodes or not elements:
        raise ValueError("Gmsh mesh must contain nodes and C3D10 volume elements")
    volume = element_sets.get("VOLUME", set())
    if not volume or volume != set(elements):
        raise ValueError(f"ELSET=VOLUME must map exactly all C3D10 elements (set={len(volume)}, elements={len(elements)})")
    corner_keys: set[tuple[int, ...]] = set()
    signed_volumes: list[float] = []
    for eid, conn in elements.items():
        if not set(conn) <= nodes.keys():
            raise ValueError(f"Element {eid} references an absent node")
        corners = tuple(sorted(conn[:4]))
        if corners in corner_keys:
            raise ValueError(f"Duplicate C3D10 corner topology at element {eid}")
        corner_keys.add(corners)
        a, b, c, d = (nodes[n] for n in conn[:4])
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        ad = tuple(d[i] - a[i] for i in range(3))
        det = (ab[0] * (ac[1] * ad[2] - ac[2] * ad[1])
               - ab[1] * (ac[0] * ad[2] - ac[2] * ad[0])
               + ab[2] * (ac[0] * ad[1] - ac[1] * ad[0]))
        signed_volumes.append(det / 6.0)
    if min(signed_volumes) <= 1e-12:
        raise ValueError(f"C3D10 corner orientation/volume must be positive (minimum={min(signed_volumes):.9g} mm^3)")
    corner_quality: list[float] = []
    corner_pairs = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))
    for conn, elem_volume in zip(elements.values(), signed_volumes):
        edge_square_sum = 0.0
        for i, j in corner_pairs:
            edge_square_sum += sum((nodes[conn[i]][axis] - nodes[conn[j]][axis]) ** 2 for axis in range(3))
        corner_quality.append(12.0 * (3.0 * elem_volume) ** (2.0 / 3.0) / edge_square_sum)
    edge_pairs = ((0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3))
    max_midside_deviation = 0.0
    for eid, conn in elements.items():
        for midside, (a, b) in enumerate(edge_pairs, 4):
            expected = tuple((nodes[conn[a]][axis] + nodes[conn[b]][axis]) / 2 for axis in range(3))
            deviation = max(abs(nodes[conn[midside]][axis] - expected[axis]) for axis in range(3))
            max_midside_deviation = max(max_midside_deviation, deviation)
            if deviation > 1e-7:
                raise ValueError(f"Element {eid} midside node {conn[midside]} is not the edge midpoint; Jacobian validation is inconclusive")
    volume_mm3 = sum(signed_volumes)
    expected_volume = L * B * H
    if not math.isclose(volume_mm3, expected_volume, rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError(f"C3D10 corner volume {volume_mm3:.9g} does not match geometry volume {expected_volume:.9g} mm^3")
    face_nodes = {
        "X_FIXED": {nid for nid, xyz in nodes.items() if math.isclose(xyz[0], 0.0, abs_tol=1e-8)},
        "X_DISPLACED": {nid for nid, xyz in nodes.items() if math.isclose(xyz[0], L, abs_tol=1e-8)},
    }
    for name in ("X_FIXED", "X_DISPLACED"):
        members = nsets.get(name, set())
        if not members or members != face_nodes[name] or not members <= nodes.keys():
            raise ValueError(f"NSET={name} does not exactly map expected end face (set={len(members)}, expected={len(face_nodes[name])})")
    pin_nodes: dict[str, int] = {}
    for name, xyz in (("PIN_A", (0.0, -B / 2, -H / 2)), ("PIN_B", (0.0, B / 2, -H / 2))):
        matching = [nid for nid, point in nodes.items() if all(math.isclose(point[i], xyz[i], abs_tol=1e-8) for i in range(3))]
        if len(matching) != 1:
            raise ValueError(f"{name} coordinate must resolve to exactly one mesh node; found {len(matching)}")
        pin_nodes[name] = matching[0]
    return {"node_count": len(nodes), "element_count": len(elements), "element_type": "C3D10",
            "orientation": {"minimum_corner_volume_mm3": min(signed_volumes), "negative_elements": 0,
                            "max_midside_edge_deviation_mm": max_midside_deviation,
                            "jacobian": "positive and constant for verified affine C3D10 mapping"},
            "shape_quality": {"metric": "corner tetrahedron mean ratio (1 is equilateral)",
                               "minimum": min(corner_quality), "mean": sum(corner_quality) / len(corner_quality),
                               "acceptance_threshold": None, "status": "measured_threshold_not_defined"},
            "volume_mm3": volume_mm3,
            "groups": {"VOLUME": len(volume), "X_FIXED": len(nsets["X_FIXED"]),
                       "X_DISPLACED": len(nsets["X_DISPLACED"]),
                       "PIN_A": {"node": pin_nodes["PIN_A"], "xyz_mm": [0, -B/2, -H/2], "selection": "unique coordinate match"},
                       "PIN_B": {"node": pin_nodes["PIN_B"], "xyz_mm": [0, B/2, -H/2], "selection": "unique coordinate match"}},
            "group_mapping": {"VOLUME": "ELSET=VOLUME exactly equals all C3D10 IDs",
                              "X_FIXED": "NSET exactly equals nodes at x=0",
                              "X_DISPLACED": f"NSET exactly equals nodes at x={L:g}",
                              "PIN_A": "derived from unique expected coordinates; not a Gmsh physical group",
                              "PIN_B": "derived from unique expected coordinates; not a Gmsh physical group"}}


def make_analysis_deck(mesh_path: Path, inspected: dict, out: Path, size: float) -> Path:
    """Turn a validated mesh artifact into a separate, runnable CCX input deck."""
    lines = mesh_path.read_text(encoding="ascii").splitlines()
    pin_a, pin_b = inspected["groups"]["PIN_A"]["node"], inspected["groups"]["PIN_B"]["node"]
    lines += ["", "*NSET, NSET=PIN_A", str(pin_a), "*NSET, NSET=PIN_B", str(pin_b),
              "*MATERIAL, NAME=STEEL", "*ELASTIC", f"{E}, {NU}",
              "*SOLID SECTION, ELSET=VOLUME, MATERIAL=STEEL", "", "*STEP", "*STATIC",
              "*BOUNDARY", "X_FIXED, 1, 1, 0.0", f"X_DISPLACED, 1, 1, {DELTA}",
              "PIN_A, 2, 3, 0.0", "PIN_B, 3, 3, 0.0",
              "*NODE PRINT, NSET=X_FIXED, TOTALS=ONLY", "RF",
              "*NODE PRINT, NSET=X_DISPLACED, TOTALS=ONLY", "RF",
              "*EL PRINT, ELSET=VOLUME", "S", "E", "COORD",
              "*NODE FILE", "U, RF", "*EL FILE", "S, E", "*END STEP", ""]
    deck = out / f"axial_bar_gmsh_deck_h{size:g}mm.inp"
    deck.write_text("\n".join(lines), encoding="ascii")
    inspect_analysis_deck(deck, inspected)
    return deck


def inspect_analysis_deck(path: Path, inspected: dict) -> None:
    lines = path.read_text(encoding="ascii").splitlines()
    text = "\n".join(lines).upper()
    required = ("*MATERIAL, NAME=STEEL", "*ELASTIC", "*SOLID SECTION, ELSET=VOLUME, MATERIAL=STEEL",
                "*STEP", "*STATIC", "*BOUNDARY", "*NODE PRINT, NSET=X_FIXED, TOTALS=ONLY",
                "*NODE PRINT, NSET=X_DISPLACED, TOTALS=ONLY", "*EL PRINT, ELSET=VOLUME",
                "*NODE FILE", "*EL FILE", "*END STEP")
    missing = [card for card in required if card not in text]
    if missing:
        raise ValueError(f"Analysis deck is missing required cards: {missing}")
    for name in ("PIN_A", "PIN_B"):
        if f"*NSET, NSET={name}" not in text:
            raise ValueError(f"Analysis deck is missing NSET={name}")
    if "PIN_A, 2, 3, 0.0" not in text or "PIN_B, 3, 3, 0.0" not in text:
        raise ValueError("Analysis deck does not apply the canonical pin DOFs")
    if "X_FIXED, 1, 1, 0.0" not in text or f"X_DISPLACED, 1, 1, {DELTA}" not in text:
        raise ValueError("Analysis deck does not apply the canonical axial end displacements")
    sets: dict[str, set[int]] = {}
    current_set = ""
    for line in lines:
        row = line.strip()
        if row.startswith("*"):
            fields = [field.strip() for field in row[1:].split(",")]
            section = fields[0].upper()
            options = {k.strip().upper(): v.strip().upper() for f in fields[1:] if "=" in f for k, v in [f.split("=", 1)]}
            current_set = options.get("NSET", "") if section == "NSET" else ""
            if current_set:
                sets.setdefault(current_set, set())
            continue
        if current_set:
            sets[current_set].update(int(value.strip()) for value in row.split(",") if value.strip())
    for name in ("PIN_A", "PIN_B"):
        expected = {inspected["groups"][name]["node"]}
        if sets.get(name) != expected:
            raise ValueError(f"Analysis NSET={name} does not contain exactly its validated coordinate node")
    expected_material = f"{E}, {NU}".upper()
    if expected_material not in text:
        raise ValueError("Analysis deck material constants do not match the canonical case")


def generate_gmsh(size: float, out: Path, gmsh_path: str) -> dict:
    geo = out / f"axial_bar_h{size:g}mm.geo"
    mesh_inp = out / f"axial_bar_gmsh_mesh_h{size:g}mm.inp"
    geo.write_text(gmsh_geo(size), encoding="ascii")
    with tempfile.TemporaryDirectory(prefix="gmsh-", dir=out) as tmp:
        candidate = Path(tmp) / mesh_inp.name
        cmd = [gmsh_path, str(geo), "-3", "-order", "2", "-format", "inp", "-o", str(candidate)]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if run.returncode:
            raise RuntimeError(f"Gmsh export failed ({run.returncode}): {run.stderr[-2000:]}")
        inspected = inspect_gmsh_inp(candidate)
        mesh_inp.write_bytes(candidate.read_bytes())
    deck = make_analysis_deck(mesh_inp, inspected, out, size)
    return {"characteristic_size_mm": size, **inspected, "geometry": geo.name,
            "geometry_sha256": sha256(geo),
            "artifact_kind": "mesh_and_separate_analysis_deck", "mesh_input": mesh_inp.name,
            "mesh_sha256": sha256(mesh_inp), "analysis_input": deck.name,
            "analysis_sha256": sha256(deck), "export_command": cmd}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def local_tool(binary: str, explicit: Path | None = None) -> str | None:
    if explicit:
        return str(explicit.expanduser().resolve())
    env_name = "GMSH_BIN" if binary == "gmsh" else "CCX_BIN"
    if os.environ.get(env_name):
        return str(Path(os.environ[env_name]).expanduser().resolve())
    local = {
        "gmsh": ROOT / ".local-tools/gmsh-4.15.2/gmsh-4.15.2-Linux64/bin/gmsh",
        "ccx": ROOT / ".local-tools/ccx-2.23/CalculiX/ccx_2.23/src/ccx_2.23",
    }.get(binary)
    if local and local.is_file():
        return str(local)
    return shutil.which(binary)


def runtime_env(lib_dir: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    configured = lib_dir or (Path(os.environ["CCX_LIB_DIR"]) if os.environ.get("CCX_LIB_DIR") else None)
    local_runtime = ROOT / ".local-tools/ccx-2.23/runtime/usr/lib/x86_64-linux-gnu"
    if configured is None and local_runtime.is_dir():
        configured = local_runtime
    if configured:
        old = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = str(configured.resolve()) + (f":{old}" if old else "")
    return env


def read_deck_mesh(path: Path) -> tuple[dict[int, tuple[float, float, float]], dict[int, tuple[int, ...]]]:
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, tuple[int, ...]] = {}
    section = ""
    for raw in path.read_text(encoding="ascii").splitlines():
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            section = line[1:].split(",", 1)[0].strip().upper()
            continue
        vals = [v.strip() for v in line.split(",") if v.strip()]
        if section == "NODE":
            nodes[int(vals[0])] = tuple(float(v) for v in vals[1:4])
        elif section == "ELEMENT":
            elements[int(vals[0])] = tuple(int(v) for v in vals[1:])
    if not nodes or not elements or any(len(conn) != 10 for conn in elements.values()):
        raise ValueError("Analysis deck does not contain a complete C3D10 mesh")
    return nodes, elements


def extract_metrics(deck: Path, dat: Path) -> dict:
    """Extract force totals and volume-weighted central integration-point metrics."""
    nodes, elements = read_deck_mesh(deck)
    edge_pairs = ((0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3))
    element_volumes: dict[int, float] = {}
    for eid, conn in elements.items():
        if not set(conn) <= nodes.keys():
            raise ValueError(f"Element {eid} references an absent node")
        for i, (a, b) in enumerate(edge_pairs, 4):
            midpoint = tuple((nodes[conn[a]][axis] + nodes[conn[b]][axis]) / 2 for axis in range(3))
            if any(not math.isclose(nodes[conn[i]][axis], midpoint[axis], abs_tol=1e-7) for axis in range(3)):
                raise ValueError(f"Element {eid} has a curved/nonlinear midside mapping; Jacobian weights are not assessed")
        a, b, c, d = (nodes[n] for n in conn[:4])
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        ad = tuple(d[i] - a[i] for i in range(3))
        det = (ab[0] * (ac[1] * ad[2] - ac[2] * ad[1])
               - ab[1] * (ac[0] * ad[2] - ac[2] * ad[0])
               + ab[2] * (ac[0] * ad[1] - ac[1] * ad[0]))
        if det <= 1e-12:
            raise ValueError(f"Element {eid} has non-positive corner orientation/volume")
        element_volumes[eid] = det / 6.0

    values: dict[str, dict[tuple[int, int], tuple[float, ...]]] = {"S": {}, "E": {}, "COORD": {}}
    totals: dict[str, tuple[float, float, float]] = {}
    active = ""
    for line in dat.read_text(encoding="ascii").splitlines():
        low = line.lower().strip()
        if "total force (fx,fy,fz) for set " in low:
            name = low.split("for set", 1)[1].split("and time", 1)[0].strip().upper()
            if name not in ("X_FIXED", "X_DISPLACED"):
                continue
            totals[name] = ()
            continue
        if line.lower().find("stresses (elem, integ.pnt.") >= 0:
            active = "S"
            continue
        if line.lower().find("strains (elem, integ.pnt.") >= 0:
            active = "E"
            continue
        if "coordinates (elem, integ.pnt." in low:
            active = "COORD"
            continue
        if low.startswith("summary of job information") or low.startswith("step "):
            active = ""
        fields = line.split()
        # The first nonempty numeric row after each total-force heading contains Fx,Fy,Fz.
        for set_name in ("X_FIXED", "X_DISPLACED"):
            if set_name in totals and len(totals[set_name]) == 0 and fields:
                try:
                    force = tuple(float(v) for v in fields[:3])
                    if len(force) == 3:
                        totals[set_name] = force
                        break
                except ValueError:
                    pass
        if active and fields:
            try:
                eid, ip = int(fields[0]), int(fields[1])
                payload = tuple(float(v) for v in fields[2:])
            except (ValueError, IndexError):
                continue
            if eid in elements:
                expected = 3 if active == "COORD" else 6
                if len(payload) == expected:
                    key = (eid, ip)
                    if key in values[active]:
                        raise ValueError(f"Duplicate {active} output for element/integration point {key}")
                    values[active][key] = payload
    expected_points = {(eid, ip) for eid in elements for ip in range(1, 5)}
    for name in ("S", "E", "COORD"):
        if set(values[name]) != expected_points:
            raise ValueError(f"{name} output points do not match C3D10 mesh (observed={len(values[name])}, expected={len(expected_points)})")
    if set(totals) != {"X_FIXED", "X_DISPLACED"} or any(len(x) != 3 for x in totals.values()):
        raise ValueError("Missing CCX total reaction force output for one or both end faces")

    selected = []
    for key in sorted(expected_points):
        eid = key[0]
        x = values["COORD"][key][0]
        if L / 3 <= x <= 2 * L / 3:
            selected.append((values["S"][key], values["E"][key], element_volumes[eid] / 4.0))
    if not selected:
        raise ValueError("No integration points fall inside the canonical central-third region")
    total_weight = sum(item[2] for item in selected)
    avg_s = [sum(item[0][i] * item[2] for item in selected) / total_weight for i in range(6)]
    avg_e = [sum(item[1][i] * item[2] for item in selected) / total_weight for i in range(6)]
    reaction = abs(totals["X_FIXED"][0])
    balance = abs(totals["X_FIXED"][0] + totals["X_DISPLACED"][0]) / (E * B * H * DELTA / L)
    reference = {"reaction_N": E * B * H * DELTA / L, "S11_MPa": E * DELTA / L,
                 "E11": DELTA / L, "E22": -NU * DELTA / L, "E33": -NU * DELTA / L}
    observed = {"reaction_N": reaction, "S11_MPa": avg_s[0], "E11": avg_e[0], "E22": avg_e[1], "E33": avg_e[2]}
    errors = {key: 100 * abs(observed[key] - target) / abs(target) for key, target in reference.items()}
    return {"status": "extracted", "method": {"field_source": "CalculiX .dat integration-point S/E/COORD",
            "weighting": "corner tetra volume / 4; verified affine C3D10 midside nodes",
            "central_region_mm": [L/3, 2*L/3], "sample_count": len(selected),
            "volume_weight_mm3": total_weight, "element_count": len(elements), "integration_points_per_element": 4},
            "reactions": {"X_FIXED_Fx_N": totals["X_FIXED"][0], "X_DISPLACED_Fx_N": totals["X_DISPLACED"][0],
                          "equilibrium_relative_to_reference": balance},
            "reference": reference, "observed": observed, "relative_error_percent": errors,
            "acceptance": {"reaction_le_1_percent": errors["reaction_N"] <= 1,
                           "central_stress_le_1_percent": errors["S11_MPa"] <= 1,
                           "lateral_strain_y_le_5_percent": errors["E22"] <= 5,
                           "lateral_strain_z_le_5_percent": errors["E33"] <= 5}}


def version(binary: str, explicit: Path | None = None, lib_dir: Path | None = None) -> dict:
    path = local_tool(binary, explicit)
    if not path:
        return {"available": False, "path": None, "version": None, "sha256": None}
    probe_cwd = None
    p = None
    try:
        # CCX interprets --version as an input basename and creates .dat/.sta/.cvg.
        # Probe it only in an experiment-local temporary directory and preserve the banner.
        if binary == "ccx":
            probe_root = ROOT / "runs" / "ccx-probes"
            probe_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="ccx-version-", dir=probe_root) as tmp:
                probe_cwd = str(Path(tmp).relative_to(ROOT))
                p = subprocess.run([path, "--version"], cwd=tmp, capture_output=True, text=True,
                                   timeout=5, env=runtime_env(lib_dir))
        else:
            probe_cwd = None
            p = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
        lines = (p.stdout + "\n" + p.stderr).splitlines()
        output = "\n".join(lines)
        ccx_version = re.search(r"CalculiX Version [^\r\n]+", output)
        reported = ccx_version.group(0).strip() if ccx_version else next((line.strip() for line in lines if line.strip()), f"exit={p.returncode}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        reported = f"version query failed: {exc}"
    output = (p.stdout + p.stderr) if p else reported
    available = not (binary == "ccx" and p is not None and p.returncode == 127 and "error while loading shared libraries" in output)
    return {"available": available, "path": os.path.realpath(path), "version": reported,
            "probe_exit_code": p.returncode if p else None,
            "probe_cwd": f"{probe_cwd} (temporary; removed after probe)" if probe_cwd else None,
            "probe_note": "CCX has no --version option; banner captured in isolated cwd; exit 201 is expected." if binary == "ccx" and p and p.returncode == 201 else None,
            "sha256": sha256(Path(path)) if Path(path).is_file() else None}


def execute_deck(deck: Path, output: Path, ccx_path: str, lib_dir: Path | None) -> dict:
    base_dir = output / "solver_runs" / deck.stem
    run_dir = base_dir
    attempt = 1
    while run_dir.exists():
        run_dir = base_dir / f"attempt_{attempt:03d}"
        attempt += 1
    run_dir.mkdir(parents=True, exist_ok=True)
    run_input = run_dir / deck.name
    shutil.copy2(deck, run_input)
    cmd = [ccx_path, "-i", run_input.stem]
    try:
        result = subprocess.run(cmd, cwd=run_dir, capture_output=True, text=True,
                                timeout=600, env=runtime_env(lib_dir))
        (run_dir / "solver.stdout.log").write_text(result.stdout, encoding="utf-8")
        (run_dir / "solver.stderr.log").write_text(result.stderr, encoding="utf-8")
        dat_path = run_dir / f"{run_input.stem}.dat"
        metrics = None
        extraction_error = None
        if result.returncode == 0 and "Job finished" in result.stdout and dat_path.is_file():
            try:
                metrics = extract_metrics(run_input, dat_path)
                (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
            except (ValueError, OSError) as exc:
                extraction_error = str(exc)
        generated = sorted(p for p in run_dir.iterdir() if p.is_file() and p != run_input)
        size_match = re.search(r"h([0-9.]+)mm", deck.name)
        size = float(size_match.group(1)) if size_match else None
        mesh_artifact = output / f"axial_bar_gmsh_mesh_h{size:g}mm.inp" if size is not None else None
        geometry_artifact = output / f"axial_bar_h{size:g}mm.geo" if size is not None else None
        return {"deck": deck.name, "run_directory": str(run_dir.relative_to(output)),
                "cwd": str(run_dir.resolve()),
                "command": cmd, "exit_code": result.returncode,
                "status": ("solver_completed_and_extracted" if metrics else
                           "solver_completed_extraction_unassessed" if result.returncode == 0 and not extraction_error else
                           "solver_completed_extraction_failed" if result.returncode == 0 else "solver_failed"),
                "metrics": metrics, "extraction_error": extraction_error,
                "source_artifacts": {"analysis_deck_sha256": sha256(deck),
                                     "mesh_input": mesh_artifact.name if mesh_artifact else None,
                                     "mesh_input_sha256": sha256(mesh_artifact) if mesh_artifact and mesh_artifact.is_file() else None,
                                     "geometry": geometry_artifact.name if geometry_artifact else None,
                                     "geometry_sha256": sha256(geometry_artifact) if geometry_artifact and geometry_artifact.is_file() else None,
                                     "run_input_sha256": sha256(run_input)},
                "artifacts": {p.name: sha256(p) for p in generated}}
    except subprocess.TimeoutExpired as exc:
        (run_dir / "solver.stdout.log").write_text(exc.stdout or "", encoding="utf-8")
        (run_dir / "solver.stderr.log").write_text(exc.stderr or "", encoding="utf-8")
        return {"deck": deck.name, "run_directory": str(run_dir.relative_to(output)),
                "cwd": str(run_dir.resolve()),
                "command": cmd, "exit_code": None, "status": "timeout", "timeout_seconds": 600}


def archive_if_present(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = path.with_name(f"{path.stem}.history.{stamp}{path.suffix}")
    serial = 1
    while archived.exists():
        archived = path.with_name(f"{path.stem}.history.{stamp}.{serial:03d}{path.suffix}")
        serial += 1
    shutil.copy2(path, archived)
    return archived


def refresh_solver_history(output: Path) -> Path:
    history_path = output / "solver_runs_history.json"
    previous = json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else {"schema_version": "1.0", "runs": []}
    by_path = {item["run_directory"]: item for item in previous.get("runs", [])}
    solver_root = output / "solver_runs"
    if solver_root.is_dir():
        for dat in solver_root.rglob("*.dat"):
            run_dir = dat.parent
            inputs = sorted(run_dir.glob("*.inp"))
            if not inputs:
                continue
            run_key = str(run_dir.relative_to(output))
            metrics_path = run_dir / "metrics.json"
            stdout = run_dir / "solver.stdout.log"
            sta = dat.with_suffix(".sta")
            files = {item.name: sha256(item) for item in sorted(run_dir.iterdir()) if item.is_file()}
            previous_item = by_path.get(run_key)
            entry = {"run_directory": run_key, "cwd": str(run_dir.resolve()),
                     "deck": inputs[0].name, "input_sha256": sha256(inputs[0]),
                     "solver_completed": bool(stdout.is_file() and "Job finished" in stdout.read_text(encoding="utf-8", errors="replace")),
                     "status_file": sta.name if sta.is_file() else None,
                     "metrics": json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else None,
                     "artifacts_sha256": files}
            if previous_item:
                # The immutable execution directory is the evidence; keep its original ledger entry.
                entry = previous_item
            by_path[run_key] = entry
    previous["runs"] = [by_path[key] for key in sorted(by_path)]
    history_path.write_text(json.dumps(previous, indent=2) + "\n", encoding="utf-8")
    return history_path


def reconcile_output(output: Path, report: dict | None = None) -> dict:
    report_path = output / "solver_runs_manifest.json"
    report = report or json.loads(report_path.read_text(encoding="utf-8"))
    manifest_path = output / "manifest.json"
    prepared_path = output / "manifest.prepared.json"
    if not prepared_path.is_file():
        if not manifest_path.is_file():
            raise ValueError("Cannot reconcile without the prepared manifest")
        shutil.copy2(manifest_path, prepared_path)
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    final = dict(prepared)
    final["preparation_status"] = prepared.get("status")
    final["status"] = report.get("status", "solver_runs_completed")
    final["finalized_at_utc"] = datetime.now(timezone.utc).isoformat()
    final["execution"] = dict(prepared.get("execution", {}))
    final["execution"].update({"solver_executed": True, "solver_runs_manifest": report_path.name,
                               "solver_runs_manifest_sha256": sha256(report_path) if report_path.is_file() else None,
                               "solver_run_count": len(report.get("runs", [])),
                               "run_directories": [run.get("run_directory") for run in report.get("runs", [])],
                               "limitation": None})
    final["gates"] = report.get("gates", {})
    history_path = refresh_solver_history(output)
    final["solver_runs_history"] = {"path": history_path.name, "sha256": sha256(history_path),
                                    "attempt_count": len(json.loads(history_path.read_text(encoding="utf-8")).get("runs", []))}
    manifest_path.write_text(json.dumps(final, indent=2) + "\n", encoding="utf-8")
    return final


def generate(size: float, out: Path) -> dict:
    nx, ny, nz = round(L / size), round(B / size), round(H / size)
    if not all(math.isclose(n * size, d) for n, d in ((nx, L), (ny, B), (nz, H))):
        raise ValueError(f"mesh size {size} does not divide the dimensions")
    nodes: list[tuple[float, float, float]] = []
    ids = {}
    for k in range(nz + 1):
        for j in range(ny + 1):
            for i in range(nx + 1):
                n = len(nodes) + 1
                ids[i, j, k] = n
                nodes.append((i * size, -B / 2 + j * size, -H / 2 + k * size))
    elems = []
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                elems.append((ids[i,j,k], ids[i+1,j,k], ids[i+1,j+1,k], ids[i,j+1,k],
                              ids[i,j,k+1], ids[i+1,j,k+1], ids[i+1,j+1,k+1], ids[i,j+1,k+1]))
    fixed = [ids[0,j,k] for k in range(nz + 1) for j in range(ny + 1)]
    moved = [ids[nx,j,k] for k in range(nz + 1) for j in range(ny + 1)]
    # Pin A at y=-b/2 and pin B at the farthest distinct y; only transverse DOFs.
    pin_a = ids[0,0,0]
    pin_b = ids[0,ny,0]
    lines = ["*HEADING", "OCRE axial bar; units N, mm, MPa", "*NODE"]
    lines += [f"{n}, {x:.9g}, {y:.9g}, {z:.9g}" for n, (x,y,z) in enumerate(nodes, 1)]
    lines += ["*ELEMENT, TYPE=C3D8, ELSET=VOLUME"]
    lines += [f"{e}, " + ", ".join(map(str, conn)) for e, conn in enumerate(elems, 1)]
    for name, values in (("X_FIXED", fixed), ("X_DISPLACED", moved)):
        lines.append(f"*NSET, NSET={name}")
        lines += [", ".join(map(str, values[i:i+16])) for i in range(0, len(values), 16)]
    lines += ["*MATERIAL, NAME=STEEL", "*ELASTIC", f"{E}, {NU}",
              "*SOLID SECTION, ELSET=VOLUME, MATERIAL=STEEL", "", "*STEP", "*STATIC",
              "*BOUNDARY", "X_FIXED, 1, 1, 0.0", f"X_DISPLACED, 1, 1, {DELTA}",
              f"{pin_a}, 2, 3, 0.0", f"{pin_b}, 3, 3, 0.0",
              "*NODE PRINT, NSET=X_FIXED", "RF", "*EL PRINT, ELSET=VOLUME", "S", "E",
              "*NODE FILE", "U, RF", "*EL FILE", "S, E", "*END STEP", ""]
    inp = out / f"axial_bar_h{size:g}mm.inp"
    inp.write_text("\n".join(lines), encoding="ascii")
    return {"characteristic_size_mm": size, "divisions": {"x": nx, "y": ny, "z": nz},
            "node_count": len(nodes), "element_count": len(elems), "element_type": "C3D8",
            "groups": {"VOLUME": len(elems), "X_FIXED": len(fixed), "X_DISPLACED": len(moved),
                       "PIN_A": {"node": pin_a, "xyz_mm": [0, -B/2, -H/2], "fixed_dofs": [2,3]},
                       "PIN_B": {"node": pin_b, "xyz_mm": [0, B/2, -H/2], "fixed_dofs": [3]}},
            "input": inp.name, "sha256": sha256(inp)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("preflight", "prepare", "prepare-gmsh", "solve", "reconcile"), nargs="?", default="prepare")
    ap.add_argument("--output", type=Path, default=ROOT / "runs")
    ap.add_argument("--gmsh", type=Path, help="Gmsh executable; otherwise GMSH_BIN, local tool, then PATH")
    ap.add_argument("--ccx", type=Path, help="CalculiX executable; otherwise CCX_BIN, local tool, then PATH")
    ap.add_argument("--ccx-lib-dir", type=Path, help="Local directory containing CCX runtime libraries (also CCX_LIB_DIR)")
    ap.add_argument("--size", type=float, choices=SIZES, help="Restrict solve to one characteristic size")
    args = ap.parse_args()
    args.output = args.output.expanduser().resolve()
    if not args.output.is_relative_to(ROOT.resolve()):
        ap.error(f"--output must stay inside {ROOT}")
    if args.command == "reconcile":
        final = reconcile_output(args.output)
        print(json.dumps({"manifest": str(args.output / "manifest.json"), "status": final["status"],
                          "solver_executed": final["execution"]["solver_executed"],
                          "run_directories": final["execution"]["run_directories"],
                          "gates": final["gates"]}, indent=2))
        return 0
    gmsh, ccx = version("gmsh", args.gmsh), version("ccx", args.ccx, args.ccx_lib_dir)
    if args.command == "preflight":
        result = {"python": platform.python_version(), "gmsh": gmsh, "calculix_ccx": ccx,
                  "ready_to_mesh": True, "ready_to_solve": ccx["available"],
                  "note": "C3D8 baseline is separate. Gmsh C3D10 export yields a mesh artifact; a separate analysis deck is written only after mesh checks pass."}
        print(json.dumps(result, indent=2))
        return 0 if ccx["available"] else 2

    args.output.mkdir(parents=True, exist_ok=True)
    if args.command == "solve":
        if not ccx["available"]:
            print("CalculiX is unavailable or its runtime dependencies could not be loaded.", file=sys.stderr)
            return 2
        decks = sorted(args.output.glob("axial_bar_gmsh_deck_h*mm.inp"))
        if args.size is not None:
            decks = [deck for deck in decks if f"h{args.size:g}mm" in deck.name]
        if not decks:
            print("No validated Gmsh analysis decks found; run prepare-gmsh first.", file=sys.stderr)
            return 2
        runs = [execute_deck(deck, args.output, ccx["path"], args.ccx_lib_dir) for deck in decks]
        extracted = all(run.get("metrics") and run["status"] == "solver_completed_and_extracted" for run in runs)
        convergence = {"status": "not_assessed"}
        if extracted and len(runs) == len(SIZES):
            parsed = []
            for run in runs:
                match = re.search(r"h([0-9.]+)mm", run["deck"])
                parsed.append((float(match.group(1)), run))
            parsed.sort(key=lambda item: item[0])
            fine, coarse = parsed[0][1]["metrics"], parsed[1][1]["metrics"]
            changes = {key: 100 * abs(fine["observed"][key] - coarse["observed"][key]) / abs(fine["observed"][key])
                       for key in ("reaction_N", "S11_MPa")}
            convergence = {"status": "passed" if all(value <= 1 for value in changes.values()) else "failed",
                           "compared_sizes_mm": [parsed[0][0], parsed[1][0]],
                           "relative_change_percent": changes, "limit_percent": 1}
        acceptance = extracted and all(all(run["metrics"]["acceptance"].values()) for run in runs)
        execution_passed = all(run["status"].startswith("solver_completed") for run in runs)
        prepared_manifest = args.output / "manifest.json"
        shape_metrics = [artifact.get("shape_quality", {}) for artifact in json.loads(prepared_manifest.read_text()).get("mesh_artifacts", [])] if prepared_manifest.is_file() else []
        manifest_path = args.output / "solver_runs_manifest.json"
        archive_if_present(manifest_path)
        report = {"status": "solver_runs_completed" if execution_passed else "solver_runs_incomplete",
                  "toolchain": {"calculix_ccx": ccx}, "runs": runs,
                  "gates": {
                      "A_input_case": {"status": "not_assessed", "note": "Case is hardcoded from the canonical reference; no external schema/revision input is validated."},
                      "B_mesh_and_groups": {"status": "warning" if any(x.get("status") == "measured_threshold_not_defined" for x in shape_metrics) else "passed",
                          "note": "Connectivity, semantic groups, volume, corner orientation, and affine midside/Jacobian checks passed; mean-ratio shape is recorded without an acceptance threshold."},
                      "C_solver_deck": {"status": "passed", "note": "Separate analysis decks passed card checks and CalculiX accepted all executed decks."},
                      "D_numerical_solution": {"status": "passed" if acceptance and convergence.get("status") == "passed" else "warning" if extracted else "not_assessed",
                          "analytical_criteria": "passed" if acceptance else "failed_or_not_assessed", "convergence": convergence},
                      "E_provenance_and_failures": {"status": "warning", "note": "Input/output hashes, versions, logs and native outputs are recorded; fault-injection/replay and external validation are not assessed."},
                      "solver_execution": "passed" if execution_passed else "failed_or_incomplete",
                      "result_extraction": "passed" if extracted else "not_assessed_or_failed"}}
        manifest_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        final_manifest = reconcile_output(args.output, report)
        print(json.dumps({"status": report["status"], "manifest": str(args.output / "manifest.json"),
                          "solver_runs_manifest": str(manifest_path),
                          "history": final_manifest["solver_runs_history"], "runs": runs,
                          "gates": report["gates"]}, indent=2))
        return 0 if execution_passed else 1
    if args.command == "prepare-gmsh":
        if not gmsh["available"]:
            print("Gmsh is unavailable; no mesh export was attempted.", file=sys.stderr)
            return 2
        meshes = [generate_gmsh(s, args.output, gmsh["path"]) for s in SIZES]
    else:
        meshes = [generate(s, args.output) for s in SIZES]
    ref = {"axial_strain": DELTA/L, "axial_stress_MPa": E*DELTA/L,
           "reaction_N": E*(B*H)*DELTA/L,
           "lateral_strain": -NU*DELTA/L}
    manifest = {"schema_version": "0.2.0", "case_id": "case_tension_bar_v1",
                "artifact_kind": "gmsh_c3d10_mesh_and_separate_ccx_analysis_decks" if args.command == "prepare-gmsh" else "structured_c3d8_baseline_decks",
                "status": "prepared_solver_not_run" if not ccx["available"] else "prepared_solver_available_not_run",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "geometry": {"shape": "rectangular_prism", "length_mm": L, "width_mm": B, "height_mm": H,
                             "axis": "x", "units": {"length": "mm", "force": "N", "stress": "MPa"}},
                "material": {"model": "linear_elastic_isotropic", "young_modulus_MPa": E, "poisson_ratio": NU},
                "boundary_conditions": {"x_fixed_ux_mm": 0, "x_displaced_ux_mm": DELTA,
                                        "transverse_pins": "two minimal pins; free Poisson contraction"},
                "analytical_reference_only": ref, "mesh_artifacts": meshes,
                "toolchain": {"python": platform.python_version(), "gmsh": gmsh, "calculix_ccx": ccx},
                "execution": {"solver_executed": False,
                              "limitation": "Solver has not been run; no FE result is claimed." if ccx["available"] else "CalculiX ccx not found or its local runtime cannot be loaded; no FE result is claimed.",
                              "resource_limits": "Gmsh timeout 120 s per export; CalculiX timeout 600 s per deck; no explicit memory cap."}}
    manifest_path = args.output / "manifest.json"
    archive_if_present(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    label = "Gmsh C3D10 mesh artifacts plus separate CalculiX analysis decks" if args.command == "prepare-gmsh" else "structured C3D8 baseline decks"
    print(f"Prepared {len(meshes)} {label} in {args.output}")
    print(f"Analytical reference (not solver output): stress={ref['axial_stress_MPa']:.6g} MPa, reaction={ref['reaction_N']:.6g} N")
    print(f"Manifest: {manifest_path} (sha256 {sha256(manifest_path)})")
    if not ccx["available"]:
        print("PREPARED ONLY: CalculiX is unavailable; no stress/reaction extraction from an FE run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
