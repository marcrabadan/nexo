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
        Parser { toks, p: 0, source, in_text, block_end: usize::MAX, next_id }
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
            body.push(self.statement()?);
        }
        Ok(Program { body })
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
            body.push(self.statement()?);
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
                return self.type_def(annotations);
            }
            let f = self.fn_def(annotations)?;
            return Ok(self.stmt_node(f.span, StmtKind::Fn(f)));
        }
        if let Tok::Keyword(kw) = tok.tok {
            match kw {
                "fn" => {
                    let f = self.fn_def(Vec::new())?;
                    return Ok(self.stmt_node(f.span, StmtKind::Fn(f)));
                }
                "pub" => {
                    if self.peek_at(1).is_kw("type") {
                        return self.type_def(Vec::new());
                    }
                    let f = self.fn_def(Vec::new())?;
                    return Ok(self.stmt_node(f.span, StmtKind::Fn(f)));
                }
                "type" => return self.type_def(Vec::new()),
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
                "test" => {
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
                    let body = self.block()?;
                    return Ok(self.stmt_node(tok.span, StmtKind::Test { name, body }));
                }
                "on" | "when" | "every" | "lifecycle" => return self.event_stmt(),
                _ => {}
            }
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
        if tok.is_kw("expect") {
            self.advance();
            let e = self.expr()?;
            let span = self.span_from(tok.span);
            return Ok(self.stmt_node(span, StmtKind::Expect(e)));
        }
        if tok.is_kw("ensure") {
            self.advance();
            let e = self.expr()?;
            let span = self.span_from(tok.span);
            return Ok(self.stmt_node(span, StmtKind::Ensure(e)));
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

    /// use_stmt = "use" [ IDENT ] dotted_name [ "as" IDENT ]
    fn use_stmt(&mut self) -> PResult<Stmt> {
        let tok = self.advance();
        let (first, _) = self.expect_name()?;
        let mut lang = None;
        let mut name = first.clone();
        if self.is_name() {
            lang = Some(first);
            name = self.dotted_name()?;
        } else {
            while self.at_op(".") {
                self.advance();
                name.push('.');
                name.push_str(&self.expect_name()?.0);
            }
        }
        if self.at_kw("as") {
            self.advance();
            self.expect_name()?;
        }
        let what = match lang {
            Some(l) => format!("{l} {name}"),
            None => name,
        };
        let span = self.span_from(tok.span);
        Ok(self.stmt_node(span, StmtKind::Use { what }))
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
            if self.at_op("(") {
                self.advance();
                self.args(")")?;
                self.expect_op(")")?;
            }
            result.push(Annotation { span: self.span_from(tok.span), name });
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
                    let ty = self.type_()?;
                    let mut default = None;
                    if self.at_op("=") {
                        self.advance();
                        default = Some(self.expr()?);
                    }
                    // annotation_inline, e.g. @private
                    while self.at_op("@") {
                        self.advance();
                        self.dotted_name()?;
                        if self.at_op("(") {
                            self.advance();
                            self.args(")")?;
                            self.expect_op(")")?;
                        }
                    }
                    self.expect_newline()?;
                    node.fields.push(FieldDef { span: tok.span, name: n, ty, default });
                } else if self.peek_at(1).is_op("=") {
                    self.advance();
                    self.advance();
                    let e = self.expr()?;
                    node.settings.push((n, e));
                    self.expect_newline()?;
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
        let tok = self.peek().clone();
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
        Ok(TypeName { span: self.span_from(tok.span), name, params, optional: false, fallible: false, weak: false })
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
            let ExprKind::Call { func, args } = trigger.kind else {
                return Err(self.error_at(&word("call"), &tok));
            };
            if let ExprKind::Name(n) = func.kind {
                name = n;
            }
            params = args;
            if self.at_kw("as") {
                self.advance();
                self.expect_name()?;
            }
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
                let args = self.args(")")?;
                self.expect_op(")")?;
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
        let p = parse("ai:\n    model = \"x\"\n    nested:\n        a = 1\nprint(1)\n").unwrap();
        assert!(matches!(p.body[0].kind, StmtKind::Config { .. }));
        assert_eq!(p.body.len(), 2);
    }
}
