"""Recursive-descent parser following the EBNF of section 24.

Each method is named after the grammar rule it implements. Precedence, from
lowest to highest: `->`, `|>`, `or`, `and`, `not`, comparisons, `??`, `..`,
`+ -`, `* / // %`, `**`, unary `-`, member access and call.
"""
from __future__ import annotations

from . import ast as A
from . import errors
from .errors import NexoError, Span
from .lexer import Interp, Token, tokenize, tokenize_expr

STEP_WORDS = {"where", "map", "pick", "group", "sum", "count", "avg", "sort", "take", "each"}
ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=")
COMPARE_OPS = ("==", "!=", "<", ">", "<=", ">=")

# Descriptions used in "Unexpected X here. Expected Y."
WORDS = {
    "NEWLINE": {"es": "el fin de la línea", "en": "the end of the line"},
    "EOF": {"es": "el final del archivo", "en": "the end of the file"},
    "INDENT": {"es": "una indentación", "en": "an indentation"},
    "DEDENT": {"es": "el final del bloque", "en": "the end of the block"},
    "STRING": {"es": "un texto", "en": "a text"},
    "expr": {"es": "una expresión", "en": "an expression"},
    "name": {"es": "un nombre", "en": "a name"},
    "type": {"es": "un tipo", "en": "a type"},
    "newline": {"es": "el fin de la línea", "en": "the end of the line"},
    "block": {"es": '":" y un bloque indentado en la línea siguiente',
              "en": '":" and an indented block on the next line'},
    "indented": {"es": "un bloque indentado 4 espacios", "en": "a block indented 4 spaces"},
    "pattern": {"es": "un patrón (_, un literal o un nombre)", "en": "a pattern (_, a literal or a name)"},
    "else": {"es": "else e: después del try", "en": "else e: after the try"},
    "by": {"es": '"by"', "en": '"by"'},
    "string": {"es": "un texto entre comillas", "en": "a quoted text"},
    "member": {"es": "el nombre de un campo o método", "en": "the name of a field or method"},
    "call": {"es": "una llamada, por ejemplo nombre(...)", "en": "a call, for example name(...)"},
    "route": {"es": "una ruta: get, post, put, patch o delete y su camino", "en": "a route: get, post, put, patch or delete and its path"},
}


def word(key: str) -> str:
    return WORDS[key][errors.LANG]


def describe(tok: Token) -> str:
    if tok.kind in WORDS:
        return word(tok.kind)
    if tok.kind in ("INT", "FLOAT"):
        return f'"{tok.value}"'
    if tok.kind == "UNIT":
        text, _f, unit = tok.value
        return f'"{text}{unit}"' if unit in ("%",) else f'"{text} {unit}"'
    return f'"{tok.value}"'


