"""Merge several levels' setup sources into one setup source for a merged mission.

Each level ("leg") keeps its own scripts and objects. What changes is everything that would
collide or that the engine must tell apart:

  - the leg's #defines are prefixed (L0_, L1_, ...) and the values that are numbers in the
    engine's small namespaces are rebased: character numbers, object tags, AI list ids
  - numeric character, tag, AI list, objective and path operands in commands are rebased the
    same way; room numbers of a leg in a later world are offset by that world's room offset
  - every stage flag command addresses the leg's own flag word (set_stage_flag_w and friends)
  - if_all_objectives_complete becomes a check of the leg's own objective range
  - the success exit of a non-final leg (end_level in its outro list) becomes campaign_joint
  - the intro section is the first leg's; later legs' intro weapons are recorded for the
    engine to hand out at the joint
  - later legs' characters start on a gate list that waits for the leg to begin
  - structural objects a later leg places where an earlier one already did (same type, model
    and pad) are never created, and the later leg's tag is pointed at the earlier object

The result assembles with setupasm.py like any stock setup."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from merge import macros  # noqa: E402

ROOT = macros.ROOT
SETUPS = os.path.join(ROOT, "src", "setups")

# One command each: structural things that exist in the world whichever leg placed them.
DEDUPE_TYPES = {"door", "lift", "glass", "tinted_glass", "safe", "fan", "alarm", "cctv",
                "singlemonitor", "multimonitor", "hangingmonitors", "autogun", "escastep"}
EXCLUDE_FLAGS = "OBJFLAG2_EXCLUDE_A | OBJFLAG2_EXCLUDE_SA | OBJFLAG2_EXCLUDE_PA | OBJFLAG2_EXCLUDE_PD"
CHR_SPECIAL_MIN = 0xF0  # chr operands at or above this are the engine's special values


class Leg:
    def __init__(self, name, index, world, room_offset, chr_base, tag_base, ailist_base,
                 objective_base, path_base, outro=None, final=False):
        self.name = name              # setup short name, e.g. "lue"
        self.index = index            # leg number 0..
        self.world = world            # world index in the merged geometry
        self.room_offset = room_offset
        self.chr_base = chr_base
        self.tag_base = tag_base
        self.ailist_base = ailist_base
        self.objective_base = objective_base
        self.path_base = path_base
        self.pad_base = 0             # where this leg's own pads start in the merged pads file
        self.outro = outro            # name of the AI list whose end_level is the success exit
        self.final = final
        self.prefix = "L%d_" % index
        # filled while merging
        self.text = None
        self.defines = {}             # original name -> value text
        self.ailist_ids = {}          # old id -> new id
        self.num_objectives = 0
        self.intro = []               # (macro, args) of this leg's intro
        self.spawn_pad = None
        self.gates = {}               # function id (text) -> gate list name
        self.props = []               # list of (macro, args, rawcall) in order
        self.lists = {}               # new array name -> body text
        self.list_order = []
        self.table = []               # (array name, new id)
        self.paths = []               # (array name, new id, flags)
        self.path_arrays = {}         # new array name -> body text


# ----------------------------------------------------------------------------- text helpers

# The build this is for: the NTSC final release (src/include/versions.h).
VERSION_DEFINES = {"VERSION_NTSC_BETA": 0, "VERSION_NTSC_1_0": 1, "VERSION_NTSC_FINAL": 2,
                   "VERSION_PAL_BETA": 3, "VERSION_PAL_FINAL": 4, "VERSION_JPN_FINAL": 5,
                   "VERSION": 2, "PAL": 0, "PLATFORM_N64": None}


def resolve_conditionals(text):
    """Apply the setup's #if/#ifdef/#else/#endif lines for the final NTSC build, so what is
    left is what the game's own data holds. Only the version symbols above are known."""
    out = []
    stack = []  # (taking, any_taken)
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("#if ") or s.startswith("#ifdef ") or s.startswith("#ifndef "):
            parent = all(t for t, _ in stack)
            if s.startswith("#ifdef "):
                cond = VERSION_DEFINES.get(s.split()[1], None) is not None
            elif s.startswith("#ifndef "):
                cond = VERSION_DEFINES.get(s.split()[1], None) is None
            else:
                cond = bool(_eval_cond(s[4:]))
            stack.append((parent and cond, cond))
            continue
        if s.startswith("#elif "):
            taking, taken = stack.pop()
            parent = all(t for t, _ in stack)
            cond = bool(_eval_cond(s[6:])) and not taken
            stack.append((parent and cond, taken or cond))
            continue
        if s.startswith("#else"):
            taking, taken = stack.pop()
            parent = all(t for t, _ in stack)
            stack.append((parent and not taken, True))
            continue
        if s.startswith("#endif"):
            stack.pop()
            continue
        if all(t for t, _ in stack):
            out.append(line)
    return "\n".join(out)


