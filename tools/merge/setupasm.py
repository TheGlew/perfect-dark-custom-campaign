"""Assemble a setup from its C source into the N64 setup file the port loads.

The C source is run through the C preprocessor with the game's headers, which turns every
macro into integer expressions. What remains is a handful of arrays whose elements are C
constant expressions, plus enum names for pads (PAD_*) and text (L_*) that the preprocessor
leaves alone. Those expressions are evaluated here and laid out as the engine expects
(see setupbin.py). The result for a stock setup equals the file in the ROM in every
structure the engine reads, which test_setupasm.py checks.

Usage: setupasm.py <setup.c> <out.bin> [--pads-header h] [--include dir]... [-D NAME=VAL]...
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from merge import setupbin  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GENERATED = os.path.join(ROOT, "build", "src", "generated", "ntsc-final")
INCLUDES = [
    os.path.join(ROOT, "src", "include"),
    os.path.join(ROOT, "include"),
    os.path.join(ROOT, "include", "PR"),
    os.path.join(ROOT, "port", "include"),
    GENERATED,
    os.path.join(ROOT, "src", "generated", "ntsc-final"),
]


def preprocess(path, extra_includes=(), defines=()):
    gcc = "gcc"
    for cand in (r"C:\msys64\mingw64\bin\gcc.exe",):
        if os.path.exists(cand):
            gcc = cand
    args = [gcc, "-E", "-P", "-DVERSION=2", "-DPLATFORM_64BIT"]
    for inc in list(INCLUDES) + list(extra_includes):
        args += ["-I", inc]
    for d in defines:
        args += ["-D" + d]
    args.append(path)
    env = dict(os.environ)
    bindir = os.path.dirname(gcc)
    if bindir:
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")  # the compiler's own DLLs
    res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if res.returncode != 0:
        raise RuntimeError("preprocessing failed:\n" + res.stderr[:2000])
    return res.stdout


def load_enums_from_text(text, table=None):
    """Enum names -> values from every enum definition in a preprocessed translation unit
    (the generated pad and text headers, the weapon and model enums, and so on). Assigned
    values may be expressions over earlier names."""
    table = {} if table is None else table
    for em in re.finditer(r"\benum\s*\w*\s*\{([^}]*)\}", text):
        value = 0
        for item in em.group(1).split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                name, v = item.split("=", 1)
                name = name.strip()
                try:
                    value = eval_expr(v.strip(), table)
                except KeyError:
                    continue
            else:
                name = item
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                table[name] = value
            value += 1
    return table


def load_enums(paths, table=None):
    table = {} if table is None else table
    for p in paths:
        load_enums_from_text(open(p, encoding="utf-8", errors="replace").read(), table)
    return table


def default_symbol_paths():
    paths = []
    lang = os.path.join(GENERATED, "lang")
    if os.path.isdir(lang):
        paths += [os.path.join(lang, f) for f in os.listdir(lang) if f.endswith(".h")]
    pads = os.path.join(ROOT, "src", "generated", "ntsc-final", "pads")
    if os.path.isdir(pads):
        paths += [os.path.join(pads, f) for f in os.listdir(pads) if f.endswith(".h")]
    return paths


_TOKEN = re.compile(r"""
    (?P<float>\d+\.\d*(?:[eE][-+]?\d+)?[fF]?|\.\d+(?:[eE][-+]?\d+)?[fF]?|\d+[eE][-+]?\d+[fF]?|\d+[fF])
  | (?P<int>0[xX][0-9a-fA-F]+[uUlL]*|\d+[uUlL]*)
  | (?P<char>'(?:\\.|[^'\\])')
  | (?P<ident>[A-Za-z_]\w*)
  | (?P<op><<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^~!<>?:(),])
  | (?P<ws>\s+)