class Parser:
    def __init__(self, tokens: list[Token], file: str, in_text: bool = False):
        self.in_text = in_text  # parsing an {expr} inside a text literal
        self.toks = tokens
        self.p = 0
        self.file = file
        self.block_end = -1  # token index right after an expression that ended in a block
        # after `use nexo.sync`, spawn, select and shared start statements
        self.sync = False
        # `use nexo.db` makes connect and @table available
        self.db = False
        # statements a module block or annotation desugars into, after the
        # one statement() returned (D90)
        self.extra: list[A.Stmt] = []
        self.in_test = False
        self.in_transaction = 0

    # -- token helpers ---------------------------------------------------
    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.p + k, len(self.toks) - 1)]

    def prev(self) -> Token:
        return self.toks[self.p - 1]

    def advance(self) -> Token:
        tok = self.toks[self.p]
        if tok.kind != "EOF":
            self.p += 1
        return tok

    def at_op(self, *ops: str) -> bool:
        return self.peek().is_op(*ops)

    def at_kw(self, *words: str) -> bool:
        return self.peek().is_kw(*words)

    def at_name(self, value: str | None = None) -> bool:
        tok = self.peek()
        return tok.kind == "NAME" and (value is None or tok.value == value)

    def error(self, expected: str, tok: Token | None = None) -> NexoError:
        tok = tok or self.peek()
        if self.in_text and tok.kind == "EOF":
            return NexoError("unexpected", tok.span, fix="expected", got='"}"', expected=expected)
        if expected == word("name") and tok.kind == "KEYWORD":
            return NexoError("reserved_word", tok.span, word=tok.value)
        return NexoError("unexpected", tok.span, fix="expected", got=describe(tok), expected=expected)

    def expect_op(self, op: str) -> Token:
        if not self.at_op(op):
            raise self.error(f'"{op}"')
        return self.advance()

    def expect_kw(self, kw: str) -> Token:
        if not self.at_kw(kw):
            raise self.error(f'"{kw}"')
        return self.advance()

    def expect_name(self) -> Token:
        if self.peek().kind != "NAME":
            raise self.error(word("name"))
        return self.advance()

    def expect_newline(self) -> None:
        if self.p == self.block_end:
            return
        if self.peek().kind != "NEWLINE":
            raise self.error(word("newline"))
        self.advance()

    def span_from(self, start: Token | Span) -> Span:
        s = start.span if isinstance(start, Token) else start
        return s.to(self.prev().span)

    # -- program and blocks ---------------------------------------------
    def program(self) -> A.Program:
        start = self.peek()
        body = []
        while self.peek().kind != "EOF":
            if self.peek().kind == "INDENT":
                raise NexoError("dedent_mismatch", self.peek().span)
            self.statement_into(body)
        program = A.Program(start.span, body)
        model_aliases(program)
        return program

    def statement_into(self, body: list) -> None:
        body.append(self.statement())
        if self.extra:
            body.extend(self.extra)
            self.extra = []

    def block(self) -> list[A.Stmt]:
        """block = ":" NEWLINE INDENT statement { statement } DEDENT"""
        if not self.at_op(":"):
            raise self.error(word("block"))
        self.advance()
        return self.indented_body()

    def indented_body(self) -> list[A.Stmt]:
        if self.peek().kind != "NEWLINE":
            raise self.error(word("block"))
        self.advance()
        if self.peek().kind != "INDENT":
            raise self.error(word("indented"))
        self.advance()
        body: list = []
        while self.peek().kind != "DEDENT":
            self.statement_into(body)
        self.advance()
        return body

    # -- statements -----------------------------------------------------
    def statement(self) -> A.Stmt:
        tok = self.peek()
        if tok.is_op("@"):
            annotations = self.annotations()
            if self.at_kw("type") or (self.at_kw("pub") and self.peek(1).is_kw("type")):
                return self.table_def(self.type_def(annotations))
            return self.tool_def(self.fn_def(annotations))
        if self.in_test and tok.kind == "NAME" and tok.value == "fake" and self.peek(1).kind == "NAME":
            return self.fake_stmt()
        if tok.kind == "NAME" and self.at_transaction():
            return self.transaction()
        if tok.kind == "KEYWORD":
            kw = tok.value
            if kw == "fn":
                return self.fn_def([])
            if kw == "pub":
                if self.peek(1).is_kw("type"):
                    return self.table_def(self.type_def([]))
                return self.fn_def([])
            if kw == "type":
                return self.table_def(self.type_def([]))
            if kw == "trait":
                return self.trait_def()
            if kw == "if":
                return self.if_stmt()
            if kw == "for":
                return self.for_stmt()
            if kw == "while":
                self.advance()
                cond = self.expr()
                return A.While(tok.span, cond, self.block())
            if kw == "repeat":
                self.advance()
                count = self.expr()
                return A.Repeat(tok.span, count, self.block())
            if kw == "match":
                return self.match_stmt()
            if kw == "try":
                return self.try_stmt()
            if kw == "parallel" and self.peek(1).is_op(":"):
                self.advance()
                return A.ParallelBlock(tok.span, self.block())
            if kw == "test":
                self.advance()
                if self.peek().kind != "STRING" or any(not isinstance(p, str) for p in self.peek().value):
                    raise self.error(word("string"))
                name = "".join(self.advance().value)
                self.in_test = True
                try:
                    return A.TestDef(tok.span, name, self.block())
                finally:
                    self.in_test = False
            if kw in ("on", "when", "every", "lifecycle"):
                return self.event_stmt()
        if self.sync and tok.kind == "NAME" and tok.value == "select" and self.peek(1).is_op(":"):
            return self.select_stmt()
        stmt = self.simple_stmt()
        self.expect_newline()
        return stmt

    def simple_stmt(self) -> A.Stmt:
        tok = self.peek()
        if tok.is_kw("pass"):
            self.advance()
            return A.Pass(tok.span)
        if tok.is_kw("var"):
            self.advance()
            name = self.expect_name()
            type_ = None
            if self.at_op(":"):
                self.advance()
                type_ = self.type_()
            self.expect_op("=")
            value = self.expr()
            return A.VarDecl(self.span_from(tok), name.value, name.span, type_, value)
        if tok.is_kw("return"):
            self.advance()
            if self.peek().kind in ("NEWLINE", "DEDENT", "EOF"):
                return A.Return(tok.span, None)
            value = self.expr()
            return A.Return(self.span_from(tok), value)
        if tok.is_kw("use"):
            return self.use_stmt()
        if self.sync and tok.kind == "NAME" and tok.value == "spawn" and self.peek(1).kind == "NAME":
            self.advance()
            call = self.postfix()
            if not isinstance(call, A.Call):
                raise self.error(word("call"), tok)
            return A.Spawn(self.span_from(tok), call)
        if (self.sync and tok.kind == "NAME" and tok.value == "shared" and self.peek(1).kind == "NAME"
                and self.peek(2).is_op("=")):
            self.advance()
            name = self.expect_name()
            self.expect_op("=")
            return A.SharedDecl(self.span_from(tok), name.value, name.span, self.expr())
        if tok.is_kw("emit"):
            self.advance()
            call = self.postfix()
            if not isinstance(call, A.Call):
                raise self.error(word("call"), tok)
            return A.Emit(self.span_from(tok), call)
        if tok.is_kw("expect"):
            self.advance()
            return A.Expect(self.span_from(tok), self.expr())
        if tok.is_kw("ensure"):
            self.advance()
            return A.Ensure(self.span_from(tok), self.expr())
        if tok.is_kw("assert"):
            self.advance()
            expr = self.expr()
            fails = False
            if self.at_name("fails"):
                self.advance()
                fails = True
            return A.Assert(self.span_from(tok), expr, fails)
        if self.is_config_block():
            return self.config_block()
        # assignment or expression statement
        target = self.expr()
        if self.p == self.block_end:
            return A.ExprStmt(target.span, target)
        type_ = None
        if self.at_op(":") and isinstance(target, A.Name):
            self.advance()
            type_ = self.type_()
            if not self.at_op("="):
                raise self.error('"="')
        if self.at_op(*ASSIGN_OPS):
            op = self.advance().value
            if not isinstance(target, (A.Name, A.Attr, A.Index)) or not valid_target(target):
                raise NexoError("invalid_target", target.span)
            if type_ is not None and op != "=":
                raise self.error('"="', self.prev())
            value = self.expr()
            return A.Assign(target.span, target, op, value, type_)
        return A.ExprStmt(target.span, target)

    def is_config_block(self) -> bool:
        if self.peek().kind != "NAME":
            return False
        if self.peek(1).is_op(":") and self.peek(2).kind == "NEWLINE":
            return True
        if (self.peek().value == "ai" and self.at_name_at(1, "profile") and self.peek(2).kind == "STRING"
                and self.peek(3).is_op(":")):
            return True
        return (self.peek(1).kind in ("NAME", "STRING") and self.peek(2).is_op(":")
                and self.peek(3).kind == "NEWLINE")

    def at_name_at(self, k: int, value: str) -> bool:
        tok = self.peek(k)
        return tok.kind == "NAME" and tok.value == value

    def config_block(self) -> A.ConfigBlock:
        """config_block = IDENT [ IDENT | STRING ] ":" NEWLINE INDENT {...} DEDENT

        Module blocks (ai, infra, pipeline...) are outside phase 0, so the
        body is only skipped; the interpreter reports them as unavailable."""
        tok = self.advance()
        name = None
        if tok.value in MODULE_BLOCKS:
            return self.module_block(tok)
        if not self.at_op(":"):
            n = self.advance()
            name = n.value if n.kind == "NAME" else "".join(p for p in n.value if isinstance(p, str))
        self.expect_op(":")
        self.expect_newline_raw()
        if self.peek().kind != "INDENT":
            raise self.error(word("indented"))
        depth = 0
        while True:
            t = self.advance()
            if t.kind == "INDENT":
                depth += 1
            elif t.kind == "DEDENT":
                depth -= 1
                if depth == 0:
                    break
            elif t.kind == "EOF":
                break
        self.block_end = self.p
        return A.ConfigBlock(tok.span, tok.value, name)

    def expect_newline_raw(self) -> None:
        if self.peek().kind != "NEWLINE":
            raise self.error(word("newline"))
        self.advance()

    def use_stmt(self) -> A.Use:
        """use_stmt = "use" [ IDENT ] dotted_name [ "as" IDENT ], and
        `use c "library"` (section 18, D103)"""
        tok = self.advance()
        first_tok = self.expect_name()
        first = first_tok.value
        lang = None
        if self.peek().kind == "NAME" or (first == "c" and self.peek().kind == "STRING"):
            lang, first = first, None
        if lang == "c":
            if self.peek().kind != "STRING" or any(not isinstance(p, str) for p in self.peek().value):
                raise self.error(word("string"))
            name = "".join(self.advance().value)
        else:
            name = first if first is not None else self.dotted_name()
            if first is not None:
                while self.at_op("."):
                    self.advance()
                    name += "." + self.expect_name().value
        alias = None
        alias_span = None
        if self.at_kw("as"):
            self.advance()
            a = self.expect_name()
            alias, alias_span = a.value, a.span
        span = self.span_from(tok)
        if lang is None and name == "nexo.sync":
            self.sync = True
        if lang is None and name == "nexo.db":
            self.db = True
        if lang in ("c", "python"):
            bound = alias or (name.rsplit(".", 1)[-1] if lang == "python" else name)
            if not is_identifier(bound):
                raise NexoError("use_needs_as", span, name=name)
            fn = "__python_import" if lang == "python" else "__c_library"
            call = A.Call(span, A.Name(span, fn), [A.Arg(span, None, A.Str(span, [name]))])
            self.extra.append(A.Assign(alias_span or span, A.Name(alias_span or span, bound), "=", call))
        return A.Use(span, lang, name, alias)

    def select_stmt(self) -> A.Select:
        """select: arms `NAME from expr: ...` and `after expr: ...` (nexo.sync)"""
        start = self.advance()
        self.expect_op(":")
        self.expect_newline_raw()
        if self.peek().kind != "INDENT":
            raise self.error(word("indented"))
        self.advance()
        arms = []
        while self.peek().kind != "DEDENT":
            tok = self.peek()
            var = channel = after = None
            if tok.kind == "NAME" and tok.value == "after" and not (self.peek(1).kind == "NAME" and self.peek(1).value == "from"):
                self.advance()
                after = self.expr()
            else:
                var = self.expect_name().value
                if not self.at_name("from"):
                    raise self.error('"from"')
                self.advance()
                channel = self.expr()
            self.expect_op(":")
            if self.peek().kind == "NEWLINE":
                body = self.indented_body()
            else:
                body = [self.simple_stmt()]
                self.expect_newline()
            arms.append(A.SelectArm(tok.span, var, channel, after, body))
        self.advance()
        return A.Select(start.span, arms)

    def dotted_name(self) -> str:
        name = self.expect_name().value
        while self.at_op("."):
            self.advance()
            name += "." + self.expect_name().value
        return name

    def annotations(self) -> list[A.Annotation]:
        """annotation = "@" dotted_name [ "(" [ args ] ")" ] NEWLINE; also
        `@tool "description"` (section 8) and rates such as
        `@limit(10 per minute per ip)` (D93)"""
        result = []
        while self.at_op("@"):
            tok = self.advance()
            name = self.dotted_name()
            args = []
            if self.at_op("("):
                self.advance()
                args = [A.Arg(a.span, a.name, self.rate(a.value)) for a in self.args(")")]
                self.expect_op(")")
            elif self.peek().kind == "STRING":
                value = self.primary()
                args = [A.Arg(value.span, None, value)]
            result.append(A.Annotation(self.span_from(tok), name, args))
            self.expect_newline()
        return result

    def rate(self, value: A.Expr) -> A.Expr:
        """`10 per minute per ip` -> __rate(10, "minute", "ip")"""
        if not self.at_name("per"):
            return value
        args = [A.Arg(value.span, None, value)]
        while self.at_name("per"):
            self.advance()
            w = self.expect_name()
            args.append(A.Arg(w.span, None, A.Str(w.span, [w.value])))
        span = self.span_from(value.span)
        return A.Call(span, A.Name(span, "__rate"), args)

    def fn_def(self, annotations: list[A.Annotation]) -> A.FnDef:
        """fn_def = { annotation } [ "pub" ] "fn" IDENT "(" [ params ] ")" [ "->" type ] block"""
        start = self.peek()
        pub = False
        if self.at_kw("pub"):
            self.advance()
            pub = True
        self.expect_kw("fn")
        name = self.expect_name()
        params = self.params()
        ret = None
        if self.at_op("->"):
            self.advance()
            ret = self.type_()
        body = self.block()
        return A.FnDef(start.span, name.value, name.span, params, ret, body, pub, annotations)

    def params(self) -> list[A.Param]:
        self.expect_op("(")
        params = []
        if not self.at_op(")"):
            params.append(self.param())
            while self.at_op(","):
                self.advance()
                params.append(self.param())
        self.expect_op(")")
        check_unique([(p.name, p.span) for p in params])
        return params

    def param(self) -> A.Param:
        """param = IDENT [ ":" type ] [ "=" expr ]"""
        if self.at_kw("self"):
            tok = self.advance()
        else:
            tok = self.expect_name()
        type_ = default = None
        if self.at_op(":"):
            self.advance()
            type_ = self.type_()
        if self.at_op("="):
            self.advance()
            default = self.expr()
        return A.Param(tok.span, tok.value, type_, default)

    def type_def(self, annotations: list[A.Annotation]) -> A.TypeDef:
        """type_def = { annotation } [ "pub" ] "type" IDENT [ type_params ]
                      ( "=" type NEWLINE | [ "is" dotted_name ] type_body )"""
        start = self.peek()
        pub = False
        if self.at_kw("pub"):
            self.advance()
            pub = True
        self.expect_kw("type")
        name = self.expect_name()
        node = A.TypeDef(start.span, name.value, name.span, pub=pub, annotations=annotations)
        if self.at_op("["):
            self.type_params()
        if self.at_op("="):
            self.advance()
            node.alias = self.type_()
            self.expect_newline()
            return node
        if self.at_kw("is"):
            self.advance()
            node.trait = self.dotted_name()
        self.type_body(node)
        return node

    def type_params(self) -> list[A.TypeExpr]:
        self.expect_op("[")
        params = [self.type_()]
        while self.at_op(","):
            self.advance()
            params.append(self.type_())
        self.expect_op("]")
        return params

    def type_body(self, node: A.TypeDef) -> None:
        """type_body = ":" NEWLINE INDENT { field | variant | fn_def | config_pair } DEDENT"""
        self.expect_op(":")
        self.expect_newline_raw()
        if self.peek().kind != "INDENT":
            raise self.error(word("indented"))
        self.advance()
        while self.peek().kind != "DEDENT":
            tok = self.peek()
            if tok.is_op("@"):
                node.methods.append(self.fn_def(self.annotations()))
            elif tok.is_kw("fn", "pub"):
                node.methods.append(self.fn_def([]))
            elif tok.kind == "NAME" and self.peek(1).is_op(":"):
                self.advance()
                self.advance()
                type_ = self.type_(field=True)
                default = None
                if self.at_op("="):
                    self.advance()
                    default = self.expr()
                marks = []
                while self.at_op("@"):  # annotation_inline, e.g. @private
                    self.advance()
                    marks.append(self.dotted_name())
                    if self.at_op("("):
                        self.advance()
                        self.args(")")
                        self.expect_op(")")
                self.expect_newline()
                node.fields.append(A.FieldDef(tok.span, tok.value, type_, default, marks))
            elif tok.kind == "NAME" and self.peek(1).is_op("="):
                self.advance()
                self.advance()
                node.settings.append((tok.value, self.expr()))
                self.expect_newline()
            elif tok.kind == "NAME":
                self.advance()
                params = self.params() if self.at_op("(") else []
                self.expect_newline()
                node.variants.append(A.VariantDef(self.span_from(tok), tok.value, params))
            else:
                raise self.error(word("name"))
        self.advance()
        check_unique([(f.name, f.span) for f in node.fields] + [(v.name, v.span) for v in node.variants]
                     + [(m.name, m.name_span) for m in node.methods])
        if node.fields and node.variants:
            raise NexoError("type_mixed", node.name_span, name=node.name)

    def trait_def(self) -> A.TraitDef:
        """trait_def = "trait" IDENT ":" NEWLINE INDENT { fn_sig NEWLINE } DEDENT"""
        start = self.advance()
        name = self.expect_name()
        self.expect_op(":")
        self.expect_newline_raw()
        if self.peek().kind != "INDENT":
            raise self.error(word("indented"))
        self.advance()
        sigs = []
        while self.peek().kind != "DEDENT":
            tok = self.expect_kw("fn")
            fname = self.expect_name()
            params = self.params()
            ret = None
            if self.at_op("->"):
                self.advance()
                ret = self.type_()
            self.expect_newline()
            sigs.append(A.FnSig(tok.span, fname.value, params, ret))
        self.advance()
        return A.TraitDef(start.span, name.value, sigs)

    def type_(self, field: bool = False) -> A.TypeExpr:
        """type = base_type [ "?" | "!" ] | literal { "|" literal }; also
        `1..5` (an int in a range, section 8) and, in fields, `ref T` and
        `many T` (nexo.db, section 6)"""
        tok = self.peek()
        if tok.kind == "INT" and self.peek(1).is_op(".."):
            low = int(self.advance().value)
            self.advance()
            if self.peek().kind != "INT":
                raise self.error(word("expr"))
            high = int(self.advance().value)
            return A.RangeType(self.span_from(tok), low, high)
        if (field and tok.kind == "NAME" and tok.value in ("ref", "many") and self.peek(1).kind == "NAME"):
            self.advance()
            inner = self.type_()
            if not isinstance(inner, A.TypeName):
                raise self.error(word("type"))
            inner.rel = tok.value
            inner.span = self.span_from(tok)
            return inner
        if tok.kind in ("STRING", "INT", "FLOAT", "UNIT") or tok.is_kw("true", "false", "none"):
            literals = [self.literal()]
            while self.at_op("|"):
                self.advance()
                literals.append(self.literal())
            return A.LiteralUnion(self.span_from(tok), literals)
        node = self.base_type()
        if self.at_op("?"):
            self.advance()
            node.optional = True
        elif self.at_op("!"):
            self.advance()
            node.fallible = True
        node.span = self.span_from(tok)
        return node

    def base_type(self) -> A.TypeName:
        """base_type = dotted_name [ type_params ] | "weak" base_type"""
        tok = self.peek()
        if tok.is_kw("weak"):
            self.advance()
            inner = self.base_type()
            inner.weak = True
            return inner
        if tok.kind != "NAME":
            raise self.error(word("type"))
        name = self.dotted_name()
        params = self.type_params() if self.at_op("[") else []
        return A.TypeName(self.span_from(tok), name, params)

    def literal(self) -> A.Expr:
        tok = self.peek()
        if tok.kind in ("STRING", "INT", "FLOAT", "UNIT") or tok.is_kw("true", "false", "none"):
            return self.primary()
        raise self.error(word("expr"))

    def if_stmt(self) -> A.If:
        start = self.advance()
        branches = [(self.expr(), self.block())]
        while self.at_kw("elif"):
            self.advance()
            branches.append((self.expr(), self.block()))
        orelse = None
        if self.at_kw("else"):
            self.advance()
            orelse = self.block()
        return A.If(start.span, branches, orelse)

    def for_stmt(self) -> A.For:
        start = self.advance()
        var = self.expect_name()
        self.expect_kw("in")
        iterable = self.expr()
        return A.For(start.span, var.value, iterable, self.block())

    def match_stmt(self) -> A.Match:
        """match_stmt = "match" expr ":" NEWLINE INDENT match_arm { match_arm } DEDENT"""
        start = self.advance()
        subject = self.expr()
        self.expect_op(":")
        self.expect_newline_raw()
        if self.peek().kind != "INDENT":
            raise self.error(word("indented"))
        self.advance()
        arms = []
        while self.peek().kind != "DEDENT":
            arms.append(self.match_arm())
        self.advance()
        return A.Match(start.span, subject, arms)

    def match_arm(self) -> A.MatchArm:
        """match_arm = pattern { "or" pattern } [ "if" expr ] ( ":" simple_stmt NEWLINE | block )"""
        start = self.peek()
        patterns = [self.pattern()]
        while self.at_kw("or"):
            self.advance()
            patterns.append(self.pattern())
        guard = None
        if self.at_kw("if"):
            self.advance()
            guard = self.expr()
        self.expect_op(":")
        if self.peek().kind == "NEWLINE":
            body = self.indented_body()
        else:
            body = [self.simple_stmt()]
            self.expect_newline()
        return A.MatchArm(start.span, patterns, guard, body)

    def pattern(self) -> A.Pattern:
        """pattern = "_" | literal | IDENT [ "(" pattern { "," pattern } ")" ]"""
        tok = self.peek()
        if tok.kind == "NAME" and tok.value == "_":
            self.advance()
            return A.PWild(tok.span)
        if tok.kind in ("STRING", "INT", "FLOAT", "UNIT") or tok.is_kw("true", "false", "none"):
            return A.PLit(tok.span, self.literal())
        if tok.kind == "NAME":
            self.advance()
            if self.at_op("("):
                self.advance()
                args = [self.pattern()]
                while self.at_op(","):
                    self.advance()
                    args.append(self.pattern())
                self.expect_op(")")
                return A.PCtor(self.span_from(tok), tok.value, args)
            return A.PName(tok.span, tok.value)
        raise self.error(word("pattern"))

    def try_stmt(self) -> A.TryStmt:
        """try_stmt = "try" [ "within" expr ] block else_clause { else_clause }
        else_clause = "else" IDENT [ "is" dotted_name ] block"""
        start = self.advance()
        within = None
        if self.at_name("within"):
            self.advance()
            within = self.expr()
        body = self.block()
        handlers = []
        while self.at_kw("else"):
            else_tok = self.advance()
            if self.peek().kind != "NAME":
                if self.at_op(":"):
                    raise NexoError("else_needs_var", else_tok.span)
                raise self.error(word("name"))
            var = self.advance().value
            kind = None
            if self.at_kw("is"):
                self.advance()
                kind = self.dotted_name()
            handlers.append(A.Handler(else_tok.span, var, kind, self.block()))
        if not handlers:
            raise self.error(word("else"))
        return A.TryStmt(start.span, within, body, handlers)

    def event_stmt(self) -> A.EventStmt:
        """event_stmt = ( "on" | "when" ) call [ "as" IDENT ] block
                      | "every" expr [ "at" expr ] block
                      | "lifecycle" IDENT [ "," args ] block"""
        tok = self.advance()
        alias = None
        if tok.value in ("on", "when"):
            trigger = self.postfix()
            if not isinstance(trigger, A.Call):
                raise self.error(word("call"), tok)
            if self.at_kw("as"):
                self.advance()
                alias = self.expect_name().value
        elif tok.value == "every":
            trigger = self.expr()
            if self.at_kw("at"):
                self.advance()
                self.expr()
        else:
            trigger = A.Name(self.peek().span, self.expect_name().value)
            if self.at_op(","):
                self.advance()
                self.args(":")
        return A.EventStmt(tok.span, tok.value, trigger, alias, self.block())

    # -- expressions ----------------------------------------------------
    def expr(self) -> A.Expr:
        """expr = lambda | pipeline ;  lambda = IDENT "->" expr"""
        tok = self.peek()
        if tok.kind == "NAME" and self.peek(1).is_op("->"):
            self.advance()
            self.advance()
            body = self.expr()
            return A.Lambda(self.span_from(tok), tok.value, body)
        return self.pipeline()

    def pipeline(self) -> A.Expr:
        """pipeline = or_expr { "|>" pipe_step }"""
        start = self.peek()
        left = self.or_expr()
        if not self.at_op("|>"):
            return left
        steps = []
        while self.at_op("|>"):
            self.advance()
            steps.append(self.pipe_step())
        self.check_steps(steps)
        return A.Pipeline(self.span_from(start), left, steps)

    def pipe_step(self) -> A.PipeStep:
        """pipe_step = IDENT { expr | IDENT } | call"""
        tok = self.peek()
        if tok.kind == "NAME" and tok.value in STEP_WORDS and not self.peek(1).is_op("(", "."):
            kind = self.advance().value
            step = A.PipeStep(tok.span, kind)
            if kind in ("group", "sort"):
                if not self.at_name("by"):
                    raise self.error(word("by"))
                self.advance()
                step.expr = self.or_expr()
                if kind == "sort" and self.at_name("desc"):
                    self.advance()
                    step.desc = True
            elif kind == "pick":
                step.columns = [self.or_expr()]
                while self.at_op(","):
                    self.advance()
                    step.columns.append(self.or_expr())
            elif kind in ("sum", "avg"):
                if self.starts_expr():
                    step.expr = self.or_expr()
            elif kind == "count":
                pass
            else:
                if not self.starts_expr():
                    raise NexoError("pipe_step_arg", tok.span, what=word("expr"),
                                    step=kind, example=f"{kind} it")
                step.expr = self.or_expr()
            step.span = self.span_from(tok)
            return step
        expr = self.unary()
        if not isinstance(expr, (A.Name, A.Attr, A.Call, A.Lambda)):
            raise self.error(word("call"), tok)
        return A.PipeStep(self.span_from(tok), "call", expr)

    def starts_expr(self) -> bool:
        tok = self.peek()
        if tok.kind in ("NEWLINE", "INDENT", "DEDENT", "EOF"):
            return False
        if tok.kind == "OP" and tok.value in ("|>", ")", "]", "}", ":", ",", "="):
            return False
        if tok.kind == "KEYWORD" and tok.value not in ("true", "false", "none", "not", "self", "parallel"):
            return False
        return True

    def check_steps(self, steps: list[A.PipeStep]) -> None:
        """Static rules: sum/avg of a column need a preceding group by, and
        only sum, count or avg can follow group by."""
        grouped = False
        for step in steps:
            if grouped:
                if step.kind not in ("sum", "count", "avg"):
                    raise NexoError("pipe_group_next", step.span)
                if step.kind != "count" and step.expr is None:
                    raise NexoError("pipe_step_arg", step.span, step=step.kind, what=word("name"),
                                    example=f"{step.kind} amount")
                grouped = False
            elif step.kind in ("sum", "avg") and step.expr is not None:
                col = source_text(step.expr.span)
                raise NexoError("pipe_needs_group", step.span, step=step.kind, col=col)
            if step.kind == "group":
                grouped = True

    def or_expr(self) -> A.Expr:
        start = self.peek()
        left = self.and_expr()
        while self.at_kw("or"):
            self.advance()
            right = self.and_expr()
            left = A.BoolOp(self.span_from(start), "or", left, right)
        return left

    def and_expr(self) -> A.Expr:
        start = self.peek()
        left = self.not_expr()
        while self.at_kw("and"):
            self.advance()
            right = self.not_expr()
            left = A.BoolOp(self.span_from(start), "and", left, right)
        return left

    def not_expr(self) -> A.Expr:
        tok = self.peek()
        if tok.is_kw("not"):
            self.advance()
            operand = self.not_expr()
            return A.Unary(self.span_from(tok), "not", operand)
        return self.comparison()

    def comparison(self) -> A.Expr:
        start = self.peek()
        first = self.coalesce()
        ops, operands = [], [first]
        while self.at_op(*COMPARE_OPS) or self.at_kw("in", "is"):
            ops.append(self.advance().value)
            operands.append(self.coalesce())
        if not ops:
            return first
        return A.Compare(self.span_from(start), ops, operands)

    def coalesce(self) -> A.Expr:
        start = self.peek()
        left = self.range_expr()
        while self.at_op("??"):
            self.advance()
            right = self.range_expr()
            left = A.Coalesce(self.span_from(start), left, right)
        return left

    def range_expr(self) -> A.Expr:
        start = self.peek()
        left = self.sum()
        if self.at_op(".."):
            self.advance()
            right = self.sum()
            return A.RangeExpr(self.span_from(start), left, right)
        return left

    def sum(self) -> A.Expr:
        start = self.peek()
        left = self.term()
        while self.at_op("+", "-"):
            op = self.advance().value
            right = self.term()
            left = A.BinOp(self.span_from(start), op, left, right)
        return left

    def term(self) -> A.Expr:
        start = self.peek()
        left = self.power()
        while self.at_op("*", "/", "//", "%"):
            op = self.advance().value
            right = self.power()
            left = A.BinOp(self.span_from(start), op, left, right)
        return left

    def power(self) -> A.Expr:
        start = self.peek()
        base = self.unary()
        if self.at_op("**"):
            self.advance()
            exp = self.power()
            return A.BinOp(self.span_from(start), "**", base, exp)
        return base

    def unary(self) -> A.Expr:
        tok = self.peek()
        if tok.is_op("-"):
            self.advance()
            operand = self.unary()
            return A.Unary(self.span_from(tok), "-", operand)
        return self.postfix()

    def postfix(self) -> A.Expr:
        """postfix = primary { "." IDENT | "(" [ args ] ")" | "[" expr "]" | "?" }"""
        start = self.peek()
        node = self.primary()
        while True:
            if self.at_op("."):
                self.advance()
                if self.peek().kind != "NAME":
                    raise self.error(word("member"))
                name = self.advance()
                node = A.Attr(self.span_from(start), node, name.value, name.span)
            elif self.at_op("("):
                self.advance()
                args = self.args(")")
                self.expect_op(")")
                if self.db and isinstance(node, A.Attr) and node.name == "sql" and len(args) == 1 \
                        and args[0].name is None and isinstance(args[0].value, A.Str):
                    args = [A.Arg(args[0].span, None, sql_text(args[0].value))]
                node = A.Call(self.span_from(start), node, args)
            elif self.at_op("["):
                self.advance()
                index = self.expr()
                self.expect_op("]")
                node = A.Index(self.span_from(start), node, index)
            elif self.at_op("?"):
                self.advance()
                node = A.Propagate(self.span_from(start), node)
            else:
                return node

    def args(self, closer: str) -> list[A.Arg]:
        """args = arg { "," arg } ;  arg = [ IDENT ":" ] expr"""
        result = []
        if self.at_op(closer):
            return result
        while True:
            tok = self.peek()
            name = None
            if tok.kind in ("NAME", "KEYWORD") and self.peek(1).is_op(":"):
                name = tok.value
                self.advance()
                self.advance()
            value = self.expr()
            result.append(A.Arg(self.span_from(tok), name, value))
            if not self.at_op(","):
                return result
            self.advance()

    def primary(self) -> A.Expr:
        tok = self.peek()
        kind = tok.kind
        if kind == "INT":
            self.advance()
            return A.Num(tok.span, tok.value, False)
        if kind == "FLOAT":
            self.advance()
            return A.Num(tok.span, tok.value, True)
        if kind == "UNIT":
            self.advance()
            text, is_float, unit = tok.value
            return A.UnitLit(tok.span, text, is_float, unit)
        if kind == "STRING":
            self.advance()
            return A.Str(tok.span, [self.interpolation(p) if isinstance(p, Interp) else p for p in tok.value])
        if kind == "NAME":
            self.advance()
            return A.Name(tok.span, tok.value)
        if kind == "KEYWORD":
            if tok.value in ("true", "false"):
                self.advance()
                return A.Bool(tok.span, tok.value == "true")
            if tok.value == "none":
                self.advance()
                return A.NoneLit(tok.span)
            if tok.value == "self":
                self.advance()
                return A.Name(tok.span, "self")
            if tok.value == "parallel" and self.peek(1).is_kw("for"):
                return self.parallel_for()
        if tok.is_op("("):
            self.advance()
            inner = self.expr()
            self.expect_op(")")
            inner.span = self.span_from(tok)
            return inner
        if tok.is_op("["):
            return self.list_lit()
        if tok.is_op("{"):
            return self.map_lit()
        raise self.error(word("expr"))

    def interpolation(self, part: Interp) -> A.Expr:
        tokens = tokenize_expr(part.source, self.file, part.line, part.col)
        sub = Parser(tokens, self.file, in_text=True)
        node = sub.expr()
        if sub.peek().kind != "EOF":
            raise sub.error("}")
        return node

    def parallel_for(self) -> A.ParallelFor:
        """"parallel" "for" IDENT "in" expr [ "," args ] block"""
        start = self.advance()
        self.advance()
        var = self.expect_name()
        self.expect_kw("in")
        iterable = self.expr()
        args = []
        if self.at_op(","):
            self.advance()
            args = self.args(":")
        span = self.span_from(start)
        body = self.block()
        self.block_end = self.p
        return A.ParallelFor(span, var.value, iterable, args, body)

    def list_lit(self) -> A.Expr:
        """list_lit = "[" [ expr { "," expr } ] "]"
        comprehension = "[" expr "for" IDENT "in" expr [ "if" expr ] "]" """
        start = self.advance()
        if self.at_op("]"):
            self.advance()
            return A.ListLit(self.span_from(start), [])
        first = self.expr()
        if self.at_kw("for"):
            self.advance()
            var = self.expect_name()
            self.expect_kw("in")
            iterable = self.expr()
            cond = None
            if self.at_kw("if"):
                self.advance()
                cond = self.expr()
            self.expect_op("]")
            return A.Comprehension(self.span_from(start), first, var.value, iterable, cond)
        items = [first]
        while self.at_op(","):
            self.advance()
            items.append(self.expr())
        self.expect_op("]")
        return A.ListLit(self.span_from(start), items)

    def map_lit(self) -> A.MapLit:
        """map_lit = "{" [ expr ":" expr { "," expr ":" expr } ] "}" """
        start = self.advance()
        pairs = []
        if not self.at_op("}"):
            while True:
                key = self.expr()
                self.expect_op(":")
                value = self.expr()
                pairs.append((key, value))
                if not self.at_op(","):
                    break
                self.advance()
        self.expect_op("}")
        return A.MapLit(self.span_from(start), pairs)


