"""The setup macro vocabulary, read from the game's headers: for every AI command, prop and
intro macro, its parameter names. The merger uses the names to know which arguments are
character numbers, object tags, pads, rooms, AI list ids, objectives or stage flags."""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HEADERS = [os.path.join(ROOT, "src", "include", f) for f in ("commands.h", "props.h", "intro.h")]

_DEF = re.compile(r"^#define\s+([a-z_][a-z0-9_]*)\(([^)]*)\)", re.M)
_DEF0 = re.compile(r"^#define\s+([a-z_][a-z0-9_]*)\s*\\?$", re.M)


def signatures():
    """{macro: [param names]}; macros without parameters map to []."""
    sigs = {}
    for h in HEADERS:
        text = open(h, encoding="utf-8", errors="replace").read()
        for m in _DEF.finditer(text):
            sigs[m.group(1)] = [p.strip() for p in m.group(2).split(",") if p.strip()]
        for m in _DEF0.finditer(text):
            sigs.setdefault(m.group(1), [])
    return sigs


# Parameter name -> what it refers to. Anything else is a plain value.
KINDS = {
    "chr": "chr", "chrnum": "chr",
    "object": "obj", "obj": "obj", "door": "obj", "lift": "obj", "item": "obj", "safe": "obj",
    "pad": "pad", "targetpad": "pad", "pad1": "pad", "pad2": "pad", "pad3": "pad", "pad4": "pad",
    "room": "room", "room_or_pad": "room_or_pad", "roomorpad": "room_or_pad",
    "ailist": "ailist", "ailistid": "ailist", "function": "ailist",
    "objective": "objective",
    "stageflag": "stageflag",
    "label": "label",
}

# Prop macros whose arguments are command-index offsets to other props (not tags): left alone.
OFFSET_PARAMS = {
    "link_guns": {"gun1offset", "gun2offset"},
    "lift_door": {"dooroffset", "liftoffset"},
    "padlocked_door": {"dooroffset", "lockoffset"},
    "rename_object": {"objoffset"},
    "safeitem": {"item", "safe", "door"},
    "conditional_scenery": {"trigger", "unexp", "exp"},
    "blocked_path": {"objoffset"},
    "tag": {"value"},
}


_derived = None