def _eval_cond(expr):
    e = expr.split("//")[0].strip()
    for name, val in VERSION_DEFINES.items():
        if val is not None:
            e = re.sub(r"\b%s\b" % name, str(val), e)
    e = e.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    e = e.replace(" not =", " !=")
    return eval(e, {"__builtins__": {}}, {})


def split_args(s):
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    last = "".join(cur).strip()
    if last or out:
        out.append(last)
    return out


def find_calls(text, names):
    """Yield (start, end, name, args) for every statement-level macro call in text whose name
    is in `names`. A call may span lines. Calls inside comments are skipped."""
    pat = re.compile(r"(?m)^[ \t]*([a-z_][a-z0-9_]*)[ \t]*\(")
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            return
        name = m.group(1)
        if name not in names:
            pos = m.end()
            continue
        # balanced parentheses from the "("
        i = m.end() - 1
        depth = 0
        j = i
        while j < len(text):
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            elif c == "'":
                j = text.find("'", j + 1 + (1 if text[j + 1] == "\\" else 0)) if j + 1 < len(text) else j
            j += 1
        start = m.start(1)
        end = j + 1
        yield start, end, name, split_args(text[i + 1:j])
        pos = end


def is_int(tok):
    return re.fullmatch(r"-?(0[xX][0-9a-fA-F]+|\d+)", tok.strip()) is not None


def fmt(v):
    return "0x%02x" % v if v >= 0 else str(v)


# ----------------------------------------------------------------------------- the merger