def check_unique(names: list[tuple[str, Span]]) -> None:
    seen = set()
    for name, span in names:
        if name in seen:
            raise NexoError("duplicate_name", span, name=name)
        seen.add(name)


def valid_target(node: A.Expr) -> bool:
    """target = IDENT { "." IDENT | "[" expr "]" }"""
    while isinstance(node, (A.Attr, A.Index)):
        node = node.obj
    return isinstance(node, A.Name)


def source_text(span: Span) -> str:
    source = errors.SOURCES.get(span.file)
    if source is None:
        return ""
    lines = source.splitlines()
    if not 1 <= span.line <= len(lines):
        return ""
    return lines[span.line - 1][span.col - 1:span.end_col - 1]


def parse(source: str, file: str = "<input>") -> A.Program:
    return Parser(tokenize(source, file), file).program()


# ------------------------------------------------------------------ modules
# Module blocks and annotations desugar into ordinary statements that call
# builtins whose names start with "__" (D90), so every later pass (the
# checker, the interpreter, the compiler) sees only core constructs.

MODULE_BLOCKS = {"ai", "agent", "server"}
ROUTE_METHODS = {"get", "post", "put", "patch", "delete"}
# keys whose bare names are model aliases (section 8)
MODEL_KEYS = {"model", "fallback", "when_over_budget"}


