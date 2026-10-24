//! Recursive-descent parser following the EBNF of section 24.
//!
//! Each method is named after the grammar rule it implements. Precedence, from
//! lowest to highest: `->`, `|>`, `or`, `and`, `not`, comparisons, `??`, `..`,
//! `+ -`, `* / // %`, `**`, unary `-`, member access and call.

use nexo_diag::{source_text, word, Diagnostic, Span};

use crate::ast::*;
use crate::lexer::{tokenize, tokenize_expr, StrPart, Tok, Token};

const ASSIGN_OPS: &[&str] = &["=", "+=", "-=", "*=", "/="];
const COMPARE_OPS: &[&str] = &["==", "!=", "<", ">", "<=", ">="];

type PResult<T> = Result<T, Diagnostic>;

pub struct Parser<'s> {
    toks: Vec<Token>,
    p: usize,
    source: &'s str,
    /// parsing an {expr} inside a text literal
    in_text: bool,
    /// token index right after an expression that ended in a block
    block_end: usize,
    next_id: u32,
    /// after `use nexo.sync`, spawn, select and shared start statements
    sync: bool,
    /// `use nexo.db` makes connect and @table available
    db: bool,
    /// statements a module block or annotation desugars into, after the
    /// one `statement` returned (D90)
    extra: Vec<Stmt>,
    in_test: bool,
    /// inside a phrase of a phase 5 block, `(` and `[` after a space start
    /// a new atom (D114)
    phrase_mode: bool,
    /// names the steps of each pipeline keep (D122)
    pipeline_vars: std::collections::HashMap<String, Vec<String>>,
}

fn describe(tok: &Token) -> String {
    match &tok.tok {
        Tok::Newline => word("NEWLINE"),
        Tok::Eof => word("EOF"),
        Tok::Indent => word("INDENT"),
        Tok::Dedent => word("DEDENT"),
        Tok::Str(_) => word("STRING"),
        Tok::Int(v) | Tok::Float(v) => format!("\"{v}\""),
        Tok::Unit { text, unit, .. } => {
            if unit == "%" {
                format!("\"{text}{unit}\"")
            } else {
                format!("\"{text} {unit}\"")
            }
        }
        Tok::Name(v) => format!("\"{v}\""),
        Tok::Keyword(v) | Tok::Op(v) => format!("\"{v}\""),
    }
}

fn is_literal_start(tok: &Token) -> bool {
    matches!(tok.tok, Tok::Str(_) | Tok::Int(_) | Tok::Float(_) | Tok::Unit { .. })
        || tok.is_kw("true")
        || tok.is_kw("false")
        || tok.is_kw("none")
}