def derived_kinds():
    """{command macro: {param: kind}} read from the engine itself: each command's byte
    layout from its macro in commands.h, and what the handler in chraicommands.c does with
    each operand byte (looks a tag up, finds a chr, jumps to a label, resolves a pad, an AI
    list or an objective). Parameter names are not trusted for AI commands; this is."""
    global _derived
    if _derived is not None:
        return _derived
    cmds_h = open(os.path.join(ROOT, "src", "include", "commands.h"), encoding="utf-8", errors="replace").read()
    chrai = open(os.path.join(ROOT, "src", "game", "chrai.c"), encoding="utf-8", errors="replace").read()
    handlers_c = open(os.path.join(ROOT, "src", "game", "chraicommands.c"), encoding="utf-8", errors="replace").read()
    campaign_c = open(os.path.join(ROOT, "src", "game", "campaign.c"), encoding="utf-8", errors="replace").read()
    # opcode -> handler name
    start = chrai.index("g_CommandPointers[])(void) = {")
    end = chrai.index("};", start)
    handler_of = {int(m.group(1), 16): m.group(2) for m in re.finditer(r"/\*0x([0-9a-f]{4})\*/\s*(\w+),", chrai[start:end])}
    # handler name -> body
    bodies = {}
    for src in (handlers_c, campaign_c):
        for m in re.finditer(r"(?ms)^bool (ai\w+)\(void\)\s*\{(.*?)^\}", src):
            bodies[m.group(1)] = m.group(2)
    # macro -> (params, elements)
    layouts = {}
    pos = 0
    pat = re.compile(r"(?m)^#define\s+([a-z_][a-z0-9_]*)\(([^)]*)\)[ \t]*\\?[ \t]*$")
    for m in pat.finditer(cmds_h):
        name = m.group(1)
        params = [p.strip() for p in m.group(2).split(",") if p.strip()]
        # the body: the continuation lines after the define line
        body = ""
        if m.group(0).rstrip().endswith("\\"):
            i = m.end() + 1
            while True:
                line_end = cmds_h.find("\n", i)
                line = cmds_h[i:line_end]
                body += line.rstrip().rstrip("\\").rstrip() + " "
                if not line.rstrip().endswith("\\"):
                    break
                i = line_end + 1
        elements = [e.strip() for e in _split_top(body) if e.strip()]
        layouts[name] = (params, elements)
    out = {}
    for name, (params, elements) in layouts.items():
        # byte layout: offset -> param name (or None)
        bytes_ = []
        ok = True
        for e in elements:
            mm = re.fullmatch(r"mkshort\((.*)\)", e)
            mw = re.fullmatch(r"mkword\((.*)\)", e)
            if mm:
                inner = mm.group(1).strip()
                bytes_ += [inner if inner in params else None] * 2
            elif mw:
                inner = mw.group(1).strip()
                bytes_ += [inner if inner in params else None] * 4
            elif re.fullmatch(r"[a-z_][a-z0-9_]*", e) and e in params:
                bytes_.append(e)
            elif re.fullmatch(r"[a-z_][a-z0-9_]*\(.*\)", e):
                ok = False  # expands to another command; not derived
                break
            else:
                names = [p for p in params if re.search(r"\b" + re.escape(p) + r"\b", e)]
                bytes_.append(names[0] if len(names) == 1 else None)
        if not ok or len(bytes_) < 2 or not elements[0].startswith("mkshort("):
            continue
        try:
            opcode = int(re.fullmatch(r"mkshort\((.*)\)", elements[0]).group(1), 0)
        except (ValueError, AttributeError):
            continue
        handler = handler_of.get(opcode)
        body = bodies.get(handler, "")
        kinds = {}

        def mark(k, kind):
            if 0 <= k < len(bytes_) and bytes_[k]:
                kinds.setdefault(bytes_[k], kind)

        for mm in re.finditer(r"(?:tagFindById|objFindByTagId)\(cmd\[(\d+)\]", body):
            mark(int(mm.group(1)), "obj")
        for mm in re.finditer(r"chrFindById\(g_Vars\.chrdata, cmd\[(\d+)\]", body):
            mark(int(mm.group(1)), "chr")
        for mm in re.finditer(r"chrFindByLiteralId\(cmd\[(\d+)\]", body):
            mark(int(mm.group(1)), "chr")
        for mm in re.finditer(r"chraiGoToLabel\(g_Vars\.ailist, g_Vars\.aioffset, cmd\[(\d+)\]", body):
            mark(int(mm.group(1)), "label")
        for mm in re.finditer(r"ailistFindById\(([^;]*?)\)\s*;", body):
            for b in re.findall(r"cmd\[(\d+)\]", mm.group(1)):
                mark(int(b), "ailist")
        for mm in re.finditer(r"\b(?:pad_?id|padnum|pad)\s*=\s*([^;]*cmd\[[^;]*);", body):
            for b in re.findall(r"cmd\[(\d+)\]", mm.group(1)):
                mark(int(b), "pad")
        for mm in re.finditer(r"\b(?:room|roomnum|room_id)\s*=\s*([^;]*cmd\[[^;]*);", body):
            for b in re.findall(r"cmd\[(\d+)\]", mm.group(1)):
                mark(int(b), "room")
        for mm in re.finditer(r"(?:objectiveCheck|objectiveGetDifficultyBits)\(cmd\[(\d+)\]", body):
            mark(int(mm.group(1)), "objective")
        out[name] = kinds
    _derived = out
    return out


def _split_top(s):
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def kind_of(macro, param):
    if macro == "tag" and param == "id":
        return "obj"  # tag(id, value): the tag number scripts name the object by
    if macro in OFFSET_PARAMS and param in OFFSET_PARAMS[macro]:
        return "offset"
    if macro == "lift" and param in ("door1", "door2", "door3", "door4"):
        return "offset"
    if macro in ("weapon", "hat") and param == "chr":
        # weapon(scale, model, chr, ...): the pad field, 0 when the weapon is a chr's own
        return "value"
    d = derived_kinds().get(macro)
    if d is not None:
        if param in d:
            k = d[param]
            if k == "chr" and "pad" in param:
                return "pad"  # a pad operand the handler can also read as a chr (chr_move_to_pad)
            return k
        # a derived layout exists; names still decide what the handlers do not read directly
        k = KINDS.get(param, "value")
        return k if k in ("room", "room_or_pad", "stageflag", "label", "objective", "ailist", "pad") else "value"
    return KINDS.get(param, "value")


if __name__ == "__main__":
    s = signatures()
    print(len(s), "macros")
    for name in ("chr", "door", "if_chr_in_room", "set_ailist", "beginobjective", "tag", "teleport_to_pad", "set_stage_flag"):
        print(name, s.get(name))