""", re.X)

_CASTS = {"u8", "s8", "u16", "s16", "u32", "s32", "f32", "f64", "int", "char", "short", "long", "unsigned", "float", "double", "void"}


class _Expr:
    """A recursive-descent evaluator for C constant expressions: numbers, chars, enum names,
    casts to integer or float types, unary - + ~ !, the binary operators with C precedence,
    and the ternary. Integer division truncates toward zero as in C."""

    def __init__(self, text, symbols):
        self.toks = []
        for m in _TOKEN.finditer(text):
            kind = m.lastgroup
            if kind == "ws":
                continue
            self.toks.append((kind, m.group(0)))
        self.pos = 0
        self.symbols = symbols

    def peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def take(self, value=None):
        kind, tok = self.peek()
        if value is not None and tok != value:
            raise SyntaxError("expected %r at %r" % (value, self.toks[self.pos:self.pos + 4]))
        self.pos += 1
        return kind, tok

    def parse(self):
        v = self.ternary()
        if self.pos != len(self.toks):
            raise SyntaxError("trailing tokens %r" % (self.toks[self.pos:self.pos + 4],))
        return v

    def ternary(self):
        c = self.binary(0)
        if self.peek()[1] == "?":
            self.take()
            a = self.ternary()
            self.take(":")
            b = self.ternary()
            return a if c else b
        return c

    _PREC = [("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("<<", ">>"), ("+", "-"), ("*", "/", "%")]

    def binary(self, level):
        if level == len(self._PREC):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek()[1] in self._PREC[level]:
            op = self.take()[1]
            right = self.binary(level + 1)
            left = self.apply(op, left, right)
        return left

    @staticmethod
    def apply(op, a, b):
        if op == "||":
            return 1 if (a or b) else 0
        if op == "&&":
            return 1 if (a and b) else 0
        if op == "|":
            return int(a) | int(b)
        if op == "^":
            return int(a) ^ int(b)
        if op == "&":
            return int(a) & int(b)
        if op == "==":
            return 1 if a == b else 0
        if op == "!=":
            return 1 if a != b else 0
        if op == "<":
            return 1 if a < b else 0
        if op == ">":
            return 1 if a > b else 0
        if op == "<=":
            return 1 if a <= b else 0
        if op == ">=":
            return 1 if a >= b else 0
        if op == "<<":
            return int(a) << int(b)
        if op == ">>":
            return int(a) >> int(b)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if isinstance(a, float) or isinstance(b, float):
                return a / b
            q = abs(a) // abs(b)
            return q if (a >= 0) == (b >= 0) else -q
        if op == "%":
            return a - b * _Expr.apply("/", a, b)
        raise SyntaxError(op)

    def unary(self):
        kind, tok = self.peek()
        if tok == "-":
            self.take()
            return -self.unary()
        if tok == "+":
            self.take()
            return self.unary()
        if tok == "~":
            self.take()
            return ~int(self.unary())
        if tok == "!":
            self.take()
            return 0 if self.unary() else 1
        if tok == "(":
            # a cast, or a parenthesised expression
            save = self.pos
            self.take()
            k2, t2 = self.peek()
            if k2 == "ident" and t2 in _CASTS:
                words = []
                while self.peek()[1] != ")" and self.peek()[1] != "*":
                    words.append(self.take()[1])
                pointer = False
                while self.peek()[1] == "*":
                    self.take()
                    pointer = True
                self.take(")")
                v = self.unary()
                if pointer:
                    return int(v)
                if "f32" in words or "float" in words or "f64" in words or "double" in words:
                    return float(v)
                return int(v)
            self.pos = save
            self.take("(")
            v = self.ternary()
            self.take(")")
            return v
        return self.primary()

    def primary(self):
        kind, tok = self.take()
        if kind == "int":
            return int(tok.rstrip("uUlL"), 0)
        if kind == "float":
            return float(tok.rstrip("fF"))
        if kind == "char":
            body = tok[1:-1]
            if body.startswith("\\"):
                return ord(bytes(body, "utf-8").decode("unicode_escape"))
            return ord(body)
        if kind == "ident":
            if tok == "NULL":
                return 0
            if tok in self.symbols:
                return self.symbols[tok]
            raise KeyError(tok)
        raise SyntaxError("unexpected %r" % (tok,))


def eval_expr(expr, symbols):
    v = _Expr(expr, symbols).parse()
    if isinstance(v, float):
        v = int(v)
    return v


def to_s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def split_elements(body):
    """Split an initializer body on commas at brace/paren depth 0, dropping empties."""
    out = []
    depth = 0
    cur = []
    for ch in body:
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        if ch == "," and depth == 0:
            s = "".join(cur).strip()
            if s:
                out.append(s)
            cur = []
        else:
            cur.append(ch)
    s = "".join(cur).strip()
    if s:
        out.append(s)
    return out


_DECL = re.compile(r"^(u8|u32|s32|struct ailist|struct path|struct stagesetup)\s+(\w+)(\[\])?\s*=\s*\{", re.M)


def parse_declarations(text):
    """Return {name: (kind, body)} for every top-level initialised array/struct after the setup struct."""
    start = text.find("struct stagesetup setup")
    if start < 0:
        raise ValueError("no setup struct")
    decls = {}
    for m in _DECL.finditer(text, start):
        kind, name = m.group(1), m.group(2)
        # find the matching close brace
        i = m.end()
        depth = 1
        while depth:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        body = text[m.end():i - 1]
        decls[name] = (kind, body)
    return decls


def assemble_text(text, symbols):
    decls = parse_declarations(text)
    s = setupbin.Setup()
    # arrays referenced by name
    kind, body = decls["props"]
    s.props = [eval_expr(e, symbols) & 0xFFFFFFFF for e in split_elements(body)]
    kind, body = decls["intro"]
    s.intro = [to_s32(eval_expr(e, symbols)) for e in split_elements(body)]
    # paths
    kind, body = decls["paths"]
    for entry in split_elements(body):
        inner = entry.strip()
        if not inner.startswith("{"):
            continue
        fields = split_elements(inner[1:-1])
        padsname = fields[0].strip()
        if padsname in ("((void *)0)", "0", "NULL"):
            continue
        pid = eval_expr(fields[1], symbols)
        flags = eval_expr(fields[2], symbols) if len(fields) > 2 else 0
        pkind, pbody = decls[padsname]
        pads = [eval_expr(e, symbols) for e in split_elements(pbody)]
        if pads and pads[-1] == -1:
            pads = pads[:-1]
        s.paths.append((pid, flags, 0, pads))  # the length field is 0 in the file; the engine fills it
    # ai lists
    kind, body = decls["ailists"]
    for entry in split_elements(body):
        inner = entry.strip()
        if not inner.startswith("{"):
            continue
        fields = split_elements(inner[1:-1])
        fname = fields[0].strip()
        if fname in ("((void *)0)", "0", "NULL"):
            continue
        lid = eval_expr(fields[1], symbols)
        fkind, fbody = decls[fname]
        lst = bytes(eval_expr(e, symbols) & 0xFF for e in split_elements(fbody))
        s.ailists.append((lid, lst))
    return s


def assemble_file(path, extra_includes=(), defines=(), symbol_paths=None):
    """The enums the headers bring into the translation unit resolve first; the symbol headers
    given explicitly (a merged mission's pads) override them."""
    text = preprocess(path, extra_includes, defines)
    symbols = load_enums_from_text(text, {})
    load_enums(symbol_paths if symbol_paths is not None else default_symbol_paths(), symbols)
    return assemble_text(text, symbols)


if __name__ == "__main__":
    args = sys.argv[1:]
    includes = []
    defines = []
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--include":
            includes.append(args[i + 1]); i += 2
        elif a == "-D":
            defines.append(args[i + 1]); i += 2
        else:
            pos.append(a); i += 1
    src, out = pos[0], pos[1]
    setup = assemble_file(src, includes, defines)
    open(out, "wb").write(setup.write())
    print("wrote", out, "props", len(setup.props), "intro", len(setup.intro), "paths", len(setup.paths), "ailists", len(setup.ailists))