impl<'s> Parser<'s> {
    pub fn new(toks: Vec<Token>, source: &'s str, in_text: bool, next_id: u32) -> Parser<'s> {
        Parser {
            toks,
            p: 0,
            source,
            in_text,
            block_end: usize::MAX,
            next_id,
            sync: false,
            db: false,
            extra: Vec::new(),
            in_test: false,
            phrase_mode: false,
            pipeline_vars: std::collections::HashMap::new(),
        }
    }

    fn id(&mut self) -> u32 {
        self.next_id += 1;
        self.next_id
    }

    fn expr_node(&mut self, span: Span, kind: ExprKind) -> Expr {
        Expr { id: self.id(), span, kind }
    }

    fn stmt_node(&mut self, span: Span, kind: StmtKind) -> Stmt {
        Stmt { id: self.id(), span, kind }
    }

    // -- token helpers ---------------------------------------------------
    fn peek(&self) -> &Token {
        self.peek_at(0)
    }

    fn peek_at(&self, k: usize) -> &Token {
        &self.toks[(self.p + k).min(self.toks.len() - 1)]
    }

    fn prev(&self) -> &Token {
        &self.toks[self.p - 1]
    }

    fn advance(&mut self) -> Token {
        let tok = self.toks[self.p].clone();
        if tok.tok != Tok::Eof {
            self.p += 1;
        }
        tok
    }

    fn at_op(&self, op: &str) -> bool {
        self.peek().is_op(op)
    }

    /// Is there a space between the last token and the next one?
    fn spaced(&self) -> bool {
        let (a, b) = (self.prev().span, self.peek().span);
        a.line != b.line || a.end_col != b.col
    }

    fn at_ops(&self, ops: &[&str]) -> bool {
        ops.iter().any(|o| self.peek().is_op(o))
    }

    fn at_kw(&self, kw: &str) -> bool {
        self.peek().is_kw(kw)
    }

    fn at_name(&self, value: &str) -> bool {
        matches!(&self.peek().tok, Tok::Name(n) if n == value)
    }

    fn is_name(&self) -> bool {
        matches!(self.peek().tok, Tok::Name(_))
    }

    fn is_name_at(&self, k: usize) -> bool {
        matches!(self.peek_at(k).tok, Tok::Name(_))
    }

    fn error_at(&self, expected: &str, tok: &Token) -> Diagnostic {
        if self.in_text && tok.tok == Tok::Eof {
            return Diagnostic::new("unexpected", tok.span)
                .fix("expected")
                .arg("got", "\"}\"")
                .arg("expected", expected);
        }
        if expected == word("name") {
            if let Tok::Keyword(k) = tok.tok {
                return Diagnostic::new("reserved_word", tok.span).arg("word", k);
            }
        }
        Diagnostic::new("unexpected", tok.span)
            .fix("expected")
            .arg("got", describe(tok))
            .arg("expected", expected)
    }

    fn error(&self, expected: &str) -> Diagnostic {
        self.error_at(expected, self.peek())
    }

    fn expect_op(&mut self, op: &str) -> PResult<Token> {
        if !self.at_op(op) {
            return Err(self.error(&format!("\"{op}\"")));
        }
        Ok(self.advance())
    }

    fn expect_kw(&mut self, kw: &str) -> PResult<Token> {
        if !self.at_kw(kw) {
            return Err(self.error(&format!("\"{kw}\"")));
        }
        Ok(self.advance())
    }

    fn expect_name(&mut self) -> PResult<(String, Span)> {
        if let Tok::Name(n) = &self.peek().tok {
            let n = n.clone();
            let span = self.advance().span;
            return Ok((n, span));
        }
        Err(self.error(&word("name")))
    }

    fn expect_newline(&mut self) -> PResult<()> {
        if self.p == self.block_end {
            return Ok(());
        }
        if self.peek().tok != Tok::Newline {
            return Err(self.error(&word("newline")));
        }
        self.advance();
        Ok(())
    }

    fn expect_newline_raw(&mut self) -> PResult<()> {
        if self.peek().tok != Tok::Newline {
            return Err(self.error(&word("newline")));
        }
        self.advance();
        Ok(())
    }

    fn span_from(&self, start: Span) -> Span {
        start.to(self.prev().span)
    }

    // -- program and blocks ---------------------------------------------
    pub fn program(&mut self) -> PResult<Program> {
        let mut body = Vec::new();
        while self.peek().tok != Tok::Eof {
            if self.peek().tok == Tok::Indent {
                return Err(Diagnostic::new("dedent_mismatch", self.peek().span));
            }
            self.statement_into(&mut body)?;
        }
        let mut program = Program { body };
        model_aliases(&mut program, &mut self.next_id);
        Ok(program)
    }

    fn statement_into(&mut self, body: &mut Vec<Stmt>) -> PResult<()> {
        let s = self.statement()?;
        body.push(s);
        body.append(&mut self.extra);
        Ok(())
    }

    /// block = ":" NEWLINE INDENT statement { statement } DEDENT
    fn block(&mut self) -> PResult<Vec<Stmt>> {
        if !self.at_op(":") {
            return Err(self.error(&word("block")));
        }
        self.advance();
        self.indented_body()
    }

    fn indented_body(&mut self) -> PResult<Vec<Stmt>> {
        if self.peek().tok != Tok::Newline {
            return Err(self.error(&word("block")));
        }
        self.advance();
        if self.peek().tok != Tok::Indent {
            return Err(self.error(&word("indented")));
        }
        self.advance();
        let mut body = Vec::new();
        while self.peek().tok != Tok::Dedent {
            self.statement_into(&mut body)?;
        }
        self.advance();
        Ok(body)
    }

    // -- statements -----------------------------------------------------
    fn statement(&mut self) -> PResult<Stmt> {
        let tok = self.peek().clone();
        if tok.is_op("@") {
            let annotations = self.annotations()?;
            if self.at_kw("type") || (self.at_kw("pub") && self.peek_at(1).is_kw("type")) {
                let t = self.type_def(annotations)?;
                return self.table_def(t);
            }
            let f = self.fn_def(annotations)?;
            let s = self.stmt_node(f.span, StmtKind::Fn(f));
            return self.tool_def(s);
        }
        if self.in_test && matches!(&tok.tok, Tok::Name(n) if n == "fake") && self.is_name_at(1) {
            return self.fake_stmt();
        }
        if self.is_name() && self.at_transaction() {
            return self.transaction();
        }
        if let Tok::Keyword(kw) = tok.tok {
            match kw {
                "fn" => {
                    let f = self.fn_def(Vec::new())?;
                    return Ok(self.stmt_node(f.span, StmtKind::Fn(f)));
                }
                "pub" => {
                    if self.peek_at(1).is_kw("type") {
                        let t = self.type_def(Vec::new())?;
                        return self.table_def(t);
                    }
                    let f = self.fn_def(Vec::new())?;
                    return Ok(self.stmt_node(f.span, StmtKind::Fn(f)));
                }
                "type" => {
                    let t = self.type_def(Vec::new())?;
                    return self.table_def(t);
                }
                "trait" => return self.trait_def(),
                "if" => return self.if_stmt(),
                "for" => return self.for_stmt(),
                "while" => {
                    self.advance();
                    let cond = self.expr()?;
                    let body = self.block()?;
                    return Ok(self.stmt_node(tok.span, StmtKind::While { cond, body }));
                }
                "repeat" => {
                    self.advance();
                    let count = self.expr()?;
                    let body = self.block()?;
                    return Ok(self.stmt_node(tok.span, StmtKind::Repeat { count, body }));
                }
                "match" => return self.match_stmt(),
                "try" => return self.try_stmt(),
                "parallel" if self.peek_at(1).is_op(":") => {
                    self.advance();
                    let body = self.block()?;
                    return Ok(self.stmt_node(tok.span, StmtKind::Parallel(body)));
                }
                "test" if !self.peek_at(1).is_op("(") => {
                    self.advance();
                    let name = match &self.peek().tok {
                        Tok::Str(parts) if parts.iter().all(|p| matches!(p, StrPart::Lit(_))) => parts
                            .iter()
                            .map(|p| match p {
                                StrPart::Lit(s) => s.as_str(),
                                _ => "",
                            })
                            .collect::<String>(),
                        _ => return Err(self.error(&word("string"))),
                    };
                    self.advance();
                    self.in_test = true;
                    let body = self.block();
                    self.in_test = false;
                    let body = body?;
                    return Ok(self.stmt_node(tok.span, StmtKind::Test { name, body }));
                }
                "on" | "when" | "every" | "lifecycle" => return self.event_stmt(),
                _ => {}
            }
        }
        if self.sync && matches!(&tok.tok, Tok::Name(n) if n == "select") && self.peek_at(1).is_op(":") {
            return self.select_stmt();
        }
        let stmt = self.simple_stmt()?;
        self.expect_newline()?;
        Ok(stmt)
    }

    fn simple_stmt(&mut self) -> PResult<Stmt> {
        let tok = self.peek().clone();
        if tok.is_kw("pass") {
            self.advance();
            return Ok(self.stmt_node(tok.span, StmtKind::Pass));
        }
        if tok.is_kw("var") {
            self.advance();
            let (name, name_span) = self.expect_name()?;
            let mut ty = None;
            if self.at_op(":") {
                self.advance();
                ty = Some(self.type_()?);
            }
            self.expect_op("=")?;
            let value = self.expr()?;
            let span = self.span_from(tok.span);
            return Ok(self.stmt_node(span, StmtKind::VarDecl { name, name_span, ty, value }));
        }
        if tok.is_kw("return") {
            self.advance();
            if matches!(self.peek().tok, Tok::Newline | Tok::Dedent | Tok::Eof) {
                return Ok(self.stmt_node(tok.span, StmtKind::Return(None)));
            }
            let value = self.expr()?;
            let span = self.span_from(tok.span);
            return Ok(self.stmt_node(span, StmtKind::Return(Some(value))));
        }
        if tok.is_kw("use") {
            return self.use_stmt();
        }
        if self.sync && matches!(&tok.tok, Tok::Name(n) if n == "spawn") && matches!(self.peek_at(1).tok, Tok::Name(_)) {
            self.advance();
            let call = self.postfix()?;
            if !matches!(call.kind, ExprKind::Call { .. }) {
                return Err(self.error_at(&word("call"), &tok));
            }
            let span = self.span_from(tok.span);
            return Ok(self.stmt_node(span, StmtKind::Spawn { call }));
        }
        if self.sync
            && matches!(&tok.tok, Tok::Name(n) if n == "shared")
            && matches!(self.peek_at(1).tok, Tok::Name(_))
            && self.peek_at(2).is_op("=")
        {
            self.advance();
            let (name, name_span) = self.expect_name()?;
            self.expect_op("=")?;
            let value = self.expr()?;
            let span = self.span_from(tok.span);
            return Ok(self.stmt_node(span, StmtKind::Shared { name, name_span, value }));
        }
        if tok.is_kw("emit") {
            self.advance();
            let call = self.postfix()?;
            let ExprKind::Call { func, args } = call.kind else {
                return Err(self.error_at(&word("call"), &tok));
            };
            let name = match func.kind {
                ExprKind::Name(n) => n,
                _ => String::new(),
            };
            let span = self.span_from(tok.span);
            return Ok(self.stmt_node(span, StmtKind::Emit { name, args, call_span: call.span }));
        }
        // the span of expect/ensure is the keyword alone, as in phase 0
        if tok.is_kw("expect") {
            self.advance();
            let e = self.expr()?;
            return Ok(self.stmt_node(tok.span, StmtKind::Expect(e)));
        }
        if tok.is_kw("ensure") {
            self.advance();
            let e = self.expr()?;
            return Ok(self.stmt_node(tok.span, StmtKind::Ensure(e)));
        }
        if tok.is_kw("assert") {
            self.advance();
            let expr = self.expr()?;
            let mut fails = false;
            if self.at_name("fails") {
                self.advance();
                fails = true;
            }
            let span = self.span_from(tok.span);
            return Ok(self.stmt_node(span, StmtKind::Assert { expr, fails }));
        }
        if self.is_config_block() {
            return self.config_block();
        }
        // assignment or expression statement
        let target = self.expr()?;
        if self.p == self.block_end {
            return Ok(self.stmt_node(target.span, StmtKind::Expr(target)));
        }
        let mut ty = None;
        if self.at_op(":") && matches!(target.kind, ExprKind::Name(_)) {
            self.advance();
            ty = Some(self.type_()?);
            if !self.at_op("=") {
                return Err(self.error("\"=\""));
            }
        }
        if self.at_ops(ASSIGN_OPS) {
            let Tok::Op(op) = self.advance().tok else { unreachable!() };
            if !valid_target(&target) {
                return Err(Diagnostic::new("invalid_target", target.span));
            }
            if ty.is_some() && op != "=" {
                return Err(self.error_at("\"=\"", &self.prev().clone()));
            }
            let value = self.expr()?;
            let span = target.span;
            return Ok(self.stmt_node(span, StmtKind::Assign { target, op, value, ty }));
        }
        Ok(self.stmt_node(target.span, StmtKind::Expr(target)))
    }

    fn is_config_block(&self) -> bool {
        if !self.is_name() {
            return false;
        }
        if self.peek_at(1).is_op(":") && self.peek_at(2).tok == Tok::Newline {
            return true;
        }
        if self.at_name("ai")
            && matches!(&self.peek_at(1).tok, Tok::Name(n) if n == "profile")
            && matches!(self.peek_at(2).tok, Tok::Str(_))
            && self.peek_at(3).is_op(":")
        {
            return true;
        }
        matches!(self.peek_at(1).tok, Tok::Name(_) | Tok::Str(_))
            && self.peek_at(2).is_op(":")
            && self.peek_at(3).tok == Tok::Newline
    }

    /// config_block = IDENT [ IDENT | STRING ] ":" NEWLINE INDENT {...} DEDENT
    ///
    /// Module blocks (ai, infra, pipeline...) are outside the core, so the
    /// body is only skipped; the program reports them as unavailable.
    fn config_block(&mut self) -> PResult<Stmt> {
        let tok = self.advance();
        let Tok::Name(word_) = tok.tok.clone() else { unreachable!() };
        if MODULE_BLOCKS.contains(&word_.as_str()) {
            return self.module_block(tok);
        }
        if !self.at_op(":") {
            self.advance();
        }
        self.expect_op(":")?;
        self.expect_newline_raw()?;
        if self.peek().tok != Tok::Indent {
            return Err(self.error(&word("indented")));
        }
        let mut depth = 0;
        loop {
            let t = self.advance();
            match t.tok {
                Tok::Indent => depth += 1,
                Tok::Dedent => {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                Tok::Eof => break,
                _ => {}
            }
        }
        self.block_end = self.p;
        Ok(self.stmt_node(tok.span, StmtKind::Config { word: word_ }))
    }

    /// use_stmt = "use" [ IDENT ] dotted_name [ "as" IDENT ], and
    /// `use c "library"` (section 18, D110)
    fn use_stmt(&mut self) -> PResult<Stmt> {
        let tok = self.advance();
        let (first, _) = self.expect_name()?;
        let mut lang = None;
        let mut name = first.clone();
        if first == "c" && matches!(self.peek().tok, Tok::Str(_)) {
            lang = Some(first);
            name = match &self.peek().tok {
                Tok::Str(parts) if parts.iter().all(|p| matches!(p, StrPart::Lit(_))) => parts
                    .iter()
                    .map(|p| match p {
                        StrPart::Lit(s) => s.as_str(),
                        _ => "",
                    })
                    .collect(),
                _ => return Err(self.error(&word("string"))),
            };
            self.advance();
        } else if self.is_name() {
            lang = Some(first);
            name = self.dotted_name()?;
        } else {
            while self.at_op(".") {
                self.advance();
                name.push('.');
                name.push_str(&self.expect_name()?.0);
            }
        }
        let mut alias = None;
        let mut alias_span = None;
        if self.at_kw("as") {
            self.advance();
            let (a, sp) = self.expect_name()?;
            alias = Some(a);
            alias_span = Some(sp);
        }
        if lang.is_none() && name == "nexo.sync" {
            self.sync = true;
        }
        if lang.is_none() && name == "nexo.db" {
            self.db = true;
        }
        let span = self.span_from(tok.span);
        if let Some(l) = lang.as_deref().filter(|l| *l == "c" || *l == "python") {
            let bound = alias.clone().unwrap_or_else(|| {
                if l == "python" {
                    name.rsplit('.').next().unwrap().to_string()
                } else {
                    name.clone()
                }
            });
            if !is_identifier(&bound) {
                return Err(Diagnostic::new("use_needs_as", span).arg("name", name.clone()));
            }
            let fname = if l == "python" { "__python_import" } else { "__c_library" };
            let mut args = vec![self.text(span, &name)];
            if l == "c" {
                // the name it is used with, for the messages (libz.crc32)
                args.push(self.text(span, &bound.clone()));
            }
            let call = self.call(span, fname, args, Vec::new());
            let at = alias_span.unwrap_or(span);
            let target = self.expr_node(at, ExprKind::Name(bound));
            let st = self.stmt_node(at, StmtKind::Assign { target, op: "=", value: call, ty: None });
            self.extra.push(st);
        }
        let what = match &lang {
            Some(l) => format!("{l} {name}"),
            None => name.clone(),
        };
        Ok(self.stmt_node(span, StmtKind::Use { what, lang, name, alias }))
    }

    /// select: arms `NAME from expr: ...` and `after expr: ...` (nexo.sync)
    fn select_stmt(&mut self) -> PResult<Stmt> {
        let start = self.advance();
        self.expect_op(":")?;
        self.expect_newline_raw()?;
        if self.peek().tok != Tok::Indent {
            return Err(self.error(&word("indented")));
        }
        self.advance();
        let mut arms = Vec::new();
        while self.peek().tok != Tok::Dedent {
            let tok = self.peek().clone();
            let (mut var, mut channel, mut after) = (None, None, None);
            let next_is_from = matches!(&self.peek_at(1).tok, Tok::Name(n) if n == "from");
            if matches!(&tok.tok, Tok::Name(n) if n == "after") && !next_is_from {
                self.advance();
                after = Some(self.expr()?);
            } else {
                var = Some(self.expect_name()?.0);
                if !self.at_name("from") {
                    return Err(self.error("\"from\""));
                }
                self.advance();
                channel = Some(self.expr()?);
            }
            self.expect_op(":")?;
            let body = if self.peek().tok == Tok::Newline {
                self.indented_body()?
            } else {
                let s = self.simple_stmt()?;
                self.expect_newline()?;
                vec![s]
            };
            arms.push(SelectArm { span: tok.span, var, channel, after, body });
        }
        self.advance();
        Ok(self.stmt_node(start.span, StmtKind::Select { arms }))
    }

    fn dotted_name(&mut self) -> PResult<String> {
        let mut name = self.expect_name()?.0;
        while self.at_op(".") {
            self.advance();
            name.push('.');
            name.push_str(&self.expect_name()?.0);
        }
        Ok(name)
    }

    /// annotation = "@" dotted_name [ "(" [ args ] ")" ] NEWLINE
    fn annotations(&mut self) -> PResult<Vec<Annotation>> {
        let mut result = Vec::new();
        while self.at_op("@") {
            let tok = self.advance();
            let name = self.dotted_name()?;
            let mut args = Vec::new();
            if self.at_op("(") {
                self.advance();
                for a in self.args(")")? {
                    let value = self.rate(a.value)?;
                    args.push(Arg { span: a.span, name: a.name, value });
                }
                self.expect_op(")")?;
            } else if matches!(self.peek().tok, Tok::Str(_)) {
                let value = self.primary()?;
                args.push(Arg { span: value.span, name: None, value });
            }
            result.push(Annotation { span: self.span_from(tok.span), name, args });
            self.expect_newline()?;
        }
        Ok(result)
    }

    /// fn_def = { annotation } [ "pub" ] "fn" IDENT "(" [ params ] ")" [ "->" type ] block
    fn fn_def(&mut self, annotations: Vec<Annotation>) -> PResult<FnDef> {
        let start = self.peek().span;
        let mut is_pub = false;
        if self.at_kw("pub") {
            self.advance();
            is_pub = true;
        }
        self.expect_kw("fn")?;
        let (name, name_span) = self.expect_name()?;
        let params = self.params()?;
        let mut ret = None;
        if self.at_op("->") {
            self.advance();
            ret = Some(self.type_()?);
        }
        let body = self.block()?;
        let id = self.id();
        Ok(FnDef { id, span: start, name, name_span, params, ret, body, is_pub, annotations })
    }

    fn params(&mut self) -> PResult<Vec<Param>> {
        self.expect_op("(")?;
        let mut params = Vec::new();
        if !self.at_op(")") {
            params.push(self.param()?);
            while self.at_op(",") {
                self.advance();
                params.push(self.param()?);
            }
        }
        self.expect_op(")")?;
        check_unique(params.iter().map(|p| (p.name.as_str(), p.span)))?;
        Ok(params)
    }

    /// param = IDENT [ ":" type ] [ "=" expr ]
    fn param(&mut self) -> PResult<Param> {
        let (name, span) = if self.at_kw("self") {
            let t = self.advance();
            ("self".to_string(), t.span)
        } else {
            self.expect_name()?
        };
        let mut ty = None;
        let mut default = None;
        if self.at_op(":") {
            self.advance();
            ty = Some(self.type_()?);
        }
        if self.at_op("=") {
            self.advance();
            default = Some(self.expr()?);
        }
        Ok(Param { span, name, ty, default })
    }

    /// type_def = { annotation } [ "pub" ] "type" IDENT [ type_params ]
    ///            ( "=" type NEWLINE | [ "is" dotted_name ] type_body )
    fn type_def(&mut self, annotations: Vec<Annotation>) -> PResult<Stmt> {
        let start = self.peek().span;
        if self.at_kw("pub") {
            self.advance();
        }
        self.expect_kw("type")?;
        let (name, name_span) = self.expect_name()?;
        let mut node = TypeDef {
            span: start,
            name,
            name_span,
            alias: None,
            trait_name: None,
            fields: Vec::new(),
            variants: Vec::new(),
            methods: Vec::new(),
            settings: Vec::new(),
            annotations,
        };
        if self.at_op("[") {
            self.type_params()?;
        }
        if self.at_op("=") {
            self.advance();
            node.alias = Some(self.type_()?);
            self.expect_newline()?;
            return Ok(self.stmt_node(start, StmtKind::Type(node)));
        }
        if self.at_kw("is") {
            self.advance();
            node.trait_name = Some(self.dotted_name()?);
        }
        self.type_body(&mut node)?;
        Ok(self.stmt_node(start, StmtKind::Type(node)))
    }

    fn type_params(&mut self) -> PResult<Vec<TypeExpr>> {
        self.expect_op("[")?;
        let mut params = vec![self.type_()?];
        while self.at_op(",") {
            self.advance();
            params.push(self.type_()?);
        }
        self.expect_op("]")?;
        Ok(params)
    }

    /// type_body = ":" NEWLINE INDENT { field | variant | fn_def | config_pair } DEDENT
    fn type_body(&mut self, node: &mut TypeDef) -> PResult<()> {
        self.expect_op(":")?;
        self.expect_newline_raw()?;
        if self.peek().tok != Tok::Indent {
            return Err(self.error(&word("indented")));
        }
        self.advance();
        let mut settings_seen: Vec<String> = Vec::new();
        while self.peek().tok != Tok::Dedent {
            let tok = self.peek().clone();
            if tok.is_op("@") {
                let ann = self.annotations()?;
                node.methods.push(self.fn_def(ann)?);
            } else if tok.is_kw("fn") || tok.is_kw("pub") {
                node.methods.push(self.fn_def(Vec::new())?);
            } else if let Tok::Name(n) = &tok.tok {
                let n = n.clone();
                if self.peek_at(1).is_op(":") {
                    self.advance();
                    self.advance();
                    let ty = self.type_field(true)?;
                    let mut default = None;
                    if self.at_op("=") {
                        self.advance();
                        default = Some(self.expr()?);
                    }
                    // annotation_inline, e.g. @private
                    let mut marks = Vec::new();
                    while self.at_op("@") {
                        self.advance();
                        marks.push(self.dotted_name()?);
                        if self.at_op("(") {
                            self.advance();
                            self.args(")")?;
                            self.expect_op(")")?;
                        }
                    }
                    self.expect_newline()?;
                    node.fields.push(FieldDef { span: tok.span, name: n, ty, default, marks });
                } else if self.peek_at(1).is_op("=") {
                    // keep = 5 years after last_activity, purpose = "..." (D116)
                    let e = self.cfg_entry("type", None, &mut settings_seen)?;
                    node.settings.push((n, e));
                } else {
                    self.advance();
                    let params = if self.at_op("(") { self.params()? } else { Vec::new() };
                    self.expect_newline()?;
                    node.variants.push(VariantDef { span: self.span_from(tok.span), name: n, params });
                }
            } else {
                return Err(self.error(&word("name")));
            }
        }
        self.advance();
        let names = node
            .fields
            .iter()
            .map(|f| (f.name.as_str(), f.span))
            .chain(node.variants.iter().map(|v| (v.name.as_str(), v.span)))
            .chain(node.methods.iter().map(|m| (m.name.as_str(), m.name_span)));
        check_unique(names)?;
        if !node.fields.is_empty() && !node.variants.is_empty() {
            return Err(Diagnostic::new("type_mixed", node.name_span).arg("name", node.name.clone()));
        }
        Ok(())
    }

    /// trait_def = "trait" IDENT ":" NEWLINE INDENT { fn_sig NEWLINE } DEDENT
    fn trait_def(&mut self) -> PResult<Stmt> {
        let start = self.advance();
        let (name, _) = self.expect_name()?;
        self.expect_op(":")?;
        self.expect_newline_raw()?;
        if self.peek().tok != Tok::Indent {
            return Err(self.error(&word("indented")));
        }
        self.advance();
        let mut sigs = Vec::new();
        while self.peek().tok != Tok::Dedent {
            let tok = self.expect_kw("fn")?;
            let (fname, _) = self.expect_name()?;
            self.params()?;
            if self.at_op("->") {
                self.advance();
                self.type_()?;
            }
            self.expect_newline()?;
            sigs.push(FnSig { span: tok.span, name: fname });
        }
        self.advance();
        Ok(self.stmt_node(start.span, StmtKind::Trait { name, sigs }))
    }

    /// type = base_type [ "?" | "!" ] | literal { "|" literal }
    fn type_(&mut self) -> PResult<TypeExpr> {
        self.type_field(false)
    }

    /// Also `1..5` (an int in a range, section 8) and, in fields, `ref T`
    /// and `many T` (nexo.db, section 6).
    fn type_field(&mut self, field: bool) -> PResult<TypeExpr> {
        let tok = self.peek().clone();
        if let Tok::Int(low) = &tok.tok {
            if self.peek_at(1).is_op("..") {
                let low: i64 = low.parse().unwrap_or(0);
                self.advance();
                self.advance();
                let Tok::Int(high) = self.peek().tok.clone() else { return Err(self.error(&word("expr"))) };
                self.advance();
                let high: i64 = high.parse().unwrap_or(0);
                return Ok(TypeExpr::Range { span: self.span_from(tok.span), low, high });
            }
        }
        if field && self.is_name_at(1) {
            if let Tok::Name(w) = &tok.tok {
                if w == "ref" || w == "many" {
                    let rel: &'static str = if w == "ref" { "ref" } else { "many" };
                    self.advance();
                    let TypeExpr::Name(mut inner) = self.type_()? else { return Err(self.error(&word("type"))) };
                    inner.rel = Some(rel);
                    inner.span = self.span_from(tok.span);
                    return Ok(TypeExpr::Name(inner));
                }
            }
        }
        if is_literal_start(&tok) {
            let mut literals = vec![self.literal()?];
            while self.at_op("|") {
                self.advance();
                literals.push(self.literal()?);
            }
            return Ok(TypeExpr::Union { span: self.span_from(tok.span), literals });
        }
        let mut node = self.base_type()?;
        if self.at_op("?") {
            self.advance();
            node.optional = true;
        } else if self.at_op("!") {
            self.advance();
            node.fallible = true;
        }
        node.span = self.span_from(tok.span);
        Ok(TypeExpr::Name(node))
    }

    /// base_type = dotted_name [ type_params ] | "weak" base_type
    fn base_type(&mut self) -> PResult<TypeName> {
        let tok = self.peek().clone();
        if tok.is_kw("weak") {
            self.advance();
            let mut inner = self.base_type()?;
            inner.weak = true;
            return Ok(inner);
        }
        if !self.is_name() {
            return Err(self.error(&word("type")));
        }
        let name = self.dotted_name()?;
        let params = if self.at_op("[") { self.type_params()? } else { Vec::new() };
        Ok(TypeName {
            span: self.span_from(tok.span),
            name,
            params,
            optional: false,
            fallible: false,
            weak: false,
            rel: None,
        })
    }

    fn literal(&mut self) -> PResult<Expr> {
        if is_literal_start(self.peek()) {
            return self.primary();
        }
        Err(self.error(&word("expr")))
    }

    fn if_stmt(&mut self) -> PResult<Stmt> {
        let start = self.advance();
        let cond = self.expr()?;
        let body = self.block()?;
        let mut branches = vec![(cond, body)];
        while self.at_kw("elif") {
            self.advance();
            let c = self.expr()?;
            let b = self.block()?;
            branches.push((c, b));
        }
        let mut orelse = None;
        if self.at_kw("else") {
            self.advance();
            orelse = Some(self.block()?);
        }
        Ok(self.stmt_node(start.span, StmtKind::If { branches, orelse }))
    }

    fn for_stmt(&mut self) -> PResult<Stmt> {
        let start = self.advance();
        let (var, _) = self.expect_name()?;
        self.expect_kw("in")?;
        let iterable = self.expr()?;
        let body = self.block()?;
        Ok(self.stmt_node(start.span, StmtKind::For { var, iterable, body }))
    }

    /// match_stmt = "match" expr ":" NEWLINE INDENT match_arm { match_arm } DEDENT
    fn match_stmt(&mut self) -> PResult<Stmt> {
        let start = self.advance();
        let subject = self.expr()?;
        self.expect_op(":")?;
        self.expect_newline_raw()?;
        if self.peek().tok != Tok::Indent {
            return Err(self.error(&word("indented")));
        }
        self.advance();
        let mut arms = Vec::new();
        while self.peek().tok != Tok::Dedent {
            arms.push(self.match_arm()?);
        }
        self.advance();
        Ok(self.stmt_node(start.span, StmtKind::Match { subject, arms }))
    }

    /// match_arm = pattern { "or" pattern } [ "if" expr ] ( ":" simple_stmt NEWLINE | block )
    fn match_arm(&mut self) -> PResult<MatchArm> {
        let start = self.peek().span;
        let mut patterns = vec![self.pattern()?];
        while self.at_kw("or") {
            self.advance();
            patterns.push(self.pattern()?);
        }
        let mut guard = None;
        if self.at_kw("if") {
            self.advance();
            guard = Some(self.expr()?);
        }
        self.expect_op(":")?;
        let body = if self.peek().tok == Tok::Newline {
            self.indented_body()?
        } else {
            let s = self.simple_stmt()?;
            self.expect_newline()?;
            vec![s]
        };
        Ok(MatchArm { span: start, patterns, guard, body })
    }

    /// pattern = "_" | literal | IDENT [ "(" pattern { "," pattern } ")" ]
    fn pattern(&mut self) -> PResult<Pattern> {
        let tok = self.peek().clone();
        if matches!(&tok.tok, Tok::Name(n) if n == "_") {
            self.advance();
            return Ok(Pattern::Wild(tok.span));
        }
        if is_literal_start(&tok) {
            let lit = self.literal()?;
            return Ok(Pattern::Lit(tok.span, lit));
        }
        if let Tok::Name(n) = &tok.tok {
            let n = n.clone();
            self.advance();
            if self.at_op("(") {
                self.advance();
                let mut args = vec![self.pattern()?];
                while self.at_op(",") {
                    self.advance();
                    args.push(self.pattern()?);
                }
                self.expect_op(")")?;
                return Ok(Pattern::Ctor(self.span_from(tok.span), n, args));
            }
            return Ok(Pattern::Name(tok.span, n));
        }
        Err(self.error(&word("pattern")))
    }

    /// try_stmt = "try" [ "within" expr ] block else_clause { else_clause }
    /// else_clause = "else" IDENT [ "is" dotted_name ] block
    fn try_stmt(&mut self) -> PResult<Stmt> {
        let start = self.advance();
        let mut within = None;
        if self.at_name("within") {
            self.advance();
            within = Some(self.expr()?);
        }
        let body = self.block()?;
        let mut handlers = Vec::new();
        while self.at_kw("else") {
            let else_tok = self.advance();
            if !self.is_name() {
                if self.at_op(":") {
                    return Err(Diagnostic::new("else_needs_var", else_tok.span));
                }
                return Err(self.error(&word("name")));
            }
            let (var, _) = self.expect_name()?;
            let mut kind = None;
            if self.at_kw("is") {
                self.advance();
                kind = Some(self.dotted_name()?);
            }
            let body = self.block()?;
            handlers.push(Handler { span: else_tok.span, var, kind, body });
        }
        if handlers.is_empty() {
            return Err(self.error(&word("else")));
        }
        Ok(self.stmt_node(start.span, StmtKind::Try { within, body, handlers }))
    }

    /// event_stmt = ( "on" | "when" ) call [ "as" IDENT ] block
    ///            | "every" expr [ "at" expr ] block
    ///            | "lifecycle" IDENT [ "," args ] block
    fn event_stmt(&mut self) -> PResult<Stmt> {
        let tok = self.advance();
        let Tok::Keyword(kind) = tok.tok else { unreachable!() };
        let mut name = String::new();
        let mut params = Vec::new();
        if kind == "on" || kind == "when" {
            let trigger = self.postfix()?;
            if !matches!(trigger.kind, ExprKind::Call { .. }) {
                return Err(self.error_at(&word("call"), &tok));
            }
            let mut alias = None;
            if self.at_kw("as") {
                self.advance();
                alias = Some(self.expect_name()?.0);
            }
            let is_stream = matches!(&trigger.kind, ExprKind::Call { func, .. }
                if matches!(&func.kind, ExprKind::Name(n) if STREAM_SOURCES.contains(&n.as_str())));
            if kind == "when" && is_stream {
                return self.stream_when(tok.span, trigger, alias);
            }
            let ExprKind::Call { func, args } = trigger.kind else { unreachable!() };
            if let ExprKind::Name(n) = func.kind {
                name = n;
            }
            params = args;
        } else if kind == "every" {
            self.expr()?;
            if self.at_kw("at") {
                self.advance();
                self.expr()?;
            }
        } else {
            name = self.expect_name()?.0;
            if self.at_op(",") {
                self.advance();
                self.args(":")?;
            }
        }
        let body = self.block()?;
        Ok(self.stmt_node(tok.span, StmtKind::Event { kind, name, params, body }))
    }

    // -- expressions ----------------------------------------------------
    /// expr = lambda | pipeline ;  lambda = IDENT "->" expr
    pub fn expr(&mut self) -> PResult<Expr> {
        let tok = self.peek().clone();
        if let Tok::Name(n) = &tok.tok {
            if self.peek_at(1).is_op("->") {
                let param = n.clone();
                self.advance();
                self.advance();
                let body = self.expr()?;
                let span = self.span_from(tok.span);
                return Ok(self.expr_node(span, ExprKind::Lambda { param, body: Box::new(body) }));
            }
        }
        self.pipeline()
    }

    /// pipeline = or_expr { "|>" pipe_step }
    fn pipeline(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let left = self.or_expr()?;
        if !self.at_op("|>") {
            return Ok(left);
        }
        let mut steps = Vec::new();
        while self.at_op("|>") {
            self.advance();
            steps.push(self.pipe_step()?);
        }
        self.check_steps(&steps)?;
        let span = self.span_from(start);
        Ok(self.expr_node(span, ExprKind::Pipeline { source: Box::new(left), steps }))
    }

    /// pipe_step = IDENT { expr | IDENT } | call
    fn pipe_step(&mut self) -> PResult<PipeStep> {
        let tok = self.peek().clone();
        if let Tok::Name(w) = &tok.tok {
            if let Some(kind) = StepKind::from_word(w) {
                if !(self.peek_at(1).is_op("(") || self.peek_at(1).is_op(".")) {
                    self.advance();
                    let mut step = PipeStep { span: tok.span, kind, expr: None, columns: Vec::new(), desc: false };
                    match kind {
                        StepKind::Group | StepKind::Sort => {
                            if !self.at_name("by") {
                                return Err(self.error(&word("by")));
                            }
                            self.advance();
                            step.expr = Some(self.or_expr()?);
                            if kind == StepKind::Sort && self.at_name("desc") {
                                self.advance();
                                step.desc = true;
                            }
                        }
                        StepKind::Pick => {
                            step.columns.push(self.or_expr()?);
                            while self.at_op(",") {
                                self.advance();
                                step.columns.push(self.or_expr()?);
                            }
                        }
                        StepKind::Sum | StepKind::Avg => {
                            if self.starts_expr() {
                                step.expr = Some(self.or_expr()?);
                            }
                        }
                        StepKind::Count => {}
                        _ => {
                            if !self.starts_expr() {
                                return Err(Diagnostic::new("pipe_step_arg", tok.span)
                                    .arg("what", word("expr"))
                                    .arg("step", kind.word())
                                    .arg("example", format!("{} it", kind.word())));
                            }
                            step.expr = Some(self.or_expr()?);
                        }
                    }
                    step.span = self.span_from(tok.span);
                    return Ok(step);
                }
            }
        }
        let expr = self.unary()?;
        if !matches!(expr.kind, ExprKind::Name(_) | ExprKind::Attr { .. } | ExprKind::Call { .. } | ExprKind::Lambda { .. }) {
            return Err(self.error_at(&word("call"), &tok));
        }
        Ok(PipeStep { span: self.span_from(tok.span), kind: StepKind::Call, expr: Some(expr), columns: Vec::new(), desc: false })
    }

    fn starts_expr(&self) -> bool {
        let tok = self.peek();
        match &tok.tok {
            Tok::Newline | Tok::Indent | Tok::Dedent | Tok::Eof => false,
            Tok::Op(o) => !matches!(*o, "|>" | ")" | "]" | "}" | ":" | "," | "="),
            Tok::Keyword(k) => matches!(*k, "true" | "false" | "none" | "not" | "self" | "parallel"),
            _ => true,
        }
    }

    /// Static rules: sum/avg of a column need a preceding group by, and only
    /// sum, count or avg can follow group by (D14).
    fn check_steps(&self, steps: &[PipeStep]) -> PResult<()> {
        let mut grouped = false;
        for step in steps {
            if grouped {
                if !matches!(step.kind, StepKind::Sum | StepKind::Count | StepKind::Avg) {
                    return Err(Diagnostic::new("pipe_group_next", step.span));
                }
                if step.kind != StepKind::Count && step.expr.is_none() {
                    return Err(Diagnostic::new("pipe_step_arg", step.span)
                        .arg("step", step.kind.word())
                        .arg("what", word("name"))
                        .arg("example", format!("{} amount", step.kind.word())));
                }
                grouped = false;
            } else if matches!(step.kind, StepKind::Sum | StepKind::Avg) {
                if let Some(e) = &step.expr {
                    let col = source_text(self.source, e.span);
                    return Err(Diagnostic::new("pipe_needs_group", step.span)
                        .arg("step", step.kind.word())
                        .arg("col", col));
                }
            }
            if step.kind == StepKind::Group {
                grouped = true;
            }
        }
        Ok(())
    }

    fn or_expr(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let mut left = self.and_expr()?;
        while self.at_kw("or") {
            self.advance();
            let right = self.and_expr()?;
            let span = self.span_from(start);
            left = self.expr_node(span, ExprKind::BoolOp { op: "or", left: Box::new(left), right: Box::new(right) });
        }
        Ok(left)
    }

    fn and_expr(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let mut left = self.not_expr()?;
        while self.at_kw("and") {
            self.advance();
            let right = self.not_expr()?;
            let span = self.span_from(start);
            left = self.expr_node(span, ExprKind::BoolOp { op: "and", left: Box::new(left), right: Box::new(right) });
        }
        Ok(left)
    }

    fn not_expr(&mut self) -> PResult<Expr> {
        let tok = self.peek().clone();
        if tok.is_kw("not") {
            self.advance();
            let operand = self.not_expr()?;
            let span = self.span_from(tok.span);
            return Ok(self.expr_node(span, ExprKind::Unary { op: "not", operand: Box::new(operand) }));
        }
        self.comparison()
    }

    fn comparison(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let first = self.coalesce()?;
        let mut ops = Vec::new();
        let mut operands = vec![first];
        loop {
            let op = match &self.peek().tok {
                Tok::Op(o) if COMPARE_OPS.contains(o) => *o,
                Tok::Keyword("in") => "in",
                Tok::Keyword("is") => "is",
                _ => break,
            };
            self.advance();
            ops.push(op);
            operands.push(self.coalesce()?);
        }
        if ops.is_empty() {
            return Ok(operands.pop().unwrap());
        }
        let span = self.span_from(start);
        Ok(self.expr_node(span, ExprKind::Compare { ops, operands }))
    }

    fn coalesce(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let mut left = self.range_expr()?;
        while self.at_op("??") {
            self.advance();
            let right = self.range_expr()?;
            let span = self.span_from(start);
            left = self.expr_node(span, ExprKind::Coalesce { left: Box::new(left), right: Box::new(right) });
        }
        Ok(left)
    }

    fn range_expr(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let left = self.sum()?;
        if self.at_op("..") {
            self.advance();
            let right = self.sum()?;
            let span = self.span_from(start);
            return Ok(self.expr_node(span, ExprKind::Range { start: Box::new(left), end: Box::new(right) }));
        }
        Ok(left)
    }

    fn sum(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let mut left = self.term()?;
        while self.at_op("+") || self.at_op("-") {
            let Tok::Op(op) = self.advance().tok else { unreachable!() };
            let right = self.term()?;
            let span = self.span_from(start);
            left = self.expr_node(span, ExprKind::BinOp { op, left: Box::new(left), right: Box::new(right) });
        }
        Ok(left)
    }

    fn term(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let mut left = self.power()?;
        while self.at_ops(&["*", "/", "//", "%"]) {
            let Tok::Op(op) = self.advance().tok else { unreachable!() };
            let right = self.power()?;
            let span = self.span_from(start);
            left = self.expr_node(span, ExprKind::BinOp { op, left: Box::new(left), right: Box::new(right) });
        }
        Ok(left)
    }

    fn power(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let base = self.unary()?;
        if self.at_op("**") {
            self.advance();
            let exp = self.power()?;
            let span = self.span_from(start);
            return Ok(self.expr_node(span, ExprKind::BinOp { op: "**", left: Box::new(base), right: Box::new(exp) }));
        }
        Ok(base)
    }

    fn unary(&mut self) -> PResult<Expr> {
        let tok = self.peek().clone();
        if tok.is_op("-") {
            self.advance();
            let operand = self.unary()?;
            let span = self.span_from(tok.span);
            return Ok(self.expr_node(span, ExprKind::Unary { op: "-", operand: Box::new(operand) }));
        }
        self.postfix()
    }

    /// postfix = primary { "." IDENT | "(" [ args ] ")" | "[" expr "]" | "?" }
    fn postfix(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let mut node = self.primary()?;
        loop {
            if self.phrase_mode && (self.at_op("(") || self.at_op("[")) && self.spaced() {
                return Ok(node);
            }
            if self.at_op(".") {
                self.advance();
                let Tok::Name(name) = self.peek().tok.clone() else {
                    return Err(self.error(&word("member")));
                };
                let name_span = self.advance().span;
                let span = self.span_from(start);
                node = self.expr_node(span, ExprKind::Attr { obj: Box::new(node), name, name_span });
            } else if self.at_op("(") {
                self.advance();
                let mut args = self.args(")")?;
                self.expect_op(")")?;
                let is_sql = matches!(&node.kind, ExprKind::Attr { name, .. } if name == "sql");
                if self.db && is_sql && args.len() == 1 && args[0].name.is_none() && matches!(args[0].value.kind, ExprKind::Str(_)) {
                    let value = std::mem::replace(&mut args[0].value, Expr { id: 0, span: start, kind: ExprKind::None });
                    args[0].value = self.sql_text(value);
                }
                let span = self.span_from(start);
                node = self.expr_node(span, ExprKind::Call { func: Box::new(node), args });
            } else if self.at_op("[") {
                self.advance();
                let index = self.expr()?;
                self.expect_op("]")?;
                let span = self.span_from(start);
                node = self.expr_node(span, ExprKind::Index { obj: Box::new(node), index: Box::new(index) });
            } else if self.at_op("?") {
                self.advance();
                let span = self.span_from(start);
                node = self.expr_node(span, ExprKind::Propagate(Box::new(node)));
            } else {
                return Ok(node);
            }
        }
    }

    /// args = arg { "," arg } ;  arg = [ IDENT ":" ] expr
    fn args(&mut self, closer: &str) -> PResult<Vec<Arg>> {
        let mut result = Vec::new();
        if self.at_op(closer) {
            return Ok(result);
        }
        loop {
            let tok = self.peek().clone();
            let mut name = None;
            if matches!(tok.tok, Tok::Name(_) | Tok::Keyword(_)) && self.peek_at(1).is_op(":") {
                name = Some(match &tok.tok {
                    Tok::Name(n) => n.clone(),
                    Tok::Keyword(k) => k.to_string(),
                    _ => unreachable!(),
                });
                self.advance();
                self.advance();
            }
            let value = self.expr()?;
            result.push(Arg { span: self.span_from(tok.span), name, value });
            if !self.at_op(",") {
                return Ok(result);
            }
            self.advance();
        }
    }

    fn primary(&mut self) -> PResult<Expr> {
        let tok = self.peek().clone();
        match &tok.tok {
            Tok::Int(v) => {
                self.advance();
                return Ok(self.expr_node(tok.span, ExprKind::Num { text: v.clone(), is_float: false }));
            }
            Tok::Float(v) => {
                self.advance();
                return Ok(self.expr_node(tok.span, ExprKind::Num { text: v.clone(), is_float: true }));
            }
            Tok::Unit { text, is_float, unit } => {
                self.advance();
                let kind = ExprKind::Unit { text: text.clone(), is_float: *is_float, unit: unit.clone() };
                return Ok(self.expr_node(tok.span, kind));
            }
            Tok::Str(parts) => {
                self.advance();
                let mut pieces = Vec::new();
                for p in parts {
                    match p {
                        StrPart::Lit(s) => pieces.push(StrPiece::Lit(s.clone())),
                        StrPart::Interp { source, line, col } => {
                            pieces.push(StrPiece::Expr(self.interpolation(source, *line, *col)?))
                        }
                    }
                }
                return Ok(self.expr_node(tok.span, ExprKind::Str(pieces)));
            }
            Tok::Name(n) => {
                self.advance();
                return Ok(self.expr_node(tok.span, ExprKind::Name(n.clone())));
            }
            Tok::Keyword(k) => match *k {
                "true" | "false" => {
                    self.advance();
                    return Ok(self.expr_node(tok.span, ExprKind::Bool(*k == "true")));
                }
                "none" => {
                    self.advance();
                    return Ok(self.expr_node(tok.span, ExprKind::None));
                }
                "self" => {
                    self.advance();
                    return Ok(self.expr_node(tok.span, ExprKind::Name("self".into())));
                }
                "parallel" if self.peek_at(1).is_kw("for") => return self.parallel_for(),
                // test() in a step of a pipeline runs the tests (D122)
                "test" if self.peek_at(1).is_op("(") => {
                    self.advance();
                    return Ok(self.expr_node(tok.span, ExprKind::Name("test".into())));
                }
                _ => {}
            },
            Tok::Op("(") => {
                self.advance();
                let mut inner = self.expr()?;
                self.expect_op(")")?;
                inner.span = self.span_from(tok.span);
                return Ok(inner);
            }
            Tok::Op("[") => return self.list_lit(),
            Tok::Op("{") => return self.map_lit(),
            _ => {}
        }
        Err(self.error(&word("expr")))
    }

    fn interpolation(&mut self, source: &str, line: u32, col: u32) -> PResult<Expr> {
        let tokens = tokenize_expr(source, line, col)?;
        let mut sub = Parser::new(tokens, self.source, true, self.next_id);
        let node = sub.expr()?;
        if sub.peek().tok != Tok::Eof {
            return Err(sub.error("}"));
        }
        self.next_id = sub.next_id;
        Ok(node)
    }

    /// "parallel" "for" IDENT "in" expr [ "," args ] block
    fn parallel_for(&mut self) -> PResult<Expr> {
        let start = self.advance();
        self.advance();
        let (var, _) = self.expect_name()?;
        self.expect_kw("in")?;
        let iterable = self.expr()?;
        let mut args = Vec::new();
        if self.at_op(",") {
            self.advance();
            args = self.args(":")?;
        }
        let span = self.span_from(start.span);
        let body = self.block()?;
        self.block_end = self.p;
        Ok(self.expr_node(span, ExprKind::ParallelFor { var, iterable: Box::new(iterable), args, body }))
    }

    /// list_lit = "[" [ expr { "," expr } ] "]"
    /// comprehension = "[" expr "for" IDENT "in" expr [ "if" expr ] "]"
    fn list_lit(&mut self) -> PResult<Expr> {
        let start = self.advance();
        if self.at_op("]") {
            self.advance();
            let span = self.span_from(start.span);
            return Ok(self.expr_node(span, ExprKind::List(Vec::new())));
        }
        let first = self.expr()?;
        if self.at_kw("for") {
            self.advance();
            let (var, _) = self.expect_name()?;
            self.expect_kw("in")?;
            let iterable = self.expr()?;
            let mut condition = None;
            if self.at_kw("if") {
                self.advance();
                condition = Some(Box::new(self.expr()?));
            }
            self.expect_op("]")?;
            let span = self.span_from(start.span);
            let kind = ExprKind::Comprehension { element: Box::new(first), var, iterable: Box::new(iterable), condition };
            return Ok(self.expr_node(span, kind));
        }
        let mut items = vec![first];
        while self.at_op(",") {
            self.advance();
            items.push(self.expr()?);
        }
        self.expect_op("]")?;
        let span = self.span_from(start.span);
        Ok(self.expr_node(span, ExprKind::List(items)))
    }

    /// map_lit = "{" [ expr ":" expr { "," expr ":" expr } ] "}"
    fn map_lit(&mut self) -> PResult<Expr> {
        let start = self.advance();
        let mut pairs = Vec::new();
        if !self.at_op("}") {
            loop {
                let key = self.expr()?;
                self.expect_op(":")?;
                let value = self.expr()?;
                pairs.push((key, value));
                if !self.at_op(",") {
                    break;
                }
                self.advance();
            }
        }
        self.expect_op("}")?;
        let span = self.span_from(start.span);
        Ok(self.expr_node(span, ExprKind::Map(pairs)))
    }
}

// ------------------------------------------------------------------ modules
// Module blocks and annotations desugar into ordinary statements that call
// builtins whose names start with "__" (D90), so every later pass (the
// checker, the resolver, the code generator) sees only core constructs.

const STREAM_SOURCES: &[&str] = &["kafka", "rabbit", "sqs", "kinesis", "servicebus", "eventhub", "pubsub", "websocket"];
const MODULE_BLOCKS: &[&str] = &["ai", "agent", "server", "infra", "stage", "pipeline", "flags"];
/// blocks of phase 5 whose body is read with the rule of D114
const CFG_BLOCKS: &[&str] = &["infra", "stage", "pipeline", "flags"];
/// keywords that start an expression; any other one is a word in a phrase
const EXPR_KEYWORDS: &[&str] = &["true", "false", "none", "not", "self", "parallel"];
const ROUTE_METHODS: &[&str] = &["get", "post", "put", "patch", "delete"];
/// keys whose bare names are model aliases (section 8)
const MODEL_KEYS: &[&str] = &["model", "fallback", "when_over_budget"];

pub fn is_identifier(text: &str) -> bool {
    let mut chars = text.chars();
    match chars.next() {
        Some(c) if c.is_alphabetic() || c == '_' => {}
        _ => return false,
    }
    chars.all(|c| c.is_alphanumeric() || c == '_') && !crate::lexer::KEYWORDS.contains(&text)
}

struct Route {
    func: FnDef,
    method: String,
    path: String,
    annotations: Vec<Annotation>,
    span: Span,
}

fn str_text(parts: &[StrPart]) -> String {
    parts
        .iter()
        .map(|p| match p {
            StrPart::Lit(s) => s.clone(),
            StrPart::Interp { source, .. } => format!("{{{source}}}"),
        })
        .collect()
}

impl<'s> Parser<'s> {
    fn text(&mut self, span: Span, value: &str) -> Expr {
        self.expr_node(span, ExprKind::Str(vec![StrPiece::Lit(value.to_string())]))
    }

    fn name_expr(&mut self, span: Span, name: &str) -> Expr {
        self.expr_node(span, ExprKind::Name(name.to_string()))
    }

    fn call(&mut self, span: Span, fname: &str, args: Vec<Expr>, named: Vec<(String, Expr)>) -> Expr {
        let func = self.name_expr(span, fname);
        let mut out: Vec<Arg> = args.into_iter().map(|value| Arg { span, name: None, value }).collect();
        out.extend(named.into_iter().map(|(n, value)| Arg { span, name: Some(n), value }));
        self.expr_node(span, ExprKind::Call { func: Box::new(func), args: out })
    }

    fn expr_stmt(&mut self, e: Expr) -> Stmt {
        self.stmt_node(e.span, StmtKind::Expr(e))
    }

    /// A bare name in a model position is the name of a model (an alias).
    fn model_name(&mut self, e: Expr) -> Expr {
        match e.kind {
            ExprKind::Name(n) => self.text(e.span, &n),
            ExprKind::List(items) => {
                let items = items.into_iter().map(|i| self.model_name(i)).collect();
                self.expr_node(e.span, ExprKind::List(items))
            }
            kind => Expr { id: e.id, span: e.span, kind },
        }
    }

    /// The type of a parameter as a value the runtime can check against.
    fn type_value(&mut self, t: &Option<TypeExpr>, span: Span) -> Expr {
        match t {
            Some(TypeExpr::Name(tn)) => self.name_expr(tn.span, &tn.name.clone()),
            _ => self.expr_node(span, ExprKind::None),
        }
    }

    /// db.sql("... {x} ..."): the literal parts and the values apart, so
    /// the values become parameters (section 6, D100).
    fn sql_text(&mut self, s: Expr) -> Expr {
        let span = s.span;
        let ExprKind::Str(pieces) = s.kind else { unreachable!() };
        let mut parts = vec![String::new()];
        let mut values = Vec::new();
        for p in pieces {
            match p {
                StrPiece::Lit(t) => parts.last_mut().unwrap().push_str(&t),
                StrPiece::Expr(x) => {
                    values.push(x);
                    parts.push(String::new());
                }
            }
        }
        let parts: Vec<Expr> = parts.iter().map(|p| self.text(span, p)).collect();
        let parts = self.expr_node(span, ExprKind::List(parts));
        let values = self.expr_node(span, ExprKind::List(values));
        self.call(span, "__sql_text", vec![parts, values], Vec::new())
    }

    /// `10 per minute per ip` -> __rate(10, "minute", "ip")
    fn rate(&mut self, value: Expr) -> PResult<Expr> {
        if !self.at_name("per") {
            return Ok(value);
        }
        let start = value.span;
        let mut args = vec![value];
        while self.at_name("per") {
            self.advance();
            let (w, sp) = self.expect_name()?;
            args.push(self.text(sp, &w));
        }
        let span = self.span_from(start);
        Ok(self.call(span, "__rate", args, Vec::new()))
    }

    /// ai:, ai models:, ai profile "name":, agent name:, server name:
    fn module_block(&mut self, tok: Token) -> PResult<Stmt> {
        let Tok::Name(word_) = tok.tok.clone() else { unreachable!() };
        if CFG_BLOCKS.contains(&word_.as_str()) {
            return self.cfg_block(tok, &word_);
        }
        let mut name: Option<String> = None;
        let mut name_span = tok.span;
        let kind: &str = if word_ == "ai" && self.at_name("profile") {
            self.advance();
            let Tok::Str(parts) = self.peek().tok.clone() else { return Err(self.error(&word("string"))) };
            self.advance();
            name = Some(
                parts
                    .iter()
                    .filter_map(|p| match p {
                        StrPart::Lit(s) => Some(s.as_str()),
                        _ => None,
                    })
                    .collect(),
            );
            "profile"
        } else if word_ == "ai" && self.at_name("models") {
            self.advance();
            "models"
        } else if word_ == "ai" {
            "ai"
        } else {
            let (n, sp) = self.expect_name()?;
            name = Some(n);
            name_span = sp;
            if word_ == "agent" {
                "agent"
            } else {
                "server"
            }
        };
        self.expect_op(":")?;
        self.expect_newline_raw()?;
        if self.peek().tok != Tok::Indent {
            return Err(self.error(&word("indented")));
        }
        self.advance();
        let mut pairs: Vec<(String, Expr)> = Vec::new();
        let mut routes: Vec<Route> = Vec::new();
        while self.peek().tok != Tok::Dedent {
            let t = self.peek().clone();
            let is_route = matches!(&t.tok, Tok::Name(m) if ROUTE_METHODS.contains(&m.as_str()))
                && matches!(self.peek_at(1).tok, Tok::Str(_));
            if kind == "server" && (t.is_op("@") || is_route) {
                routes.push(self.route()?);
                continue;
            }
            let (key, key_span) = self.expect_name()?;
            if !self.at_op("=") {
                return Err(self.error("\"=\""));
            }
            self.advance();
            let v = self.expr()?;
            let mut value = self.rate(v)?;
            if MODEL_KEYS.contains(&key.as_str()) && kind != "models" {
                value = self.model_name(value);
            }
            if pairs.iter().any(|(n, _)| *n == key) {
                return Err(Diagnostic::new("duplicate_name", key_span).arg("name", key));
            }
            pairs.push((key, value));
            self.expect_newline()?;
        }
        self.advance();
        self.block_end = self.p;
        let span = tok.span;
        if kind == "models" {
            let c = self.call(span, "__ai_models", Vec::new(), pairs);
            return Ok(self.expr_stmt(c));
        }
        if kind == "ai" || kind == "profile" {
            let first = match &name {
                Some(n) => self.text(span, &n.clone()),
                None => self.expr_node(span, ExprKind::None),
            };
            let c = self.call(span, "__ai_config", vec![first], pairs);
            return Ok(self.expr_stmt(c));
        }
        let name = name.unwrap();
        let fname = if kind == "agent" { "__ai_agent" } else { "__web_server" };
        let first = self.text(span, &name);
        let value = self.call(span, fname, vec![first], pairs);
        let target = self.name_expr(name_span, &name);
        let stmt = self.stmt_node(name_span, StmtKind::Assign { target, op: "=", value, ty: None });
        for r in routes {
            let mut named: Vec<(String, Expr)> = Vec::new();
            for a in &r.annotations {
                match (a.name.as_str(), a.args.len()) {
                    ("role", 1) => named.push(("role".into(), a.args[0].value.clone())),
                    ("limit", 1) => named.push(("limit".into(), a.args[0].value.clone())),
                    ("public", 0) => {
                        let b = self.expr_node(a.span, ExprKind::Bool(true));
                        named.push(("public".into(), b));
                    }
                    _ => {
                        return Err(Diagnostic::new("not_supported", a.span).arg("what", format!("\"@{}\"", a.name)))
                    }
                }
            }
            let mspan = r.span;
            let types: Vec<Expr> = r.func.params.iter().map(|p| p.ty.clone()).collect::<Vec<_>>().iter().map(|t| self.type_value(t, mspan)).collect();
            let types = self.expr_node(mspan, ExprKind::List(types));
            let fname = r.func.name.clone();
            let fstmt = self.stmt_node(r.func.span, StmtKind::Fn(r.func));
            self.extra.push(fstmt);
            let server = self.name_expr(mspan, &name);
            let method = self.text(mspan, &r.method);
            let path = self.text(mspan, &r.path);
            let handler = self.name_expr(mspan, &fname);
            let c = self.call(mspan, "__web_route", vec![server, method, path, handler, types], named);
            let st = self.expr_stmt(c);
            self.extra.push(st);
        }
        Ok(stmt)
    }

    // -- phase 5 blocks (D114) ------------------------------------------

    /// infra NAME:, stage NAME:, pipeline NAME:, flags: as
    /// __infra("NAME", [entries]) and the like, each entry a __cfg(...).
    fn cfg_block(&mut self, tok: Token, word_: &str) -> PResult<Stmt> {
        let mut name = None;
        if word_ != "flags" {
            name = Some(self.expect_name()?.0);
        }
        self.expect_op(":")?;
        self.expect_newline_raw()?;
        if self.peek().tok != Tok::Indent {
            return Err(self.error(&word("indented")));
        }
        self.advance();
        let saved = std::mem::take(&mut self.extra);
        let entries = self.cfg_entries(word_, name.as_deref());
        let made = std::mem::replace(&mut self.extra, saved);
        let entries = entries?;
        self.block_end = self.p;
        let span = tok.span;
        let mut args = Vec::new();
        if let Some(n) = &name {
            args.push(self.text(span, &n.clone()));
        }
        args.push(self.expr_node(span, ExprKind::List(entries)));
        let c = self.call(span, &format!("__{word_}"), args, Vec::new());
        let stmt = self.expr_stmt(c);
        // the functions of the steps come first, then the block that names them
        if made.is_empty() {
            return Ok(stmt);
        }
        let mut made = made.into_iter();
        let first = made.next().unwrap();
        let mut rest: Vec<Stmt> = made.collect();
        rest.push(stmt);
        rest.append(&mut self.extra);
        self.extra = rest;
        Ok(first)
    }

    /// { entry } DEDENT, the DEDENT consumed.
    fn cfg_entries(&mut self, block: &str, name: Option<&str>) -> PResult<Vec<Expr>> {
        let mut entries = Vec::new();
        let mut seen: Vec<String> = Vec::new();
        while self.peek().tok != Tok::Dedent {
            entries.push(self.cfg_entry(block, name, &mut seen)?);
        }
        self.advance();
        Ok(entries)
    }

    fn cfg_entry(&mut self, block: &str, name: Option<&str>, seen: &mut Vec<String>) -> PResult<Expr> {
        let start = self.peek().clone();
        let mut head = vec![self.phrase()?];
        let mut values: Option<Vec<Expr>> = None;
        let mut options: Vec<(String, Expr)> = Vec::new();
        if self.at_op("=") {
            self.advance();
            let mut v = vec![self.phrase()?];
            self.cfg_items(&mut v, &mut options)?;
            values = Some(v);
            if let ExprKind::List(items) = &head[0].kind {
                if let [Expr { kind: ExprKind::Str(parts), .. }] = items.as_slice() {
                    let key: String = parts
                        .iter()
                        .map(|p| match p {
                            StrPiece::Lit(s) => s.as_str(),
                            _ => "",
                        })
                        .collect();
                    if seen.contains(&key) {
                        return Err(Diagnostic::new("duplicate_name", start.span).arg("name", key));
                    }
                    seen.push(key);
                }
            }
        } else {
            self.cfg_items(&mut head, &mut options)?;
        }
        let first_word = match &head[0].kind {
            ExprKind::List(items) => match items.first().map(|i| &i.kind) {
                Some(ExprKind::Str(parts)) => match parts.as_slice() {
                    [StrPiece::Lit(s)] => Some(s.clone()),
                    _ => None,
                },
                _ => None,
            },
            _ => None,
        };
        let span = start.span;
        let body = if self.at_op(":") && self.peek_at(1).tok == Tok::Newline {
            if block == "pipeline" && matches!(first_word.as_deref(), Some("step") | Some("after")) && values.is_none() {
                let h = head[0].clone();
                self.pipeline_code(name.unwrap_or(""), &h, span)?
            } else {
                self.advance();
                self.expect_newline_raw()?;
                if self.peek().tok != Tok::Indent {
                    return Err(self.error(&word("indented")));
                }
                self.advance();
                let inner = self.cfg_entries(block, name)?;
                self.expr_node(span, ExprKind::List(inner))
            }
        } else {
            self.expect_newline()?;
            self.expr_node(span, ExprKind::None)
        };
        let mut pairs = Vec::new();
        for (k, v) in options {
            let key = self.text(span, &k);
            pairs.push((key, v));
        }
        let opts = self.expr_node(span, ExprKind::Map(pairs));
        let head = self.expr_node(span, ExprKind::List(head));
        let values = match values {
            Some(v) => self.expr_node(span, ExprKind::List(v)),
            None => self.expr_node(span, ExprKind::None),
        };
        Ok(self.call(span, "__cfg", vec![head, values, opts, body], Vec::new()))
    }

    /// { "," item }: a phrase, or NAME ":" atom
    fn cfg_items(&mut self, items: &mut Vec<Expr>, options: &mut Vec<(String, Expr)>) -> PResult<()> {
        while self.at_op(",") {
            self.advance();
            let t = self.peek().clone();
            let word_ = match &t.tok {
                Tok::Name(n) => Some(n.clone()),
                Tok::Keyword(k) => Some(k.to_string()),
                _ => None,
            };
            if let Some(w) = word_.filter(|_| self.peek_at(1).is_op(":") && self.peek_at(2).tok != Tok::Newline) {
                self.advance();
                self.advance();
                if options.iter().any(|(k, _)| *k == w) {
                    return Err(Diagnostic::new("duplicate_name", t.span).arg("name", w));
                }
                let a = self.atom()?;
                options.push((w, a));
            } else {
                items.push(self.phrase()?);
            }
        }
        Ok(())
    }

    /// phrase = atom { atom }
    fn phrase(&mut self) -> PResult<Expr> {
        let start = self.peek().span;
        let mut atoms = vec![self.atom()?];
        while !(matches!(self.peek().tok, Tok::Newline | Tok::Dedent | Tok::Eof)
            || self.at_op(",")
            || self.at_op("=")
            || self.at_op(":"))
        {
            atoms.push(self.atom()?);
        }
        let span = self.span_from(start);
        Ok(self.expr_node(span, ExprKind::List(atoms)))
    }

    fn atom(&mut self) -> PResult<Expr> {
        let tok = self.peek().clone();
        if let Tok::Keyword(k) = tok.tok {
            if !EXPR_KEYWORDS.contains(&k) {
                self.advance();
                return Ok(self.text(tok.span, k));
            }
        }
        if tok.is_op("->") {
            self.advance();
            return Ok(self.text(tok.span, "->"));
        }
        let ok = matches!(tok.tok, Tok::Name(_) | Tok::Int(_) | Tok::Float(_) | Tok::Unit { .. } | Tok::Str(_) | Tok::Keyword(_))
            || tok.is_op("[")
            || tok.is_op("{")
            || tok.is_op("(")
            || tok.is_op("-");
        if !ok {
            return Err(self.error(&word("expr")));
        }
        let old = self.phrase_mode;
        self.phrase_mode = true;
        let e = self.or_expr();
        self.phrase_mode = old;
        let e = e?;
        Ok(self.cfg_value(e))
    }

    /// An atom of a phase 5 block as data (D114): a bare name is a word, a
    /// dotted name a reference, a comparison a condition, 2027-03-01 a date.
    fn cfg_value(&mut self, e: Expr) -> Expr {
        let sp = e.span;
        if let Some(parts) = dotted(&e) {
            if parts.len() == 1 {
                return self.text(sp, &parts[0]);
            }
            let args = parts.iter().map(|p| self.text(sp, p)).collect();
            return self.call(sp, "__ref", args, Vec::new());
        }
        match e.kind {
            ExprKind::Compare { ops, operands } => {
                let mut out: Option<Expr> = None;
                let operands: Vec<Expr> = operands;
                for (i, op) in ops.iter().enumerate() {
                    let o = self.text(sp, op);
                    let l = self.cfg_value(operands[i].clone());
                    let r = self.cfg_value(operands[i + 1].clone());
                    let c = self.call(sp, "__cond", vec![o, l, r], Vec::new());
                    out = Some(match out {
                        None => c,
                        Some(prev) => {
                            let and = self.text(sp, "and");
                            self.call(sp, "__cond", vec![and, prev, c], Vec::new())
                        }
                    });
                }
                out.unwrap()
            }
            ExprKind::BoolOp { op, left, right } => {
                let o = self.text(sp, op);
                let l = self.cfg_value(*left);
                let r = self.cfg_value(*right);
                self.call(sp, "__cond", vec![o, l, r], Vec::new())
            }
            ExprKind::Unary { op: "not", operand } => {
                let o = self.text(sp, "not");
                let x = self.cfg_value(*operand);
                self.call(sp, "__cond", vec![o, x], Vec::new())
            }
            ExprKind::BinOp { op: "-", left, right } => {
                let date = match (&left.kind, &right.kind) {
                    (ExprKind::BinOp { op: "-", left: y, right: m }, ExprKind::Num { text: d, is_float: false }) => {
                        match (&y.kind, &m.kind) {
                            (ExprKind::Num { text: y, is_float: false }, ExprKind::Num { text: m, is_float: false })
                                if y.len() == 4 && m.len() == 2 && d.len() == 2 =>
                            {
                                Some((y.clone(), m.clone(), d.clone(), y_span(&left), m_span(&left), right.span))
                            }
                            _ => None,
                        }
                    }
                    _ => None,
                };
                match date {
                    Some((y, m, d, ys, ms, ds)) => {
                        let mut args = Vec::new();
                        for (t, s) in [(y, ys), (m, ms), (d, ds)] {
                            let n: i64 = t.parse().unwrap_or(0);
                            args.push(self.expr_node(s, ExprKind::Num { text: n.to_string(), is_float: false }));
                        }
                        self.call(sp, "date", args, Vec::new())
                    }
                    None => Expr { id: e.id, span: sp, kind: ExprKind::BinOp { op: "-", left, right } },
                }
            }
            ExprKind::List(items) => {
                let items = items.into_iter().map(|i| self.cfg_value(i)).collect();
                self.expr_node(sp, ExprKind::List(items))
            }
            kind => Expr { id: e.id, span: sp, kind },
        }
    }

    /// The body of `step NAME:` or `after success|failure:` is ordinary
    /// code: a function of the run of the pipeline (D122).
    fn pipeline_code(&mut self, pipeline: &str, head: &Expr, span: Span) -> PResult<Expr> {
        let words: Vec<Option<String>> = match &head.kind {
            ExprKind::List(items) => items
                .iter()
                .map(|i| match &i.kind {
                    ExprKind::Str(parts) => match parts.as_slice() {
                        [StrPiece::Lit(s)] => Some(s.clone()),
                        _ => None,
                    },
                    _ => None,
                })
                .collect(),
            _ => Vec::new(),
        };
        if words.len() != 2 || words[1].is_none() {
            return Err(Diagnostic::new("unexpected", head.span)
                .fix("expected")
                .arg("got", format!("\"{}\"", source_text(self.source, head.span)))
                .arg("expected", word("name")));
        }
        let saved = std::mem::take(&mut self.extra);
        let body = self.block();
        self.extra = saved;
        let body = body?;
        let mut ret = None;
        crate::checker::walk_stmts(&body, false, &mut |s| {
            if matches!(s.kind, StmtKind::Return(_)) && ret.is_none() {
                ret = Some(s.span);
            }
        });
        if let Some(sp) = ret {
            return Err(Diagnostic::new("step_return", sp));
        }
        let fname = format!("{pipeline} {} {}", words[0].clone().unwrap_or_default(), words[1].clone().unwrap());
        let mut assigned: Vec<String> = Vec::new();
        for s in &body {
            match &s.kind {
                StmtKind::Assign { target: Expr { kind: ExprKind::Name(n), .. }, op: "=", .. } => {
                    if !assigned.contains(n) {
                        assigned.push(n.clone());
                    }
                }
                StmtKind::VarDecl { name, .. } => {
                    if !assigned.contains(name) {
                        assigned.push(name.clone());
                    }
                }
                _ => {}
            }
        }
        let mut used = std::collections::HashSet::new();
        crate::checker::walk_stmts(&body, true, &mut |s| stmt_names(s, &mut used));
        let known = self.pipeline_vars.entry(pipeline.to_string()).or_insert_with(|| vec!["version".into(), "step".into()]).clone();
        let mut full = Vec::new();
        for n in known.iter().filter(|n| used.contains(*n)) {
            let run = self.name_expr(span, "__run");
            let key = self.text(span, n);
            let value = self.call(span, "__ci_get", vec![run, key], Vec::new());
            let target = self.name_expr(span, n);
            full.push(self.stmt_node(span, StmtKind::Assign { target, op: "=", value, ty: None }));
        }
        full.extend(body);
        for n in &assigned {
            let run = self.name_expr(span, "__run");
            let key = self.text(span, n);
            let v = self.name_expr(span, n);
            let c = self.call(span, "__ci_keep", vec![run, key, v], Vec::new());
            full.push(self.expr_stmt(c));
        }
        let known = self.pipeline_vars.get_mut(pipeline).unwrap();
        for n in assigned {
            if !known.contains(&n) {
                known.push(n);
            }
        }
        let id = self.id();
        let func = FnDef {
            id,
            span,
            name: fname.clone(),
            name_span: span,
            params: vec![Param { span, name: "__run".into(), ty: None, default: None }],
            ret: None,
            body: full,
            is_pub: false,
            annotations: Vec::new(),
        };
        let st = self.stmt_node(span, StmtKind::Fn(func));
        self.extra.push(st);
        Ok(self.name_expr(span, &fname))
    }

    /// when kafka("orders") as msg[, key: msg.id]: body (section 19), as a
    /// function and __stream_when(source, it, key: (msg) -> msg.id).
    fn stream_when(&mut self, span: Span, source: Expr, alias: Option<String>) -> PResult<Stmt> {
        let mut key = None;
        if self.at_op(",") {
            self.advance();
            if !self.at_name("key") {
                return Err(self.error("\"key\""));
            }
            self.advance();
            self.expect_op(":")?;
            key = Some(self.expr()?);
        }
        let param = alias.unwrap_or_else(|| "__msg".to_string());
        let fname = format!("when {}:{}", span.line, span.col);
        let body = self.block()?;
        let id = self.id();
        let func = FnDef {
            id,
            span,
            name: fname.clone(),
            name_span: span,
            params: vec![Param { span, name: param.clone(), ty: None, default: None }],
            ret: None,
            body,
            is_pub: false,
            annotations: Vec::new(),
        };
        let fstmt = self.stmt_node(span, StmtKind::Fn(func));
        let mut named = Vec::new();
        if let Some(k) = key {
            let ks = k.span;
            let lam = self.expr_node(ks, ExprKind::Lambda { param, body: Box::new(k) });
            named.push(("key".to_string(), lam));
        }
        let fe = self.name_expr(span, &fname);
        let c = self.call(span, "__stream_when", vec![source, fe], named);
        let st = self.expr_stmt(c);
        self.extra.push(st);
        Ok(fstmt)
    }

    /// route = { annotation } METHOD STRING [ "(" params ")" ] block
    fn route(&mut self) -> PResult<Route> {
        let annotations = if self.at_op("@") { self.annotations()? } else { Vec::new() };
        let tok = self.peek().clone();
        let (m, mspan) = self.expect_name()?;
        if !ROUTE_METHODS.contains(&m.as_str()) {
            return Err(self.error_at(&word("route"), &tok));
        }
        let Tok::Str(parts) = self.peek().tok.clone() else { return Err(self.error(&word("string"))) };
        self.advance();
        let path = str_text(&parts);
        let params = if self.at_op("(") { self.params()? } else { Vec::new() };
        let method = m.to_uppercase();
        let span = self.span_from(mspan);
        let body = self.block()?;
        let id = self.id();
        let func = FnDef {
            id,
            span,
            name: format!("{method} {path}"),
            name_span: mspan,
            params,
            ret: None,
            body,
            is_pub: false,
            annotations: Vec::new(),
        };
        Ok(Route { func, method, path, annotations, span: mspan })
    }

    /// @table type T: the type, then __db_table(T, key, many, marks); and
    /// __privacy_type(T, [settings], {marks}) after a type with personal
    /// data or settings (D116).
    fn table_def(&mut self, st: Stmt) -> PResult<Stmt> {
        let mut st = self.table_def_db(st)?;
        let StmtKind::Type(node) = &mut st.kind else { return Ok(st) };
        let personal: Vec<(String, Span, Vec<String>)> = node
            .fields
            .iter()
            .map(|f| (f.name.clone(), f.span, f.marks.iter().filter(|m| *m == "personal" || *m == "private").cloned().collect::<Vec<_>>()))
            .filter(|(_, _, m)| !m.is_empty())
            .collect();
        if node.settings.is_empty() && personal.is_empty() {
            return Ok(st);
        }
        let span = node.name_span;
        let tname = node.name.clone();
        let settings: Vec<Expr> = std::mem::take(&mut node.settings).into_iter().map(|(_, e)| e).collect();
        let settings = self.expr_node(span, ExprKind::List(settings));
        let mut pairs = Vec::new();
        for (n, fs, ms) in personal {
            let k = self.text(fs, &n);
            let items = ms.iter().map(|m| self.text(fs, m)).collect();
            let v = self.expr_node(fs, ExprKind::List(items));
            pairs.push((k, v));
        }
        let marks = self.expr_node(span, ExprKind::Map(pairs));
        let t = self.name_expr(span, &tname);
        let c = self.call(span, "__privacy_type", vec![t, settings, marks], Vec::new());
        let es = self.expr_stmt(c);
        self.extra.push(es);
        Ok(st)
    }

    fn table_def_db(&mut self, mut st: Stmt) -> PResult<Stmt> {
        let StmtKind::Type(node) = &mut st.kind else { return Ok(st) };
        let table = node.annotations.iter().find(|a| a.name == "table").cloned();
        let rel = node.fields.iter().find_map(|f| match &f.ty {
            TypeExpr::Name(t) if t.rel.is_some() => Some((t.rel.unwrap(), t.span)),
            _ => None,
        });
        let Some(a) = table else {
            if let Some((w, sp)) = rel {
                return Err(Diagnostic::new("table_only", sp).arg("word", w));
            }
            return Ok(st);
        };
        if !a.args.is_empty() {
            return Err(Diagnostic::new("not_supported", a.span).arg("what", "\"@table(...)\""));
        }
        if !self.db {
            return Err(Diagnostic::new("needs_use", a.span).arg("what", "\"@table\"").arg("module", "nexo.db"));
        }
        node.annotations.retain(|x| x.name != "table");
        let mut key: Option<String> = None;
        let mut many: Vec<(String, Span, String, Span)> = Vec::new();
        let mut marks: Vec<(String, Span, Vec<String>)> = Vec::new();
        let mut kept = Vec::new();
        let type_name = node.name.clone();
        let type_span = node.name_span;
        for mut f in std::mem::take(&mut node.fields) {
            if let TypeExpr::Name(t) = &mut f.ty {
                if t.rel == Some("many") {
                    many.push((f.name.clone(), f.span, t.name.clone(), t.span));
                    continue;
                }
                if t.name == "id" && t.params.is_empty() && t.rel.is_none() {
                    if key.is_some() {
                        return Err(Diagnostic::new("table_two_ids", f.span).arg("type", type_name));
                    }
                    key = Some(f.name.clone());
                    t.name = "int".into();
                    t.optional = true;
                }
            }
            let mut fmarks = f.marks.clone();
            if matches!(&f.ty, TypeExpr::Name(t) if t.rel == Some("ref")) {
                fmarks.push("ref".into());
            }
            if !fmarks.is_empty() {
                marks.push((f.name.clone(), f.span, fmarks));
            }
            kept.push(f);
        }
        node.fields = kept;
        let span = a.span;
        let tname = self.name_expr(type_span, &type_name);
        let key_e = match &key {
            Some(k) => self.text(span, &k.clone()),
            None => self.expr_node(span, ExprKind::None),
        };
        let mut many_pairs = Vec::new();
        for (n, ns, t, ts) in many {
            let k = self.text(ns, &n);
            let v = self.text(ts, &t);
            many_pairs.push((k, v));
        }
        let many_e = self.expr_node(span, ExprKind::Map(many_pairs));
        let mut mark_pairs = Vec::new();
        for (n, ns, ms) in marks {
            let k = self.text(ns, &n);
            let items = ms.iter().map(|m| self.text(ns, m)).collect();
            let v = self.expr_node(ns, ExprKind::List(items));
            mark_pairs.push((k, v));
        }
        let marks_e = self.expr_node(span, ExprKind::Map(mark_pairs));
        let c = self.call(span, "__db_table", vec![tname, key_e, many_e, marks_e], Vec::new());
        let es = self.expr_stmt(c);
        self.extra.push(es);
        Ok(st)
    }

    /// @tool "description" fn ...: the function, then __ai_tool(fn, ...).
    fn tool_def(&mut self, mut st: Stmt) -> PResult<Stmt> {
        let StmtKind::Fn(f) = &mut st.kind else { return Ok(st) };
        let Some(a) = f.annotations.iter().find(|a| a.name == "tool").cloned() else { return Ok(st) };
        f.annotations.retain(|x| x.name != "tool");
        let fname = f.name.clone();
        let fspan = f.name_span;
        let ptypes: Vec<Option<TypeExpr>> = f.params.iter().map(|p| p.ty.clone()).collect();
        let desc = match a.args.first() {
            Some(arg) => arg.value.clone(),
            None => self.text(a.span, &fname),
        };
        let types: Vec<Expr> = ptypes.iter().map(|t| self.type_value(t, a.span)).collect();
        let types = self.expr_node(a.span, ExprKind::List(types));
        let fe = self.name_expr(fspan, &fname);
        let c = self.call(a.span, "__ai_tool", vec![fe, desc, types], Vec::new());
        let es = self.expr_stmt(c);
        self.extra.push(es);
        Ok(st)
    }

    /// fake NAME returns EXPR (inside a test, section 14)
    fn fake_stmt(&mut self) -> PResult<Stmt> {
        let tok = self.advance();
        let name_start = self.peek().span;
        let name = self.dotted_name()?;
        let name_span = self.span_from(name_start);
        if !self.at_name("returns") {
            return Err(self.error("\"returns\""));
        }
        self.advance();
        let value = self.expr()?;
        let span = self.span_from(tok.span);
        let n = self.text(name_span, &name);
        let c = self.call(span, "__fake", vec![n, value], Vec::new());
        let st = self.expr_stmt(c);
        self.expect_newline()?;
        Ok(st)
    }

    /// NAME { "." NAME } "." "transaction" ":" NEWLINE
    fn at_transaction(&self) -> bool {
        let mut k = 1;
        while self.peek_at(k).is_op(".") && self.is_name_at(k + 1) {
            k += 2;
        }
        k >= 3
            && matches!(&self.peek_at(k - 1).tok, Tok::Name(n) if n == "transaction")
            && self.peek_at(k).is_op(":")
            && self.peek_at(k + 1).tok == Tok::Newline
    }

    /// db.transaction: block (section 6), as
    ///     __txL_C = __db_begin(db)
    ///     try:
    ///         block
    ///         __db_commit(__txL_C)
    ///     else __e:
    ///         __db_rollback(__txL_C)
    ///         fail(__e)
    fn transaction(&mut self) -> PResult<Stmt> {
        let start = self.peek().span;
        let (first, fspan) = self.expect_name()?;
        let mut obj = self.name_expr(fspan, &first);
        while !(matches!(&self.peek_at(1).tok, Tok::Name(n) if n == "transaction") && self.peek_at(2).is_op(":")) {
            self.advance();
            let (n, ns) = self.expect_name()?;
            let span = self.span_from(start);
            obj = self.expr_node(span, ExprKind::Attr { obj: Box::new(obj), name: n, name_span: ns });
        }
        self.advance();
        self.advance();
        let span = self.span_from(start);
        let mut body = self.block()?;
        let mut ret = None;
        crate::checker::walk_stmts(&body, false, &mut |s| {
            if matches!(s.kind, StmtKind::Return(_)) && ret.is_none() {
                ret = Some(s.span);
            }
        });
        if let Some(sp) = ret {
            return Err(Diagnostic::new("transaction_return", sp));
        }
        let tx = format!("__tx{}_{}", span.line, span.col);
        let begin_call = self.call(span, "__db_begin", vec![obj], Vec::new());
        let target = self.name_expr(span, &tx);
        let begin = self.stmt_node(span, StmtKind::Assign { target, op: "=", value: begin_call, ty: None });
        let txe = self.name_expr(span, &tx);
        let commit = self.call(span, "__db_commit", vec![txe], Vec::new());
        let commit = self.expr_stmt(commit);
        body.push(commit);
        let txe = self.name_expr(span, &tx);
        let rollback = self.call(span, "__db_rollback", vec![txe], Vec::new());
        let rollback = self.expr_stmt(rollback);
        let e = self.name_expr(span, "__e");
        let refail = self.call(span, "fail", vec![e], Vec::new());
        let refail = self.expr_stmt(refail);
        let handler = Handler { span, var: "__e".into(), kind: None, body: vec![rollback, refail] };
        let tr = self.stmt_node(span, StmtKind::Try { within: None, body, handlers: vec![handler] });
        self.extra.push(tr);
        Ok(begin)
    }
}

/// ["a", "b", "c"] for a.b.c (["a"] for a bare name), or None.
fn dotted(e: &Expr) -> Option<Vec<String>> {
    let mut out = Vec::new();
    let mut e = e;
    loop {
        match &e.kind {
            ExprKind::Attr { obj, name, .. } => {
                out.push(name.clone());
                e = obj;
            }
            ExprKind::Name(n) => {
                out.push(n.clone());
                out.reverse();
                return Some(out);
            }
            _ => return None,
        }
    }
}

fn y_span(e: &Expr) -> Span {
    match &e.kind {
        ExprKind::BinOp { left, .. } => left.span,
        _ => e.span,
    }
}

fn m_span(e: &Expr) -> Span {
    match &e.kind {
        ExprKind::BinOp { right, .. } => right.span,
        _ => e.span,
    }
}

/// Every expression a statement holds directly (not those of its blocks).
pub fn each_stmt_expr<'a>(s: &'a Stmt, f: &mut dyn FnMut(&'a Expr)) {
    match &s.kind {
        StmtKind::Expr(e) | StmtKind::Expect(e) | StmtKind::Ensure(e) | StmtKind::Return(Some(e)) => f(e),
        StmtKind::Assign { target, value, .. } => {
            f(target);
            f(value);
        }
        StmtKind::VarDecl { value, .. } | StmtKind::Shared { value, .. } => f(value),
        StmtKind::Assert { expr, .. } => f(expr),
        StmtKind::If { branches, .. } => branches.iter().for_each(|(c, _)| f(c)),
        StmtKind::For { iterable: e, .. } | StmtKind::While { cond: e, .. } | StmtKind::Repeat { count: e, .. } => f(e),
        StmtKind::Match { subject, arms } => {
            f(subject);
            for a in arms {
                if let Some(g) = &a.guard {
                    f(g);
                }
            }
        }
        StmtKind::Spawn { call } => f(call),
        StmtKind::Try { within: Some(w), .. } => f(w),
        StmtKind::Emit { args, .. } => args.iter().for_each(|a| f(&a.value)),
        _ => {}
    }
}

/// Every expression directly inside another one, with the expressions of
/// the statements of a `parallel for` body.
pub fn each_child<'a>(e: &'a Expr, f: &mut dyn FnMut(&'a Expr)) {
    match &e.kind {
        ExprKind::Call { func, args } => {
            f(func);
            args.iter().for_each(|a| f(&a.value));
        }
        ExprKind::Propagate(x) => f(x),
        ExprKind::Coalesce { left, right } => {
            f(left);
            f(right);
        }
        ExprKind::Lambda { body, .. } => f(body),
        ExprKind::Pipeline { source, steps } => {
            f(source);
            for st in steps {
                if let Some(x) = &st.expr {
                    f(x);
                }
                st.columns.iter().for_each(|c| f(c));
            }
        }
        ExprKind::ParallelFor { iterable, args, body, .. } => {
            f(iterable);
            args.iter().for_each(|a| f(&a.value));
            crate::checker::walk_stmts(body, true, &mut |s| each_stmt_expr(s, f));
        }
        _ => {
            for c in crate::checker::children(e) {
                f(c);
            }
        }
    }
}

/// The names a statement reads or writes.
fn stmt_names(s: &Stmt, out: &mut std::collections::HashSet<String>) {
    each_stmt_expr(s, &mut |e| expr_names(e, out));
}

fn expr_names(e: &Expr, out: &mut std::collections::HashSet<String>) {
    if let ExprKind::Name(n) = &e.kind {
        out.insert(n.clone());
    }
    each_child(e, &mut |c| expr_names(c, out));
}

/// Calls of `ask` (section 8, D107): in `model: fast` a bare name declared
/// by an `ai models:` block is the name of that model; in `creativity: low`,
/// `guard: injection` and `redact: private` a bare name is a word; and
/// `o: Opinion = ask(...)` passes the type of the variable as `as:`.
fn model_aliases(program: &mut Program, ids: &mut u32) {
    let mut aliases: Vec<String> = Vec::new();
    for s in &program.body {
        if let StmtKind::Expr(Expr { kind: ExprKind::Call { func, args }, .. }) = &s.kind {
            if matches!(&func.kind, ExprKind::Name(n) if n == "__ai_models") {
                aliases.extend(args.iter().filter_map(|a| a.name.clone()));
            }
        }
    }
    for s in &mut program.body {
        fix_stmt(s, &aliases, ids);
    }
}

/// The ask call a typed variable receives: it gets `as: T`.
fn typed_ask(value: &mut Expr, ty: &Option<TypeExpr>, ids: &mut u32) {
    let Some(TypeExpr::Name(tn)) = ty else { return };
    // `o: Opinion = ask(...)?` too
    let value = match &mut value.kind {
        ExprKind::Propagate(inner) => inner.as_mut(),
        _ => value,
    };
    if let ExprKind::Call { func, args } = &mut value.kind {
        if matches!(&func.kind, ExprKind::Name(n) if n == "ask") && !args.iter().any(|a| a.name.as_deref() == Some("as")) {
            *ids += 1;
            let t = Expr { id: *ids, span: tn.span, kind: ExprKind::Name(tn.name.clone()) };
            args.push(Arg { span: tn.span, name: Some("as".into()), value: t });
        }
    }
}

fn fix_body(body: &mut [Stmt], aliases: &[String], ids: &mut u32) {
    for s in body {
        fix_stmt(s, aliases, ids);
    }
}

fn fix_stmt(s: &mut Stmt, aliases: &[String], ids: &mut u32) {
    match &mut s.kind {
        StmtKind::Expr(e) | StmtKind::Return(Some(e)) | StmtKind::Expect(e) | StmtKind::Ensure(e) => fix_expr(e, aliases, ids),
        StmtKind::Assert { expr, .. } => fix_expr(expr, aliases, ids),
        StmtKind::Assign { target, value, ty, .. } => {
            typed_ask(value, ty, ids);
            fix_expr(target, aliases, ids);
            fix_expr(value, aliases, ids);
        }
        StmtKind::VarDecl { value, ty, .. } => {
            typed_ask(value, ty, ids);
            fix_expr(value, aliases, ids);
        }
        StmtKind::Shared { value, .. } => fix_expr(value, aliases, ids),
        StmtKind::Spawn { call } => fix_expr(call, aliases, ids),
        StmtKind::Emit { args, .. } => args.iter_mut().for_each(|a| fix_expr(&mut a.value, aliases, ids)),
        StmtKind::Fn(f) => fix_body(&mut f.body, aliases, ids),
        StmtKind::Type(t) => t.methods.iter_mut().for_each(|m| fix_body(&mut m.body, aliases, ids)),
        StmtKind::If { branches, orelse } => {
            for (c, b) in branches {
                fix_expr(c, aliases, ids);
                fix_body(b, aliases, ids);
            }
            if let Some(b) = orelse {
                fix_body(b, aliases, ids);
            }
        }
        StmtKind::For { iterable: e, body, .. } | StmtKind::While { cond: e, body } | StmtKind::Repeat { count: e, body } => {
            fix_expr(e, aliases, ids);
            fix_body(body, aliases, ids);
        }
        StmtKind::Match { subject, arms } => {
            fix_expr(subject, aliases, ids);
            for a in arms {
                if let Some(g) = &mut a.guard {
                    fix_expr(g, aliases, ids);
                }
                fix_body(&mut a.body, aliases, ids);
            }
        }
        StmtKind::Try { within, body, handlers } => {
            if let Some(w) = within {
                fix_expr(w, aliases, ids);
            }
            fix_body(body, aliases, ids);
            handlers.iter_mut().for_each(|h| fix_body(&mut h.body, aliases, ids));
        }
        StmtKind::Parallel(b) | StmtKind::Test { body: b, .. } | StmtKind::Event { body: b, .. } => fix_body(b, aliases, ids),
        StmtKind::Select { arms } => arms.iter_mut().for_each(|a| fix_body(&mut a.body, aliases, ids)),
        _ => {}
    }
}

fn fix_expr(e: &mut Expr, aliases: &[String], ids: &mut u32) {
    if let ExprKind::Call { func, args } = &mut e.kind {
        if matches!(&func.kind, ExprKind::Name(n) if n == "ask") {
            for a in args.iter_mut() {
                let word = matches!(a.name.as_deref(), Some("creativity" | "guard" | "redact"));
                if word || a.name.as_deref() == Some("model") {
                    if let ExprKind::Name(n) = &a.value.kind {
                        if word || aliases.contains(n) {
                            a.value.kind = ExprKind::Str(vec![StrPiece::Lit(n.clone())]);
                        }
                    }
                }
            }
        }
    }
    match &mut e.kind {
        ExprKind::Str(parts) => {
            for p in parts {
                if let StrPiece::Expr(x) = p {
                    fix_expr(x, aliases, ids);
                }
            }
        }
        ExprKind::List(items) => items.iter_mut().for_each(|x| fix_expr(x, aliases, ids)),
        ExprKind::Map(pairs) => pairs.iter_mut().for_each(|(k, v)| {
            fix_expr(k, aliases, ids);
            fix_expr(v, aliases, ids);
        }),
        ExprKind::Comprehension { element, iterable, condition, .. } => {
            fix_expr(element, aliases, ids);
            fix_expr(iterable, aliases, ids);
            if let Some(c) = condition {
                fix_expr(c, aliases, ids);
            }
        }
        ExprKind::Attr { obj, .. } | ExprKind::Propagate(obj) => fix_expr(obj, aliases, ids),
        ExprKind::Unary { operand, .. } => fix_expr(operand, aliases, ids),
        ExprKind::Index { obj, index } => {
            fix_expr(obj, aliases, ids);
            fix_expr(index, aliases, ids);
        }
        ExprKind::Call { func, args } => {
            fix_expr(func, aliases, ids);
            args.iter_mut().for_each(|a| fix_expr(&mut a.value, aliases, ids));
        }
        ExprKind::BinOp { left, right, .. }
        | ExprKind::BoolOp { left, right, .. }
        | ExprKind::Coalesce { left, right }
        | ExprKind::Range { start: left, end: right } => {
            fix_expr(left, aliases, ids);
            fix_expr(right, aliases, ids);
        }
        ExprKind::Compare { operands, .. } => operands.iter_mut().for_each(|x| fix_expr(x, aliases, ids)),
        ExprKind::Lambda { body, .. } => fix_expr(body, aliases, ids),
        ExprKind::Pipeline { source, steps } => {
            fix_expr(source, aliases, ids);
            for st in steps {
                if let Some(x) = &mut st.expr {
                    fix_expr(x, aliases, ids);
                }
                st.columns.iter_mut().for_each(|c| fix_expr(c, aliases, ids));
            }
        }
        ExprKind::ParallelFor { iterable, args, body, .. } => {
            fix_expr(iterable, aliases, ids);
            args.iter_mut().for_each(|a| fix_expr(&mut a.value, aliases, ids));
            fix_body(body, aliases, ids);
        }
        _ => {}
    }
}

fn check_unique<'a>(names: impl Iterator<Item = (&'a str, Span)>) -> PResult<()> {
    let mut seen = std::collections::HashSet::new();
    for (name, span) in names {
        if !seen.insert(name) {
            return Err(Diagnostic::new("duplicate_name", span).arg("name", name));
        }
    }
    Ok(())
}

