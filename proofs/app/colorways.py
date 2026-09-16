"""Alternate colorways (PRD 5.3 / "Optional elements"): up to four per
version, each a full thread-stop assignment with its own render and
mockup, offered on the proof page as tappable cards; the approval
records the chosen one. Built in Proofs by re-digitizing the version's
document with the thread colours swapped -- the design (and its hash)
does not change, only the palette, which is exactly the PRD's line
between a design change and a colorway change.
"""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import core_client, events, garments, proofs, stitch, storage
from .db import Colorway, Proof, ProofVersion, ThreadStop, User

MAX_COLORWAYS = 4


def _hex_to_rgb(hex_: str) -> dict:
    h = hex_.strip().lstrip("#")
    if len(h) != 6:
        raise proofs.TransitionError(f"'{hex_}' isn't a colour (use #rrggbb).")
    return {"r": int(h[0:2], 16), "g": int(h[2:4], 16), "b": int(h[4:6], 16)}


def add_colorway(db: Session, v: ProofVersion, user: Optional[User], *, name: str, stop_colors: list[dict],
                 stitch_client: Optional[core_client.StitchClient] = None) -> Colorway:
    """`stop_colors`: per stop in order, {"hex", "name", "brand", "code"}."""
    proof = db.get(Proof, v.proof_id)
    if v.status != "ready_to_send":
        raise proofs.TransitionError("Colorways can only be added before the version is sent.")
    existing = list(v.colorways)
    if len(existing) + 1 >= MAX_COLORWAYS + 1 and len(existing) >= MAX_COLORWAYS - 1:
        raise proofs.TransitionError(f"Up to {MAX_COLORWAYS} colorways per version.")
    base_stops = list(v.thread_stops)
    if len(stop_colors) != len(base_stops):
        raise proofs.TransitionError(f"Give a colour for each of the {len(base_stops)} stops.")
    document = json.loads(v.document_json)
    # Swap every object's thread colour by matching its rgb to the stop it belongs to.
    by_hex = {s.hex.lower(): i for i, s in enumerate(base_stops)}
    for obj in document.get("objects", []):
        rgb = obj.get("threadColor", {}).get("rgb", {})
        hx = "#%02x%02x%02x" % (int(rgb.get("r", 0)), int(rgb.get("g", 0)), int(rgb.get("b", 0)))
        i = by_hex.get(hx)
        if i is None:
            continue
        new = stop_colors[i]
        obj["threadColor"] = {"id": obj["threadColor"].get("id"), "name": new.get("name") or new["hex"], "brand": new.get("brand") or None,
                              "catalogNumber": new.get("code") or None, "rgb": _hex_to_rgb(new["hex"])}
    client = stitch_client or core_client.stitch
    digitized = client.digitize(document)
    analysis = stitch.analyze(digitized)
    if analysis.design_hash != v.design_hash:
        raise proofs.TransitionError("The colorway changed the design itself; that can't happen -- refusing.")
    ordinal = (max(c.ordinal for c in existing) + 1) if existing else 2
    cw = Colorway(proof_version_id=v.id, ordinal=ordinal, name=name.strip() or f"Colorway {ordinal}")
    db.add(cw)
    db.flush()
    for i, s in enumerate(analysis.stops):
        db.add(ThreadStop(proof_version_id=v.id, colorway_id=cw.id, stop_number=s.stop_number, thread_brand=s.thread_brand, thread_code=s.thread_code,
                          thread_name=s.thread_name, hex=s.hex, stitch_count=s.stitch_count))
    render = stitch.render_png(digitized)
    hashes = v.artifact_hashes
    cws = hashes.setdefault("colorways", {})
    entry = {"render": stitch.sha256(render)}
    storage.put(proofs._artifact_key(proof, v.version_number, f"cw{ordinal}-render.png"), render)
    if v.garment_template_id and v.garment_template_id in garments.TEMPLATE_BY_ID:
        t = garments.TEMPLATE_BY_ID[v.garment_template_id]
        zone = t.zone(v.garment_zone or t.default_zone)
        mock = garments.composite(render, stitch.render_pixels_per_mm(render, analysis), t, zone, garments.color_hex(v.garment_color),
                                  offsets_mm=(v.placement_down_mm, v.placement_across_mm))
        storage.put(proofs.artifact_key(proof, v, f"cw{ordinal}-mockup.png"), mock)
        entry["mockup"] = stitch.sha256(mock)
    cws[str(ordinal)] = entry
    v.artifact_hashes_json = json.dumps(hashes, sort_keys=True)
    events.append(db, proof_id=proof.id, proof_version_id=v.id, event_type="colorway_added", actor_type="user" if user else "system",
                  actor_id=user.id if user else "", payload={"ordinal": ordinal, "name": cw.name, "stops": [s.hex for s in analysis.stops]})
    return cw


def stops_for(db: Session, v: ProofVersion, ordinal: int) -> list[ThreadStop]:
    if ordinal <= 1:
        return list(v.thread_stops)
    cw = next((c for c in v.colorways if c.ordinal == ordinal), None)
    if cw is None:
        return list(v.thread_stops)
    return list(db.execute(select(ThreadStop).where(ThreadStop.colorway_id == cw.id).order_by(ThreadStop.stop_number)).scalars())


def colorway_by_ordinal(v: ProofVersion, ordinal: int) -> Optional[Colorway]:
    return next((c for c in v.colorways if c.ordinal == ordinal), None)