class Merger:
    def __init__(self, legs, out_c, sidecar, key="mission", symbol_paths=None, pad_positions=None):
        self.legs = legs
        self.out_c = out_c
        self.sidecar = sidecar
        self.key = key
        self.symbol_paths = symbol_paths
        self.pad_positions = pad_positions or {}  # pad name -> (x, y, z) in the merged world, in pads order
        self.pad_by_index = None
        self.pad_symbols = {}
        self.sigs = macros.signatures()
        self.cmd_names = set(self.sigs)

    # ---- per leg
    def load(self, leg):
        path = os.path.join(SETUPS, "setup%s.c" % leg.name)
        text = open(path, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
        leg.text = resolve_conditionals(text)

    def expand_local_macros(self, leg):
        """Expand the leg's own function-like and body-carrying macros so only the engine's
        macros remain. Value defines (#define NAME 0x..) are kept. A setup may define a
        macro, use it, #undef it and define it again differently, so uses are expanded with
        the definition in effect where they stand."""
        lines = leg.text.split("\n")
        defs = {}
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"#define\s+([A-Za-z_]\w*)(\([^)]*\))?[ \t]*(.*)$", line)
            if m and (m.group(2) or line.rstrip().endswith("\\")):
                name = m.group(1)
                params = [p.strip() for p in m.group(2)[1:-1].split(",")] if m.group(2) else None
                body = m.group(3)
                while body.rstrip().endswith("\\"):
                    body = body.rstrip()[:-1] + "\n"
                    i += 1
                    body += lines[i]
                defs[name] = (params, self._expand_text(body, defs))
                i += 1
                continue
            m = re.match(r"#undef\s+([A-Za-z_]\w*)", line)
            if m:
                defs.pop(m.group(1), None)
                i += 1
                continue
            # a use may span lines until its parentheses close
            if defs and re.search(r"\b(%s)\s*\(" % "|".join(re.escape(n) for n, (p, b) in defs.items() if p is not None), line) and line.count("(") > line.count(")"):
                while i + 1 < len(lines) and line.count("(") > line.count(")"):
                    i += 1
                    line += "\n" + lines[i]
            out.append(self._expand_text(line, defs) if defs else line)
            i += 1
        leg.text = "\n".join(out)

    @staticmethod
    def _expand_text(text, defs):
        for _ in range(6):
            changed = False
            for name, (params, body) in defs.items():
                if params is not None:
                    pat = re.compile(r"\b" + re.escape(name) + r"\s*\(")
                    pos = 0
                    while True:
                        m = pat.search(text, pos)
                        if not m:
                            break
                        i = m.end() - 1
                        depth, j = 0, i
                        while j < len(text):
                            if text[j] == "(":
                                depth += 1
                            elif text[j] == ")":
                                depth -= 1
                                if depth == 0:
                                    break
                            j += 1
                        args = split_args(text[i + 1:j])
                        expansion = body
                        for p, a in zip(params, args):
                            expansion = re.sub(r"\b" + re.escape(p) + r"\b", lambda _m, a=a: a, expansion)
                        text = text[:m.start()] + expansion + text[j + 1:]
                        pos = m.start() + len(expansion)
                        changed = True
                else:
                    pat = re.compile(r"\b" + re.escape(name) + r"\b")
                    if pat.search(text):
                        text = pat.sub(lambda _m: body, text)
                        changed = True
            if not changed:
                break
        return text

    def collect_defines(self, leg):
        for m in re.finditer(r"(?m)^#define\s+([A-Z_][A-Z0-9_]*)\s+(\S+)[ \t]*(?://.*)?$", leg.text):
            leg.defines[m.group(1)] = m.group(2)
        # a setup may list its labels in an enum; the members are renamed like defines
        leg.enum_members = []
        for em in re.finditer(r"enum\s*\w*\s*\{([^}]*)\}\s*;", leg.text):
            for item in em.group(1).split(","):
                name = item.split("=")[0].strip()
                if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                    leg.enum_members.append(name)

    def collect_tables(self, leg):
        """Find the ailists table and the paths table; assign new ids.

        The engine reads a list's class from its id (src/game/game_00b820.c): ids of 0x1000
        and above run as chr-less background scripts from stage start, those of 0x1400 and
        above also tick through cutscenes, and 0x0c00-0x0cff are the cutscenes themselves,
        started by number. Everything below is a character's script. New ids keep the class
        and only move within it, a block per leg."""
        text = leg.text
        m = re.search(r"struct ailist ailists\[\] = \{(.*?)\n\};", text, re.S)
        counts = {"chr": 0, "bg": 0, "bgc": 0}
        k = leg.index
        for entry in re.finditer(r"\{\s*([a-z_][a-z0-9_]*)\s*,\s*(0x[0-9a-fA-F]+|\d+)\s*\}", m.group(1)):
            old = int(entry.group(2), 0)
            if old not in leg.ailist_ids:
                if old >= 0x1400:
                    new = 0x1400 + k * 0x100 + counts["bgc"]; counts["bgc"] += 1
                elif old >= 0x1000:
                    new = 0x1000 + k * 0x100 + counts["bg"]; counts["bg"] += 1
                elif 0x0C00 <= old < 0x0D00:
                    new = 0x0C00 + k * 0x10 + (old - 0x0C00)
                else:
                    new = 0x0400 + k * 0x200 + counts["chr"]; counts["chr"] += 1
                leg.ailist_ids[old] = new
            leg.table.append((entry.group(1), leg.ailist_ids[old]))
        leg.gate_base = 0x0400 + k * 0x200 + 0x180
        if counts["chr"] > 0x180 or counts["bg"] > 0x100 or counts["bgc"] > 0x100:
            raise ValueError("too many AI lists in leg %s: %r" % (leg.name, counts))
        leg.intro_list_ids = {lid for arr, lid in leg.table if "intro" in arr and "outro" not in arr}
        m = re.search(r"struct path paths\[\] = \{(.*?)\n\};", text, re.S)
        if m:
            for entry in re.finditer(r"\{\s*([a-z_][a-z0-9_]*)\s*,\s*([^,}]+)\s*,\s*([^,}]+)\s*\}", m.group(1)):
                arr = entry.group(1)
                if arr in ("NULL", "0"):
                    continue
                pid = int(entry.group(2).strip(), 0)
                leg.paths.append((arr, leg.path_base + pid, entry.group(3).strip()))

    def rebase_defines(self, leg):
        """Return {old name: (new name, new value text)}."""
        out = {}
        for name, val in leg.defines.items():
            new = leg.prefix + name
            v = val
            if is_int(val):
                iv = int(val, 0)
                if name.startswith("CHR_") and iv < CHR_SPECIAL_MIN:
                    v = fmt(iv + leg.chr_base)
                elif name.startswith("OBJ_"):
                    v = fmt(iv + leg.tag_base)
                elif name.startswith("AILIST_") and iv in leg.ailist_ids:
                    v = fmt(leg.ailist_ids[iv])
                elif name.startswith("PATH_"):
                    v = fmt(iv + leg.path_base)
            out[name] = (new, v)
        return out

    def value_of(self, leg, tok):
        """Numeric value of a token that is a literal or a leg define, else None."""
        t = tok.strip()
        if is_int(t):
            return int(t, 0)
        if t in leg.defines and is_int(leg.defines[t]):
            return int(leg.defines[t], 0)
        return None

    def rewrite_arg(self, leg, macro, param, arg, args, params):
        kind = macros.kind_of(macro, param)
        t = arg.strip()
        if kind == "chr":
            if is_int(t):
                v = int(t, 0)
                return fmt(v + leg.chr_base) if v < CHR_SPECIAL_MIN else t
            return arg
        if kind == "obj":
            if is_int(t):
                v = int(t, 0)
                return fmt(v + leg.tag_base) if v < 0xF0 else t
            return arg
        if kind == "ailist":
            if is_int(t):
                v = int(t, 0)
                return fmt(leg.ailist_ids[v]) if v in leg.ailist_ids else t
            return arg
        if kind == "objective":
            if is_int(t):
                return fmt(int(t, 0) + leg.objective_base)
            return arg
        if kind == "pad":
            # pads are named in these sources; a number is a pad index of the leg's own pads
            # file, which sits at pad_base in the merged one
            if is_int(t) and int(t, 0) > 0 and leg.pad_base:
                return fmt(int(t, 0) + leg.pad_base)
            return arg
        if kind == "room":
            if is_int(t) and leg.room_offset:
                return fmt(int(t, 0) + leg.room_offset)
            return arg
        if kind == "room_or_pad":
            # set_lights_state(roomorpad, ...): rooms are small numbers, pads are PAD_ names
            if is_int(t) and leg.room_offset:
                return fmt(int(t, 0) + leg.room_offset)
            return arg
        return arg

    def rewrite_call(self, leg, name, args):
        params = self.sigs.get(name, [])
        new_args = []
        for i, a in enumerate(args):
            p = params[i] if i < len(params) else "value"
            new_args.append(self.rewrite_arg(leg, name, p, a, args, params))
        # stage flags address the leg's word
        if name == "set_stage_flag":
            return "set_stage_flag_w(%d, %s)" % (leg.index, new_args[0])
        if name == "unset_stage_flag":
            return "unset_stage_flag_w(%d, %s)" % (leg.index, new_args[0])
        if name == "if_stage_flag_eq":
            return "if_stage_flag_w_eq(%d, %s, %s, %s)" % (leg.index, new_args[0], new_args[1], new_args[2])
        if name == "if_all_objectives_complete":
            return "if_objectives_range_complete(%d, %d, %s)" % (leg.objective_base, leg.objective_base + leg.num_objectives - 1, new_args[0])
        if name == "assign_path":
            v = self.value_of(leg, args[0])
            if v is not None:
                return "assign_path(%s)" % fmt(v + leg.path_base)
        if name == "chr":
            # chr(spawnflags, chrnum, pad, body, head, function, ...): gate later legs' scripts
            fn = new_args[5].strip()
            fv = self.value_of(leg, args[5])
            if fv is not None and fv in leg.ailist_ids:
                fv = leg.ailist_ids[fv]
            if leg.index > 0 and fv is not None and fv != 0:
                gate = leg.gates.get(fv)
                if gate is None:
                    # an intro list runs only when its leg is the current one, so a restart
                    # at a later leg never replays it; everything else runs from its leg on
                    intro = fv in leg.intro_list_ids
                    gate = ("l%d_gate_%04x" % (leg.index, fv), leg.gate_base + len(leg.gates), intro)
                    leg.gates[fv] = gate
                new_args[5] = fmt(gate[1])
        return "%s(%s)" % (name, ", ".join(new_args))

    def count_objectives(self, leg):
        leg.num_objectives = len(re.findall(r"(?m)^\s*beginobjective\(", leg.text))

    def transform(self, leg):
        """Rename defines and rewrite every command; split the text into its arrays."""
        text = leg.text
        renames = self.rebase_defines(leg)
        # rewrite calls (from the end so spans stay valid)
        calls = list(find_calls(text, self.cmd_names))
        for start, end, name, args in reversed(calls):
            text = text[:start] + self.rewrite_call(leg, name, args) + text[end:]
        # rename defines and their uses
        for old, (new, val) in renames.items():
            text = re.sub(r"\b" + re.escape(old) + r"\b", new, text)
            text = re.sub(r"(?m)^#define\s+" + re.escape(new) + r"\s+\S+", "#define %s %s" % (new, val), text)
        for old in leg.enum_members:
            text = re.sub(r"\b" + re.escape(old) + r"\b", leg.prefix + old, text)
        # rename arrays
        for arr, _id in leg.table:
            text = re.sub(r"\b" + re.escape(arr) + r"\b", leg.prefix.lower() + arr, text)
        for arr, _id, _f in leg.paths:
            text = re.sub(r"\b" + re.escape(arr) + r"\b", leg.prefix.lower() + arr, text)
        leg.text = text
        # extract pieces
        leg.define_block = "\n".join(l for l in text.split("\n") if l.startswith("#define")) + "\n"
        for em in re.finditer(r"enum\s*\w*\s*\{[^}]*\}\s*;", text):
            leg.define_block += em.group(0).replace("enum labelnum", "enum l%d_labelnum" % leg.index) + "\n"
        leg.props_body = self._body(text, r"u32 props\[\] = \{")
        leg.intro_body = self._body(text, r"s32 intro\[\] = \{")
        for m in re.finditer(r"(?s)^(u8 (l\d+_[a-z0-9_]+)\[\] = \{)(.*?)\n\};", text, re.M):
            leg.lists[m.group(2)] = m.group(3)
            leg.list_order.append(m.group(2))
        # A later leg's background scripts (ids 0x1000 and up run chr-less from stage start)
        # wait for their leg; an intro's runs only while its leg is the current one.
        if leg.index > 0:
            prefix = leg.prefix.lower()
            for arr, lid in leg.table:
                name = prefix + arr
                if lid >= 0x1000 and name in leg.lists and not leg.lists[name].lstrip().startswith("beginloop(0xfe)"):
                    check = "if_campaign_leg_eq" if lid in leg.intro_list_ids else "if_campaign_leg_ge"
                    gate = "\n\tbeginloop(0xfe)\n\t\t%s(%d, /*goto*/ 0xfd)\n\tendloop(0xfe)\n\n\tlabel(0xfd)\n" % (check, leg.index)
                    leg.lists[name] = gate + leg.lists[name]
        for m in re.finditer(r"(?s)^(s32 (l\d+_path[0-9a-z_]+)\[\] = \{)(.*?)\n\};", text, re.M):
            leg.path_arrays[m.group(2)] = m.group(3)
        # the success exit becomes the joint
        if not leg.final and leg.outro:
            name = leg.prefix.lower() + leg.outro
            body = leg.lists[name]
            body2 = re.sub(r"(?m)^(\s*)end_level\b", r"\1campaign_joint(%d)" % (leg.index + 1), body)
            if body2 == body:
                raise ValueError("no end_level in outro list %s of leg %s" % (leg.outro, leg.name))
            leg.lists[name] = body2
        # record the intro
        for start, end, name, args in find_calls(leg.intro_body, {"spawn", "intro_weapon", "ammo", "outfit"}):
            leg.intro.append((name, args))
            if name == "spawn":
                leg.spawn_pad = args[0]

    @staticmethod
    def _body(text, header_re):
        m = re.search(header_re, text)
        i = m.end()
        depth = 1
        j = i
        while depth:
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        return text[i:j - 1]

    # ---- props across legs
    def prop_calls(self, leg):
        """Statement-level prop macro calls in the leg's props body, in order, with raw text."""
        out = []
        for start, end, name, args in find_calls(leg.props_body, self.cmd_names):
            out.append([name, args, leg.props_body[start:end]])
        return out

    def place_key(self, name, args):
        """What makes two structural objects the same thing: type, model and where they stand.
        Pads are compared by position when the mission's pads are known, so a door that
        Defection and Extraction both place (under different pad names) is found."""
        params = self.sigs[name]
        model = args[params.index("model")].strip()
        pad = args[params.index("pad")].strip()
        pos = self.pad_position(pad)
        if pos is not None:
            return (name, model, tuple(int(round(c / 10.0)) for c in pos))
        return (name, model, pad)

    def pad_position(self, pad):
        """A pad's position in the merged world, by name or, failing that, by the index the
        mission's symbol headers give the name (Area 51's three levels name one pads file)."""
        if pad in self.pad_positions:
            return self.pad_positions[pad]
        if self.pad_by_index is None:
            from merge import setupasm
            self.pad_by_index = list(self.pad_positions.values()) if self.pad_positions else []
            self.pad_symbols = setupasm.load_enums(self.symbol_paths) if self.symbol_paths else {}
        idx = self.pad_symbols.get(pad)
        if idx is not None and 0 <= idx < len(self.pad_by_index):
            return self.pad_by_index[idx]
        return None

    def merge_props(self):
        placed = {}  # place key -> absolute command index of the kept object, from earlier legs
        merged = []  # raw call texts in order
        index = 0
        for leg in self.legs:
            calls = self.prop_calls(leg)
            # absolute index of each call within the merged list
            tag_retarget = {}  # local call index -> absolute index to point at
            excluded = set()
            mine = {}
            kept_abs_guess = {}
            # first pass: what this leg places where an earlier leg already did
            for li, (name, args, raw) in enumerate(calls):
                if name in ("briefing",) and leg.index > 0:
                    excluded.add(("drop", li))
                    continue
                if name == "endprops":
                    excluded.add(("drop", li))
                    continue
                if name in DEDUPE_TYPES:
                    key = self.place_key(name, args)
                    if key in placed:
                        excluded.add(("exclude", li))
                        tag_retarget[li] = placed[key]
                    elif key not in mine:
                        mine[key] = li
            # emit, patching excluded objects and retargeting tags
            kept_abs = {}
            for li, (name, args, raw) in enumerate(calls):
                if ("drop", li) in excluded:
                    continue
                kept_abs[li] = index
                index += 1
            # second pass to write with knowledge of absolute positions
            for li, (name, args, raw) in enumerate(calls):
                if ("drop", li) in excluded:
                    continue
                if ("exclude", li) in excluded:
                    params = self.sigs[name]
                    fi = params.index("flags2")
                    new_args = list(args)
                    new_args[fi] = "(%s) | %s" % (args[fi].strip() or "0", EXCLUDE_FLAGS)
                    raw = "%s(%s)" % (name, ", ".join(new_args))
                if name == "tag":
                    # tag(id, value): value is the offset to the tagged object; retarget if excluded
                    target_local = li + int(args[1].strip(), 0)
                    if target_local in tag_retarget:
                        target_abs = tag_retarget[target_local]
                    else:
                        target_abs = kept_abs.get(target_local, kept_abs[li] + int(args[1].strip(), 0))
                    raw = "tag(%s, %d)" % (args[0].strip(), target_abs - kept_abs[li])
                elif name in ("lift", "lift_door", "padlocked_door", "link_guns", "rename_object", "safeitem", "conditional_scenery", "blocked_path"):
                    # relative offsets to other commands in the same leg: dropped commands
                    # shift them, and a reference to an object this leg no longer places
                    # (an earlier leg placed it) follows the tag to that earlier object
                    params = self.sigs[name]
                    new_args = list(args)
                    for pi, p in enumerate(params):
                        if macros.kind_of(name, p) == "offset" and is_int(args[pi]):
                            off = int(args[pi].strip(), 0)
                            if off == 0:
                                continue  # no link
                            tl = li + off
                            if tl in tag_retarget:
                                new_args[pi] = str(tag_retarget[tl] - kept_abs[li])
                            elif tl in kept_abs:
                                new_args[pi] = str(kept_abs[tl] - kept_abs[li])
                    raw = "%s(%s)" % (name, ", ".join(new_args))
                merged.append("\t" + raw)
            leg.excluded_count = sum(1 for e in excluded if e[0] == "exclude")
            # this leg's structures now count as placed, for the legs after it
            for key, li in mine.items():
                if li in kept_abs:
                    placed[key] = kept_abs[li]
        merged.append("\tendprops")
        return "\n".join(merged)

    # ---- output
    def write(self):
        out = []
        out.append("//\n// Merged mission: %s\n// Generated by tools/merge/setupmerge.py; do not edit.\n//\n\n" % " + ".join(l.name for l in self.legs))
        out.append('#include "stagesetup.h"\n\n')
        for leg in self.legs:
            out.append("// ---- leg %d: %s\n%s\n" % (leg.index, leg.name, leg.define_block))
        out.append("extern s32 intro[];\nextern u32 props[];\nextern struct path paths[];\nextern struct ailist ailists[];\n\n")
        out.append("struct stagesetup setup = {\n\tNULL,\n\tNULL,\n\tNULL,\n\tintro,\n\tprops,\n\tpaths,\n\tailists,\n\tNULL,\n};\n\n")
        out.append("u32 props[] = {\n%s\n};\n\n" % self.merge_props())
        out.append("s32 intro[] = {%s\n};\n\n" % self.legs[0].intro_body)
        for leg in self.legs:
            for arr, body in leg.path_arrays.items():
                out.append("s32 %s[] = {%s\n};\n\n" % (arr, body))
        out.append("struct path paths[] = {\n")
        for leg in self.legs:
            for arr, pid, flags in leg.paths:
                out.append("\t{ %s, %s, %s },\n" % (leg.prefix.lower() + arr, fmt(pid), flags))
        out.append("\t{ NULL, 0, 0 },\n};\n\n")
        for leg in self.legs:
            for name in leg.list_order:
                out.append("u8 %s[] = {%s\n};\n\n" % (name, leg.lists[name]))
            for fv, (gname, gid, intro) in leg.gates.items():
                check = "if_campaign_leg_eq" if intro else "if_campaign_leg_ge"
                out.append("u8 %s[] = {\n\tbeginloop(0x01)\n\t\t%s(%d, /*goto*/ 0x02)\n\tendloop(0x01)\n\n\tlabel(0x02)\n\tset_ailist(CHR_SELF, %s)\n\tendlist\n};\n\n" % (gname, check, leg.index, fmt(fv)))
        out.append("struct ailist ailists[] = {\n")
        for leg in self.legs:
            for arr, lid in leg.table:
                out.append("\t{ %s, %s },\n" % (leg.prefix.lower() + arr, fmt(lid)))
            for fv, (gname, gid, intro) in leg.gates.items():
                out.append("\t{ %s, %s },\n" % (gname, fmt(gid)))
        out.append("\t{ NULL, 0 },\n};\n")
        open(self.out_c, "w", encoding="utf-8").write("".join(out))
        side = {"legs": []}
        for leg in self.legs:
            side["legs"].append({
                "name": leg.name, "index": leg.index, "world": leg.world, "room_offset": leg.room_offset,
                "chr_base": leg.chr_base, "tag_base": leg.tag_base, "ailist_base": leg.ailist_base,
                "objective_first": leg.objective_base, "objective_count": leg.num_objectives,
                "spawn_pad": leg.spawn_pad, "intro": [[n, [a.strip() for a in args]] for n, args in leg.intro],
                "excluded_objects": getattr(leg, "excluded_count", 0), "gates": len(leg.gates),
            })
        json.dump(side, open(self.sidecar, "w"), indent=1)
        self.write_legs_table(side)
        return side

    def write_legs_table(self, side):
        """The leg table the engine compiles in (src/game/campaign.c includes it): where each
        leg starts, which objectives are its own, and what its intro handed out."""
        from merge import setupasm
        symbols = setupasm.load_enums(self.symbol_paths if getattr(self, "symbol_paths", None) else setupasm.default_symbol_paths())
        key = self.key
        rows = []
        for leg in side["legs"]:
            weapons = [a[0] for n, a in leg["intro"] if n == "intro_weapon"]
            ammo = [(a[0], a[1]) for n, a in leg["intro"] if n == "ammo"]
            outfit = next((a[0] for n, a in leg["intro"] if n == "outfit"), "OUTFIT_DEFAULT")
            pad = symbols[leg["spawn_pad"]]
            stage = {"lue": "STAGE_INFILTRATION", "lip": "STAGE_RESCUE", "tra": "STAGE_ESCAPE", "ame": "STAGE_DEFECTION",
                     "ear": "STAGE_INVESTIGATION", "ark": "STAGE_EXTRACTION", "pete": "STAGE_CHICAGO", "depo": "STAGE_G5BUILDING"}[leg["name"]]
            w = ", ".join(weapons + ["0"] * (6 - len(weapons)))
            am = ", ".join("{ %s, %s }" % (t, q) for t, q in ammo) or "{ 0, 0 }"
            off = self.world_offsets[leg["world"]] if getattr(self, "world_offsets", None) else (0, 0, 0)
            rows.append("\t{ %s, %d, %d, 0x%04x, %d, %d, %d, %d, { %d, %d, %d }, %s, %d, { %s }, %d, { %s } }," % (
                stage, leg["objective_first"], leg["objective_count"], pad, leg["world"], leg["room_offset"],
                leg["chr_base"], leg["tag_base"], off[0], off[1], off[2], outfit, len(weapons), w, len(ammo), am))
        text = "/* Generated by tools/merge/setupmerge.py for merged mission %s. Do not edit. */\n" % key
        text += "static const struct campaignleg g_CampaignLegs_%s[] = {\n%s\n};\n" % (key, "\n".join(rows))
        open(os.path.join(os.path.dirname(self.out_c), "legs_%s.inc.c" % key), "w").write(text)

    def run(self):
        for leg in self.legs:
            self.load(leg)
            self.expand_local_macros(leg)
            self.collect_defines(leg)
            self.collect_tables(leg)
            self.count_objectives(leg)
        # objective ranges
        base = 0
        for leg in self.legs:
            leg.objective_base = base
            base += leg.num_objectives
        for leg in self.legs:
            self.transform(leg)
        return self.write()


