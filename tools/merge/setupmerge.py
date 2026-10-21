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
  - an object a later leg places where an earlier one already did (same type, model and
    place) is never created, and the later leg's tags and links point at the earlier object;
    a lift that shares a stop with an earlier leg's lift is that lift, which gains the stops
    and doors the later leg gives it
  - lift numbers of a leg in a later world are offset, as its pads' are, so two worlds' lifts
    never share an engine slot

The result assembles with setupasm.py like any stock setup."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from merge import macros  # noqa: E402

ROOT = macros.ROOT
SETUPS = os.path.join(ROOT, "src", "setups")

# One command each: things that exist in the world whichever leg placed them. Lifts are
# matched by their stops, in merge_props. Pickups (weapons, keys, shields, mines) are not
# here: a later leg's is its own, wherever it lies.
DEDUPE_TYPES = {"door", "glass", "tinted_glass", "safe", "fan", "singlemonitor", "multimonitor",
                "autogun", "escastep", "stdobject", "hover_prop", "hovercar", "hoverbike",
                "chopper", "ammocrate", "ammocratemulti", "camera", "gasbottle", "debris"}
EXCLUDE_ORDER = ["A", "SA", "PA", "PD"]  # OBJFLAG2_EXCLUDE_<difficulty>
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
        self.lift_base = 0            # added to the leg's lift numbers (a later world's pads carry their own)
        self.outro = outro            # name of the AI list whose end_level is the success exit
        self.final = final
        self.prefix = "L%d_" % index
        # filled while merging
        self.text = None
        self.defines = {}             # original name -> value text
        self.ailist_ids = {}          # old id -> new id
        self.tag_map = {}             # old object tag -> new, dense across the legs
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
                    v = fmt(leg.tag_map.get(iv, iv + leg.tag_base))
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
                return fmt(leg.tag_map.get(v, v + leg.tag_base)) if v < 0xF0 else t
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
        # a prop assigned to a character (a guard's weapon, a keycard, a shield) carries the
        # character's number in its pad field, whatever the parameter is called
        if "flags" in params and len(args) > params.index("flags") and "OBJFLAG_ASSIGNEDTOCHR" in args[params.index("flags")]:
            for pname in ("pad", "chr"):
                if pname in params and params.index(pname) < len(args):
                    pi = params.index(pname)
                    t = args[pi].strip()
                    if is_int(t) and int(t, 0) < CHR_SPECIAL_MIN:
                        new_args[pi] = fmt(int(t, 0) + leg.chr_base)
        # stage flags address the leg's word
        if name == "set_stage_flag":
            return "set_stage_flag_w(%d, %s)" % (leg.index, new_args[0])
        if name == "unset_stage_flag":
            return "unset_stage_flag_w(%d, %s)" % (leg.index, new_args[0])
        if name == "if_stage_flag_eq":
            return "if_stage_flag_w_eq(%d, %s, %s, %s)" % (leg.index, new_args[0], new_args[1], new_args[2])
        if name == "if_all_objectives_complete":
            return "if_objectives_range_complete(%d, %d, %s)" % (leg.objective_base, leg.objective_base + leg.num_objectives - 1, new_args[0])
        if name == "activate_lift" and is_int(args[0]) and leg.lift_base:
            new_args[0] = fmt(int(args[0].strip(), 0) + leg.lift_base)
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
        """Statement-level prop macro calls in the leg's props body, in order, with raw text.
        A macro written without parentheses (endobjective, endprops) is a command too: it
        occupies a word, so the offsets between commands count it, and an objective's
        requirements run on into the next object without its endobjective."""
        body = leg.props_body
        out = []
        for start, end, name, args in find_calls(body, self.cmd_names):
            out.append((start, [name, args, body[start:end]]))
        bare = re.compile(r"(?m)^[ \t]*([a-z_][a-z0-9_]*)[ \t]*(?://.*)?$")
        for m in bare.finditer(body):
            name = m.group(1)
            if name in self.cmd_names and not self.sigs[name]:
                out.append((m.start(1), [name, [], name]))
        out.sort(key=lambda t: t[0])
        return [c for _, c in out]

    def pad_position(self, pad):
        """A pad's position in the merged world: by name, by the index the mission's symbol
        headers give the name (Area 51's three levels name one pads file), or by number (an
        index into the merged pads, already rebased). None when the token is not a pad: -1,
        an unknown name, an index past the end."""
        pad = pad.strip()
        if pad in self.pad_positions:
            return self.pad_positions[pad]
        if self.pad_by_index is None:
            from merge import setupasm
            self.pad_by_index = list(self.pad_positions.values()) if self.pad_positions else []
            self.pad_symbols = setupasm.load_enums(self.symbol_paths) if self.symbol_paths else {}
        idx = int(pad, 0) if is_int(pad) else self.pad_symbols.get(pad)
        if idx is not None and 0 <= idx < len(self.pad_by_index):
            return self.pad_by_index[idx]
        return None

    def place_of(self, pad):
        """Where a pad stands, to the decimetre; None when the token is not a pad."""
        pos = self.pad_position(pad)
        if pos is None:
            return None
        return tuple(int(round(c / 10.0)) for c in pos)

    def place_key(self, name, args):
        """What makes two structural objects the same thing: type, model and where they stand.
        Pads are compared by position, so a door that Defection and Extraction both place
        (under different pad names) is found. None for an object that stands on no pad."""
        params = self.sigs[name]
        if "pad" not in params or "model" not in params:
            return None
        place = self.place_of(args[params.index("pad")])
        if place is None:
            return None
        return (name, args[params.index("model")].strip(), place)

    def lift_stops(self, leg, li, args):
        """A lift's four stops as [place, pad token, door reference]: the place None for an
        unused stop (pad -1), the door a (leg, call) reference or None."""
        params = self.sigs["lift"]
        out = []
        for i in range(1, 5):
            padtok = args[params.index("pad%d" % i)].strip()
            doortok = args[params.index("door%d" % i)].strip()
            v = int(padtok, 0) if is_int(padtok) else None
            place = None if v is not None and v < 0 else self.place_of(padtok)
            door = (leg.index, li + int(doortok, 0)) if is_int(doortok) and int(doortok, 0) else None
            out.append([place, padtok, door])
        return out

    @staticmethod
    def lock_of(name, params, args):
        """A door's lock as the setup states it: its key bits and its locked-side flags."""
        if name != "door":
            return None
        keyflags = args[params.index("keyflags")].strip()
        locked = sorted(re.findall(r"OBJFLAG2_LOCKED(FRONT|BACK)\b", args[params.index("flags2")]))
        key = int(keyflags, 0) if is_int(keyflags) else keyflags
        return (key, tuple(locked)) if key or locked else None

    def merge_props(self):
        """One props list from the legs'. A structural object a later leg places where an
        earlier leg already placed one of the same type and model is excluded, never created,
        and the later leg's tags and links point at the earlier object, so the thing exists
        once and what happened to it carries. A lift is the same lift when it shares a stop:
        the earlier leg's lift gains the later leg's other stops and their doors, and the
        later leg's lift-door links are renumbered to the merged stops. Where the earlier
        object exists only on some difficulties, the later one is excluded on those alone."""
        calls = {leg.index: self.prop_calls(leg) for leg in self.legs}
        self.locks = []   # (leg, command index of the door, keyflags, locked-side flags) set when the leg begins
        drop = set()      # (leg, call) never emitted
        dup_of = {}       # (leg, call) of a later copy -> (leg, call) of the object kept
        placed = {}       # place key -> (leg, call)
        lifts = {}        # (leg, call) of a kept lift -> [model, stops]
        stopmap = {}      # (leg, call) of a lift copy -> {its stop: the merged lift's stop}
        for leg in self.legs:
            mine = {}
            mylifts = {}
            for li, (name, args, raw) in enumerate(calls[leg.index]):
                ref = (leg.index, li)
                if (name == "briefing" and leg.index > 0) or name == "endprops":
                    drop.add(ref)
                elif name == "lift":
                    stops = self.lift_stops(leg, li, args)
                    places = {s[0] for s in stops if s[0] is not None}
                    model = args[self.sigs["lift"].index("model")].strip()
                    kept = next((r for r, (m, ks) in lifts.items() if m == model and places & {s[0] for s in ks}), None)
                    if kept is None:
                        mylifts[ref] = [model, stops]
                        continue
                    dup_of[ref] = kept
                    kstops = lifts[kept][1]
                    smap = {}
                    for i, (place, padtok, door) in enumerate(stops):
                        if place is None:
                            continue
                        j = next((j for j, s in enumerate(kstops) if s[0] == place), None)
                        if j is None:
                            j = next((j for j, s in enumerate(kstops) if s[0] is None), None)
                            if j is None:
                                raise SystemExit("lift %s: the legs give it more than four stops" % model)
                            kstops[j] = [place, padtok, door]
                        elif kstops[j][2] is None and door is not None:
                            kstops[j][2] = door
                        smap[i] = j
                    stopmap[ref] = smap
                elif name in DEDUPE_TYPES:
                    key = self.place_key(name, args)
                    if key is None:
                        continue
                    if key in placed:
                        dup_of[ref] = placed[key]
                    elif key not in mine:
                        mine[key] = ref
            placed.update(mine)
            lifts.update(mylifts)
        # absolute command indices, the dropped commands gone
        abs_index = {}
        n = 0
        for leg in self.legs:
            for li in range(len(calls[leg.index])):
                if (leg.index, li) not in drop:
                    abs_index[(leg.index, li)] = n
                    n += 1

        def target(ref):
            return abs_index[dup_of.get(ref, ref)]

        tagged = set()
        for leg in self.legs:
            for li, (name, args, raw) in enumerate(calls[leg.index]):
                if name == "tag":
                    tagged.add((leg.index, li + int(args[1].strip(), 0)))
        merged = []
        for leg in self.legs:
            report = {"excluded": {}, "lifts": [], "differing": 0, "locks": [], "variants": 0, "tagged_variants": 0}
            for li, (name, args, raw) in enumerate(calls[leg.index]):
                ref = (leg.index, li)
                if ref in drop:
                    continue
                params = self.sigs[name]
                new_args = list(args)
                changed = False
                if ref in dup_of:
                    kleg, kli = dup_of[ref]
                    kargs = calls[kleg][kli][1]
                    fi = params.index("flags2")
                    kept_excl = set(re.findall(r"OBJFLAG2_EXCLUDE_(A|SA|PA|PD)\b", kargs[fi]))
                    need = [d for d in EXCLUDE_ORDER if d not in kept_excl]
                    new_args[fi] = "(%s) | %s" % (args[fi].strip() or "0", " | ".join("OBJFLAG2_EXCLUDE_" + d for d in need))
                    changed = True
                    report["excluded"][name] = report["excluded"].get(name, 0) + 1
                    if kept_excl:
                        report["variants"] += 1
                        if ref in tagged:
                            report["tagged_variants"] += 1
                    skip = {"pad", "pad1", "pad2", "pad3", "pad4", "door1", "door2", "door3", "door4", "flags2"}
                    if any(a.strip() != b.strip() for p, a, b in zip(params, args, kargs) if p not in skip):
                        report["differing"] += 1
                    mine_lock, kept_lock = self.lock_of(name, params, args), self.lock_of(name, params, kargs)
                    if kept_lock != mine_lock:
                        # one door, two locks: the earlier leg's until this leg begins, then this leg's
                        sides = sorted(re.findall(r"OBJFLAG2_LOCKED(FRONT|BACK)\b", args[params.index("flags2")]))
                        self.locks.append((leg.index, abs_index[(kleg, kli)], args[params.index("keyflags")].strip(),
                                           " | ".join("OBJFLAG2_LOCKED" + s for s in sides) or "0"))
                        report["locks"].append([args[params.index("model")].strip(), list(self.place_of(args[params.index("pad")])),
                                                self.legs[kleg].name, kept_lock, mine_lock])
                if name == "tag":
                    new_args[1] = str(target((leg.index, li + int(args[1].strip(), 0))) - abs_index[ref])
                    changed = True
                elif name == "lift" and ref in lifts:
                    model, stops = lifts[ref]
                    for i, (place, padtok, door) in enumerate(stops):
                        new_args[params.index("pad%d" % (i + 1))] = padtok
                        new_args[params.index("door%d" % (i + 1))] = str(target(door) - abs_index[ref]) if door else "0"
                    changed = True
                    if any(s[0] is not None for s in stops) and len([s for s in stops if s[0] is not None]) > len([s for s in self.lift_stops(leg, li, args) if s[0] is not None]):
                        report["lifts"].append([model, [s[0] for s in stops if s[0] is not None]])
                else:
                    for pi, p in enumerate(params):
                        if macros.kind_of(name, p) == "offset" and is_int(args[pi]):
                            off = int(args[pi].strip(), 0)
                            if off:
                                new_args[pi] = str(target((leg.index, li + off)) - abs_index[ref])
                                changed = True
                    if name == "lift_door":
                        lref = (leg.index, li + int(args[1].strip(), 0))
                        if lref in stopmap:
                            new_args[2] = str(stopmap[lref][int(args[2].strip(), 0)])
                            changed = True
                if changed:
                    raw = "%s(%s)" % (name, ", ".join(a.strip() for a in new_args))
                merged.append("\t" + raw)
            leg.excluded_count = sum(report["excluded"].values())
            leg.dedup = report
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
                "dedup": getattr(leg, "dedup", {}),
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
        # a door two legs place is one door; each leg locks it as its own setup says, from its joint
        locks = ["\t{ %d, %d, %s, %s }," % l for l in getattr(self, "locks", [])] + ["\t{ -1, 0, 0, 0 },"]
        text += "static const struct campaignlock g_CampaignLocks_%s[] = {\n%s\n};\n" % (key, "\n".join(locks))
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
        # tag ids: an AI command names an object by one byte, so the legs' tags are renumbered
        # densely, each leg's in order after the one before, and the whole mission stays
        # below the engine's special values
        base = 0
        for leg in self.legs:
            ids = {int(m.group(1), 0) for m in re.finditer(r"(?m)^\s*tag\((0x[0-9a-fA-F]+|\d+),", leg.text)}
            ids |= {int(v, 0) for n, v in leg.defines.items() if n.startswith("OBJ_") and is_int(v)}
            leg.tag_base = base
            leg.tag_map = {old: base + i for i, old in enumerate(sorted(ids))}
            base += len(ids)
        if base > 0xF0:
            raise SystemExit("%d object tags across the legs; AI commands can name at most 0xF0" % base)
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