def is_identifier(text: str) -> bool:
    from .lexer import KEYWORDS, is_ident_char, is_ident_start
    return bool(text) and is_ident_start(text[0]) and all(is_ident_char(c) for c in text) and text not in KEYWORDS


def call(span: Span, fn: str, args: list, named: list | None = None) -> A.Call:
    out = [A.Arg(span, None, a) for a in args]
    out += [A.Arg(span, n, v) for n, v in (named or [])]
    return A.Call(span, A.Name(span, fn), out)


def text(span: Span, value: str) -> A.Str:
    return A.Str(span, [value])


def model_name(e: A.Expr) -> A.Expr:
    """A bare name in a model position is the name of a model (an alias)."""
    if isinstance(e, A.Name):
        return text(e.span, e.name)
    if isinstance(e, A.ListLit):
        return A.ListLit(e.span, [model_name(i) for i in e.items])
    return e


def raw_text(tok: Token) -> str:
    """The text of a route path, with its {name} parts as written."""
    return "".join(p if isinstance(p, str) else "{" + p.source + "}" for p in tok.value)


def type_value(t: A.TypeExpr | None, span: Span) -> A.Expr:
    """The type of a parameter as a value the runtime can check against."""
    if isinstance(t, A.TypeName):
        return A.Name(t.span, t.name)
    return A.NoneLit(span)


