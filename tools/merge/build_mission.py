"""Build one merged mission end to end: geometry (background, collision, pads), the merged
setup, and the engine's leg table.

    build_mission.py <key> <datadir>

writes <datadir>/files/bgdata/bg_<key>.seg, bg_<key>_tilesZ, bg_<key>_padsZ and
<datadir>/files/Usetup<key>Z, plus src/game/merged/legs_<key>.inc.c for the engine build.
<key> is dd, chi or a51 (tools/merge/mission.py)."""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from merge import mission, setupmerge, setupasm, rzip  # noqa: E402

ROOT = mission.ROOT


def build(key, datadir):
    outdir = os.path.join(ROOT, "build", "merged", key)
    os.makedirs(outdir, exist_ok=True)
    files = os.path.join(datadir, "files")
    os.makedirs(os.path.join(files, "bgdata"), exist_ok=True)

    # geometry; building the pads also writes src/generated/ntsc-final/pads/<key>.h
    info = mission.build_geometry(key, outdir)
    for name in ("bg_%s.seg" % key, "bg_%s_tilesZ" % key, "bg_%s_padsZ" % key):
        shutil.copyfile(os.path.join(outdir, "bgdata", name), os.path.join(files, "bgdata", name))

    # the pad names resolve through the merged pads header alone; text ids through the language headers
    # Names resolve through the levels' own pad headers first (a few setups name pads that
    # exist nowhere else), then the merged pads header, whose indices win.
    pads_dir = os.path.join(ROOT, "src", "generated", "ntsc-final", "pads")
    build_pads = os.path.join(setupasm.GENERATED, "pads")
    lang = os.path.join(setupasm.GENERATED, "lang")
    leg_names = [w for ws in [w["pads"] for w in mission.MISSIONS[key]["worlds"]] for w in ws]
    if key == "a51":
        leg_names = ["lue", "lip", "tra"]  # one pads file under three names
    symbols = []
    for n in leg_names:
        for d in (build_pads, pads_dir):
            p = os.path.join(d, n + ".h")
            if os.path.exists(p):
                symbols.append(p)
    symbols += [os.path.join(lang, f) for f in os.listdir(lang) if f.endswith(".h")]
    symbols.append(os.path.join(pads_dir, "%s.h" % key))

    legs = setupmerge.mission_legs(key, info["room_offsets"])
    # where each leg's own pads begin in the merged pads file (its names carry the level)
    pad_names = list(info["pad_positions"].keys())
    for leg in legs:
        prefix = "PAD_%s_" % leg.name.upper()
        first = next((i for i, n in enumerate(pad_names) if n.startswith(prefix)), 0)
        leg.pad_base = first
        leg.lift_base = info["lift_offsets"][leg.world]
    merger = setupmerge.Merger(legs, os.path.join(outdir, "setup%s.c" % key), os.path.join(outdir, "setup%s.json" % key),
                               key=key, symbol_paths=symbols, pad_positions=info["pad_positions"])
    merger.world_offsets = [tuple(int(c) for c in w["offset"]) for w in mission.MISSIONS[key]["worlds"]]
    side = merger.run()

    setup = setupasm.assemble_file(os.path.join(outdir, "setup%s.c" % key), symbol_paths=symbols)
    raw = setup.write()
    open(os.path.join(outdir, "Usetup%sZ" % key), "wb").write(rzip.deflate(raw))
    shutil.copyfile(os.path.join(outdir, "Usetup%sZ" % key), os.path.join(files, "Usetup%sZ" % key))

    merged_dir = os.path.join(ROOT, "src", "game", "merged")
    os.makedirs(merged_dir, exist_ok=True)
    shutil.copyfile(os.path.join(outdir, "legs_%s.inc.c" % key), os.path.join(merged_dir, "legs_%s.inc.c" % key))

    info.pop("pad_positions", None)
    print("%s: %s; setup %d props words, %d ailists, %d bytes compressed; legs %s" % (
        key, info, len(setup.props), len(setup.ailists), os.path.getsize(os.path.join(outdir, "Usetup%sZ" % key)),
        [(l["name"], l["objective_first"], l["objective_count"], l["excluded_objects"], l["gates"]) for l in side["legs"]]))
    for l in side["legs"]:
        d = l["dedup"]
        if not d:
            continue
        if d["excluded"]:
            print("  %s: already placed by an earlier leg, not created again: %s; %d of them differ in the arguments beyond the place"
                  % (l["name"], d["excluded"], d["differing"]))
        for model, places in d["lifts"]:
            print("  %s: lift %s gains a later leg's stops, now %s" % (l["name"], model, places))
        if d["variants"]:
            print("  %s: %d stand where the earlier object exists on some difficulties only (created on the others); %d of those are tagged"
                  % (l["name"], d["variants"], d["tagged_variants"]))
        for model, place, kleg, klock, mlock in d["locks"]:
            print("  %s: door %s at %s has %s's lock %s until this leg begins, then %s" % (l["name"], model, place, kleg, klock, mlock))


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