/// target = IDENT { "." IDENT | "[" expr "]" }
fn valid_target(node: &Expr) -> bool {
    let mut node = node;
    if !matches!(node.kind, ExprKind::Name(_) | ExprKind::Attr { .. } | ExprKind::Index { .. }) {
        return false;
    }
    loop {
        match &node.kind {
            ExprKind::Attr { obj, .. } | ExprKind::Index { obj, .. } => node = obj,
            ExprKind::Name(_) => return true,
            _ => return false,
        }
    }
}

pub fn parse(source: &str) -> PResult<Program> {
    let tokens = tokenize(source)?;
    Parser::new(tokens, source, false, 0).program()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn err(src: &str) -> Diagnostic {
        parse(src).unwrap_err()
    }

    #[test]
    fn parses_the_section_2_examples() {
        let src = "type User:\n    name: str\n    age: int\n    email: str?\n\n    fn is_adult(self) -> bool:\n        return self.age >= 18\n\ntype Status = \"active\" | \"closed\"\n\ntype Event:\n    Click(x: int, y: int)\n    Key(code: str)\n    Quit\n\nmatch event:\n    Click(x, y) if x > 100: print(\"right\")\n    Key(\"q\") or Quit:       close()\n    _:                      pass\n";
        let p = parse(src).unwrap();
        assert_eq!(p.body.len(), 4);
        let StmtKind::Match { arms, .. } = &p.body[3].kind else { panic!() };
        assert_eq!(arms.len(), 3);
        assert_eq!(arms[1].patterns.len(), 2);
    }

    #[test]
    fn precedence() {
        let p = parse("x = 1 + 2 * 3 ** 2\n").unwrap();
        let StmtKind::Assign { value, .. } = &p.body[0].kind else { panic!() };
        let ExprKind::BinOp { op, right, .. } = &value.kind else { panic!() };
        assert_eq!(*op, "+");
        assert!(matches!(right.kind, ExprKind::BinOp { op: "*", .. }));
    }

    #[test]
    fn pipelines() {
        let p = parse("positives = numbers |> where it > 0 |> map it * 2\n").unwrap();
        let StmtKind::Assign { value, .. } = &p.body[0].kind else { panic!() };
        let ExprKind::Pipeline { steps, .. } = &value.kind else { panic!() };
        assert_eq!(steps.len(), 2);
        assert_eq!(err("total = sales |> sum amount\n").key, "pipe_needs_group");
        assert_eq!(err("x = s |> group by r |> take 1\n").key, "pipe_group_next");
    }

    #[test]
    fn errors() {
        assert_eq!(err("try:\n    x = 1\nelse:\n    x = 2\n").key, "else_needs_var");
        assert_eq!(err("1 = 2\n").key, "invalid_target");
        assert_eq!(err("fn f(a, a):\n    pass\n").key, "duplicate_name");
        assert_eq!(err("for if in x:\n    pass\n").key, "reserved_word");
        let e = err("print(\"{1 + }\")\n");
        assert_eq!(e.key, "unexpected");
        assert_eq!(e.arg_value("got"), Some("\"}\""));
        assert_eq!(err("x = [1, 2,]\n").key, "unexpected");
    }

    #[test]
    fn parallel_for_ends_the_statement() {
        let p = parse("r = parallel for n in xs, max: 2:\n    return n\nprint(r)\n").unwrap();
        assert_eq!(p.body.len(), 2);
    }

    #[test]
    fn config_blocks_are_skipped() {
        let p = parse("policy:\n    deny shell\n    nested:\n        a = 1\nprint(1)\n").unwrap();
        assert!(matches!(p.body[0].kind, StmtKind::Config { .. }));
        assert_eq!(p.body.len(), 2);
    }

    #[test]
    fn phase_5_blocks_desugar_into_entries() {
        let src = "infra shop:\n    cloud = \"aws\"\n    require tags [\"a\"]\nflags:\n    a = on, expires: 2027-03-01\npipeline main:\n    step build:\n        image = \"x\"\n    step dev:\n        print(image)\n";
        let p = parse(src).unwrap();
        assert_eq!(summary(&p), vec!["__infra()", "__flags()", "fn main step build", "fn main step dev", "__pipeline()"]);
        let StmtKind::Fn(dev) = &p.body[3].kind else { panic!() };
        assert!(matches!(&dev.body[0].kind, StmtKind::Assign { value: Expr { kind: ExprKind::Call { .. }, .. }, .. }));
    }

    fn summary(p: &Program) -> Vec<String> {
        p.body
            .iter()
            .map(|s| match &s.kind {
                StmtKind::Use { what, .. } => format!("use {what}"),
                StmtKind::Assign { target: Expr { kind: ExprKind::Name(n), .. }, .. } => format!("{n} ="),
                StmtKind::Type(t) => format!("type {} {}", t.name, t.fields.len()),
                StmtKind::Fn(f) => format!("fn {}", f.name),
                StmtKind::Expr(Expr { kind: ExprKind::Call { func, .. }, .. }) => match &func.kind {
                    ExprKind::Name(n) => format!("{n}()"),
                    _ => "call".into(),
                },
                StmtKind::Try { body, .. } => format!("try {}", body.len()),
                StmtKind::Test { .. } => "test".into(),
                _ => "?".into(),
            })
            .collect()
    }

    #[test]
    fn module_blocks_desugar() {
        let src = "use nexo.db\nuse python numpy as np\nuse c \"libm\"\n@table\ntype Customer:\n    id: id\n    email: str @unique\n    orders: many Order\nai models:\n    fast = \"openai:gpt-mini\"\nai:\n    model = fast\n@tool \"Looks up\"\nfn find(id: str) -> str:\n    return id\nagent support:\n    tools = [find]\nserver api:\n    port = 8080\n    @limit(10 per minute per ip)\n    get \"/users/{id}\"(id: int):\n        return id\ndb.transaction:\n    print(1)\nx = ask(\"hi\", model: fast)\ntest \"t\":\n    fake ask returns \"x\"\n";
        let p = parse(src).unwrap();
        assert_eq!(
            summary(&p),
            [
                "use nexo.db", "use python numpy", "np =", "use c libm", "libm =", "type Customer 2", "__db_table()",
                "__ai_models()", "__ai_config()", "fn find", "__ai_tool()", "support =", "api =", "fn GET /users/{id}",
                "__web_route()", "__tx23_1 =", "try 2", "x =", "test"
            ]
        );
        let StmtKind::Assign { value, .. } = &p.body[17].kind else { panic!() };
        let ExprKind::Call { args, .. } = &value.kind else { panic!() };
        assert!(matches!(&args[1].value.kind, ExprKind::Str(_)));
        assert_eq!(err("@table\ntype A:\n    id: id\n").key, "needs_use");
        assert_eq!(err("type A:\n    b: ref B\n").key, "table_only");
        assert_eq!(err("use nexo.db\nfn f():\n    db.transaction:\n        return 1\n").key, "transaction_return");
    }
}