def _module_methods():
    def module_block(self, tok: Token) -> A.Stmt:
        """ai:, ai models:, ai profile "name":, agent name:, server name:"""
        word_ = tok.value
        name = None
        name_tok = None
        if word_ == "ai" and self.at_name("profile"):
            self.advance()
            if self.peek().kind != "STRING":
                raise self.error(word("string"))
            name = "".join(p for p in self.advance().value if isinstance(p, str))
            kind = "profile"
        elif word_ == "ai" and self.at_name("models"):
            self.advance()
            kind = "models"
        elif word_ == "ai":
            kind = "ai"
        else:
            name_tok = self.expect_name()
            name = name_tok.value
            kind = word_
        self.expect_op(":")
        self.expect_newline_raw()
        if self.peek().kind != "INDENT":
            raise self.error(word("indented"))
        self.advance()
        pairs: list = []
        routes: list = []
        while self.peek().kind != "DEDENT":
            t = self.peek()
            if kind == "server" and (t.is_op("@") or (t.kind == "NAME" and t.value in ROUTE_METHODS
                                                       and self.peek(1).kind == "STRING")):
                routes.append(self.route())
                continue
            key = self.expect_name()
            if not self.at_op("="):
                raise self.error('"="')
            self.advance()
            value = self.rate(self.expr())
            if key.value in MODEL_KEYS and kind != "models":
                value = model_name(value)
            if any(n == key.value for n, _ in pairs):
                raise NexoError("duplicate_name", key.span, name=key.value)
            pairs.append((key.value, value))
            self.expect_newline()
        self.advance()
        self.block_end = self.p
        span = tok.span
        if kind == "models":
            return A.ExprStmt(span, call(span, "__ai_models", [], pairs))
        if kind in ("ai", "profile"):
            first = text(span, name) if name is not None else A.NoneLit(span)
            return A.ExprStmt(span, call(span, "__ai_config", [first], pairs))
        fn = "__ai_agent" if kind == "agent" else "__web_server"
        target = A.Name(name_tok.span, name)
        stmt = A.Assign(name_tok.span, target, "=", call(span, fn, [text(span, name)], pairs))
        for route_fn, method, path, annotations, mspan in routes:
            named = []
            for a in annotations:
                if a.name == "role" and len(a.args) == 1:
                    named.append(("role", a.args[0].value))
                elif a.name == "limit" and len(a.args) == 1:
                    named.append(("limit", a.args[0].value))
                elif a.name == "public" and not a.args:
                    named.append(("public", A.Bool(a.span, True)))
                else:
                    raise NexoError("not_supported", a.span, what=f'"@{a.name}"')
            types = A.ListLit(mspan, [type_value(p.type, mspan) for p in route_fn.params])
            self.extra.append(route_fn)
            self.extra.append(A.ExprStmt(mspan, call(mspan, "__web_route", [
                A.Name(mspan, name), text(mspan, method), text(mspan, path),
                A.Name(mspan, route_fn.name), types], named)))
        return stmt

    def route(self):
        """route = { annotation } METHOD STRING [ "(" params ")" ] block"""
        annotations = self.annotations() if self.at_op("@") else []
        tok = self.expect_name()
        if tok.value not in ROUTE_METHODS:
            raise self.error(word("route"), tok)
        if self.peek().kind != "STRING":
            raise self.error(word("string"))
        path = raw_text(self.advance())
        params = self.params() if self.at_op("(") else []
        method = tok.value.upper()
        span = self.span_from(tok)
        body = self.block()
        fn = A.FnDef(span, f"{method} {path}", tok.span, params, None, body)
        return fn, method, path, annotations, tok.span

    def table_def(self, node: A.Stmt) -> A.Stmt:
        """@table type T: the type, then __db_table(T, key, many, marks)."""
        if not isinstance(node, A.TypeDef):
            return node
        tables = [a for a in node.annotations if a.name == "table"]
        rels = [f for f in node.fields if isinstance(f.type, A.TypeName) and f.type.rel]
        if not tables:
            if rels:
                raise NexoError("table_only", rels[0].type.span, word=rels[0].type.rel)
            return node
        a = tables[0]
        if a.args:
            raise NexoError("not_supported", a.span, what=f'"@table(...)"')
        if not self.db:
            raise NexoError("needs_use", a.span, what='"@table"', module="nexo.db")
        node.annotations = [x for x in node.annotations if x.name != "table"]
        key = None
        many = []
        kept = []
        marks = []
        for f in node.fields:
            t = f.type
            if isinstance(t, A.TypeName) and t.rel == "many":
                many.append((text(f.span, f.name), text(t.span, t.name)))
                continue
            if isinstance(t, A.TypeName) and t.name == "id" and not t.params and t.rel is None:
                if key is not None:
                    raise NexoError("table_two_ids", f.span, type=node.name)
                key = f.name
                f.type = A.TypeName(t.span, "int", optional=True)
            fmarks = f.marks + (["ref"] if isinstance(t, A.TypeName) and t.rel == "ref" else [])
            if fmarks:
                marks.append((text(f.span, f.name), A.ListLit(f.span, [text(f.span, m) for m in fmarks])))
            kept.append(f)
        node.fields = kept
        span = a.span
        self.extra.append(A.ExprStmt(span, call(span, "__db_table", [
            A.Name(node.name_span, node.name),
            text(span, key) if key else A.NoneLit(span),
            A.MapLit(span, many), A.MapLit(span, marks)])))
        return node

    def tool_def(self, fn: A.Stmt) -> A.Stmt:
        """@tool "description" fn ...: the function, then __ai_tool(fn, ...)."""
        if not isinstance(fn, A.FnDef):
            return fn
        tools = [a for a in fn.annotations if a.name == "tool"]
        if not tools:
            return fn
        a = tools[0]
        fn.annotations = [x for x in fn.annotations if x.name != "tool"]
        desc = a.args[0].value if a.args else text(a.span, fn.name)
        types = A.ListLit(a.span, [type_value(p.type, a.span) for p in fn.params])
        self.extra.append(A.ExprStmt(a.span, call(a.span, "__ai_tool", [A.Name(fn.name_span, fn.name), desc, types])))
        return fn

    def fake_stmt(self) -> A.Stmt:
        """fake NAME returns EXPR (inside a test, section 14)"""
        tok = self.advance()
        name = self.dotted_name()
        if not self.at_name("returns"):
            raise self.error('"returns"')
        self.advance()
        value = self.expr()
        span = self.span_from(tok)
        stmt = A.ExprStmt(span, call(span, "__fake", [text(tok.span, name), value]))
        self.expect_newline()
        return stmt

    def at_transaction(self) -> bool:
        """NAME { "." NAME } "." "transaction" ":" NEWLINE"""
        k = 0
        if self.peek().kind != "NAME":
            return False
        k = 1
        while self.peek(k).is_op(".") and self.peek(k + 1).kind == "NAME":
            k += 2
        return (k >= 3 and self.peek(k - 1).value == "transaction" and self.peek(k).is_op(":")
                and self.peek(k + 1).kind == "NEWLINE")

    def transaction(self) -> A.Stmt:
        """db.transaction: block (section 6), as
            __txL_C = __db_begin(db)
            try:
                block
                __db_commit(__txL_C)
            else __e:
                __db_rollback(__txL_C)
                fail(__e)"""
        start = self.peek()
        obj: A.Expr = A.Name(start.span, self.advance().value)
        while not (self.peek(1).kind == "NAME" and self.peek(1).value == "transaction" and self.peek(2).is_op(":")):
            self.advance()
            n = self.advance()
            obj = A.Attr(self.span_from(start), obj, n.value, n.span)
        self.advance()
        self.advance()
        span = self.span_from(start)
        body = self.block()
        for s in body_returns(body):
            raise NexoError("transaction_return", s.span)
        tx = A.Name(span, f"__tx{span.line}_{span.col}")
        begin = A.Assign(span, tx, "=", call(span, "__db_begin", [obj]))
        body = body + [A.ExprStmt(span, call(span, "__db_commit", [tx]))]
        handler = A.Handler(span, "__e", None, [
            A.ExprStmt(span, call(span, "__db_rollback", [tx])),
            A.ExprStmt(span, call(span, "fail", [A.Name(span, "__e")])),
        ])
        self.extra.append(A.TryStmt(span, None, body, [handler]))
        return begin

    return locals()