# ----------------------------------------------------------------------------- missions

def mission_legs(key, room_offsets):
    """The legs of each merged mission. Bases keep every leg's numbers apart: characters and
    tags in one byte, AI list ids per leg block, paths per leg."""
    # (setup, world, the AI list whose end_level is the level's success exit)
    if key == "a51":
        names = [("lue", 0, "func040e_outro"), ("lip", 0, "func0c01_outro"), ("tra", 0, "func0c02_outro")]
    elif key == "dd":
        names = [("ame", 0, "func0416_outro"), ("ear", 1, "func0417_outro"), ("ark", 0, "func0412_outro")]
    elif key == "chi":
        names = [("pete", 0, "func040b_outro"), ("depo", 1, "func040f_outro")]
    else:
        raise KeyError(key)
    legs = []
    chr_base, tag_base, path_base = 0, 0, 0
    for i, (name, world, outro) in enumerate(names):
        legs.append(Leg(name, i, world, room_offsets[world], chr_base, tag_base, 0x1000 + 0x800 * i, 0, path_base, outro, final=(i == len(names) - 1)))
        chr_base += 0x50
        tag_base += 0x50
        path_base += 0x40
    return legs


if __name__ == "__main__":
    key = sys.argv[1]
    outdir = sys.argv[2]
    offsets = [int(x) for x in sys.argv[3].split(",")] if len(sys.argv) > 3 else [0, 0, 0]
    os.makedirs(outdir, exist_ok=True)
    legs = mission_legs(key, offsets)
    m = Merger(legs, os.path.join(outdir, "setup%s.c" % key), os.path.join(outdir, "setup%s.json" % key), key=key)
    side = m.run()
    for l in side["legs"]:
        print(l)