for _name, _fn in _module_methods().items():
    setattr(Parser, _name, _fn)


def sql_text(s: A.Str) -> A.Call:
    """db.sql("... {x} ..."): the literal parts and the values apart, so
    the values become parameters (section 6, D100)."""
    parts, values = [""], []
    for p in s.parts:
        if isinstance(p, str):
            parts[-1] += p
        else:
            values.append(p)
            parts.append("")
    return call(s.span, "__sql_text", [A.ListLit(s.span, [text(s.span, x) for x in parts]),
                                       A.ListLit(s.span, values)])


def body_returns(body: list):
    from .checker import walk_stmts
    for s in walk_stmts(body, into_fns=False):
        if isinstance(s, A.Return):
            yield s


def model_aliases(program: A.Program) -> None:
    """`ask(..., model: fast)`: a bare name declared by an `ai models:`
    block is the name of that model (section 8)."""
    from .checker import walk_stmts
    aliases = set()
    for s in program.body:
        if isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and getattr(s.expr.func, "name", "") == "__ai_models":
            aliases.update(a.name for a in s.expr.args if a.name)
    if not aliases:
        return

    def fix(e):
        if isinstance(e, A.Call) and isinstance(e.func, A.Name) and e.func.name == "ask":
            for a in e.args:
                if a.name == "model" and isinstance(a.value, A.Name) and a.value.name in aliases:
                    a.value = text(a.value.span, a.value.name)
        for c in sub_exprs(e):
            fix(c)

    for s in walk_stmts(program.body):
        for e in stmt_exprs(s):
            fix(e)


def sub_exprs(e):
    from .checker import children
    if isinstance(e, A.Call):
        return [e.func] + [a.value for a in e.args]
    if isinstance(e, A.Propagate):
        return [e.expr]
    if isinstance(e, A.Coalesce):
        return [e.left, e.right]
    if isinstance(e, A.Lambda):
        return [e.body]
    if isinstance(e, A.Pipeline):
        return [e.source] + [x for st in e.steps for x in ([st.expr] if st.expr else []) + list(st.columns)]
    if isinstance(e, A.ParallelFor):
        return [e.iterable] + [a.value for a in e.args] + [x for st in e.body for x in stmt_exprs(st)]
    return children(e)


def stmt_exprs(s):
    out = []
    for attr in ("expr", "value", "target", "cond", "count", "iterable", "subject", "within", "call"):
        v = getattr(s, attr, None)
        if isinstance(v, A.Expr):
            out.append(v)
    if isinstance(s, A.If):
        out.extend(c for c, _ in s.branches)
    if isinstance(s, A.Match):
        out.extend(a.guard for a in s.arms if a.guard is not None)
    return out
