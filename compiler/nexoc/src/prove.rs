//! Contracts the compiler proves ("Goals and guarantees"): a proven
//! contract costs nothing at run time, so its check (and the `old()`
//! values only it uses) are not compiled.
//!
//! The prover handles the common case of the examples: a function whose
//! body is straight-line integer arithmetic on its parameters and on the
//! int fields of record parameters (`source.balance -= amount`). It runs
//! the body symbolically, keeping every value as a linear combination of
//! the values on entry, and proves an `ensure` when the comparison holds
//! for every value of them. Two record parameters of the same type may be
//! the same record, so the body is run once for each way of grouping them
//! (D83). Anything else (calls, loops, conditions, floats) is left to the
//! check at run time.

use std::collections::BTreeMap;
use crate::fast::HashMap;

use crate::ast::*;

/// `sum(coef * symbol) + konst`, with i128 so that no bound is reached.
#[derive(Clone, Debug, PartialEq)]
struct Lin {
    terms: BTreeMap<String, i128>,
    konst: i128,
}

const LIMIT: i128 = 1 << 100;

impl Lin {
    fn konst(k: i128) -> Lin {
        Lin { terms: BTreeMap::new(), konst: k }
    }

    fn sym(s: String) -> Lin {
        let mut terms = BTreeMap::new();
        terms.insert(s, 1);
        Lin { terms, konst: 0 }
    }

    fn add(&self, o: &Lin, sign: i128) -> Option<Lin> {
        let mut out = self.clone();
        for (s, c) in &o.terms {
            let e = out.terms.entry(s.clone()).or_insert(0);
            *e += sign * c;
            if *e == 0 {
                out.terms.remove(s);
            }
        }
        out.konst += sign * o.konst;
        out.small()
    }

    fn scale(&self, k: i128) -> Option<Lin> {
        let mut out = Lin::konst(self.konst * k);
        for (s, c) in &self.terms {
            if c * k != 0 {
                out.terms.insert(s.clone(), c * k);
            }
        }
        out.small()
    }

    fn as_konst(&self) -> Option<i128> {
        self.terms.is_empty().then_some(self.konst)
    }

    fn small(self) -> Option<Lin> {
        let ok = self.konst.abs() < LIMIT && self.terms.values().all(|c| c.abs() < LIMIT);
        ok.then_some(self)
    }
}

#[derive(Clone, PartialEq)]
enum Kind {
    Int,
    /// a record parameter and the int fields of its type
    Record(String),
    Other,
}

/// What the prover knows about the program: the int fields of each record.
pub struct Types {
    int_fields: HashMap<String, Vec<String>>,
}

impl Types {
    pub fn new(program: &Program) -> Types {
        let mut int_fields = HashMap::default();
        // a name two files give to different types proves nothing (D124)
        let mut seen = crate::fast::HashSet::default();
        let mut twice = Vec::new();
        crate::checker::walk_stmts(&program.body, true, &mut |s| {
            if let StmtKind::Type(t) = &s.kind {
                if !seen.insert(t.name.clone()) {
                    twice.push(t.name.clone());
                }
            }
            if let StmtKind::Type(t) = &s.kind {
                if t.alias.is_none() && t.variants.is_empty() {
                    let ints = t
                        .fields
                        .iter()
                        .filter(|f| matches!(&f.ty, TypeExpr::Name(tn) if is_int(tn) && !tn.weak))
                        .map(|f| f.name.clone())
                        .collect();
                    int_fields.insert(t.name.clone(), ints);
                }
            }
        });
        for name in twice {
            int_fields.remove(&name);
        }
        Types { int_fields }
    }
}

fn is_int(t: &TypeName) -> bool {
    t.name == "int" && t.params.is_empty() && !t.optional && !t.fallible
}

/// For each `ensure` of `f` (in order): proven? `self_type` is the type a
/// method belongs to.
pub fn ensures(f: &FnDef, self_type: Option<&str>, types: &Types) -> Vec<bool> {
    let ens: Vec<&Expr> = f
        .body
        .iter()
        .filter_map(|s| match &s.kind {
            StmtKind::Ensure(e) => Some(e),
            _ => None,
        })
        .collect();
    if ens.is_empty() {
        return Vec::new();
    }
    let mut params: Vec<(String, Kind)> = Vec::new();
    for p in &f.params {
        let kind = match (&p.ty, p.name.as_str(), self_type) {
            (Some(TypeExpr::Name(tn)), _, _) if is_int(tn) && p.default.is_none() => Kind::Int,
            (Some(TypeExpr::Name(tn)), _, _)
                if !tn.optional && tn.params.is_empty() && types.int_fields.contains_key(&tn.name) =>
            {
                Kind::Record(tn.name.clone())
            }
            (None, "self", Some(t)) if types.int_fields.contains_key(t) => Kind::Record(t.to_string()),
            _ => Kind::Other,
        };
        params.push((p.name.clone(), kind));
    }
    let roots: Vec<(String, String)> = params
        .iter()
        .filter_map(|(n, k)| match k {
            Kind::Record(t) => Some((n.clone(), t.clone())),
            _ => None,
        })
        .collect();
    if roots.len() > 4 {
        return vec![false; ens.len()];
    }
    let groupings = groupings(&roots);
    let mut proven = vec![true; ens.len()];
    for g in &groupings {
        let mut run = Run { params: &params, types, rep: g, locals: HashMap::default(), fields: HashMap::default() };
        let ok = run.body(&f.body);
        for (i, e) in ens.iter().enumerate() {
            if !ok || !run.holds(e) {
                proven[i] = false;
            }
        }
    }
    proven
}

/// Every way of telling which record parameters are the same record: a
/// representative for each one (parameters of different types never are).
fn groupings(roots: &[(String, String)]) -> Vec<HashMap<String, String>> {
    let mut out: Vec<HashMap<String, String>> = vec![HashMap::default()];
    for (i, (name, ty)) in roots.iter().enumerate() {
        let mut next = Vec::new();
        for g in &out {
            // a new record...
            let mut alone = g.clone();
            alone.insert(name.clone(), name.clone());
            next.push(alone);
            // ...or the same as an earlier one of its type
            let mut reps: Vec<&String> = roots[..i]
                .iter()
                .filter(|(n, t)| t == ty && g.get(n) == Some(n))
                .map(|(n, _)| n)
                .collect();
            reps.dedup();
            for r in reps {
                let mut same = g.clone();
                same.insert(name.clone(), r.clone());
                next.push(same);
            }
        }
        out = next;
    }
    out
}

struct Run<'a> {
    params: &'a [(String, Kind)],
    types: &'a Types,
    rep: &'a HashMap<String, String>,
    locals: HashMap<String, Lin>,
    fields: HashMap<(String, String), Lin>,
}

impl Run<'_> {
    fn kind(&self, name: &str) -> Option<&Kind> {
        self.params.iter().find(|(n, _)| n == name).map(|(_, k)| k)
    }

    /// The field a place names: (representative record, field), when it is
    /// an int field of a record parameter.
    fn field(&self, obj: &Expr, field: &str) -> Option<(String, String)> {
        let ExprKind::Name(root) = &obj.kind else { return None };
        if self.locals.contains_key(root) {
            return None;
        }
        let Some(Kind::Record(t)) = self.kind(root) else { return None };
        if !self.types.int_fields.get(t)?.iter().any(|f| f == field) {
            return None;
        }
        Some((self.rep.get(root)?.clone(), field.to_string()))
    }

    /// Run the body; false when it does something the prover cannot follow.
    fn body(&mut self, body: &[Stmt]) -> bool {
        for (i, s) in body.iter().enumerate() {
            let ok = match &s.kind {
                // a contract that may change something (a call) would make
                // the symbolic state wrong for the contracts proven after it
                StmtKind::Expect(e) | StmtKind::Ensure(e) => effect_free(e),
                StmtKind::Pass => true,
                StmtKind::Return(v) => i == body.len() - 1 && v.as_ref().is_none_or(|e| self.value(e, false).is_some()),
                StmtKind::VarDecl { name, value, ty: None, .. } => match self.value(value, false) {
                    Some(v) if self.kind(name).is_none() => {
                        self.locals.insert(name.clone(), v);
                        true
                    }
                    _ => false,
                },
                StmtKind::Assign { target, op, value, ty: None } => self.assign(target, op, value),
                _ => false,
            };
            if !ok {
                return false;
            }
        }
        true
    }

    fn assign(&mut self, target: &Expr, op: &str, value: &Expr) -> bool {
        let Some(v) = self.value(value, false) else { return false };
        let combine = |cur: Option<Lin>| -> Option<Lin> {
            match op {
                "=" => Some(v.clone()),
                "+=" => cur?.add(&v, 1),
                "-=" => cur?.add(&v, -1),
                "*=" => {
                    let k = v.as_konst()?;
                    cur?.scale(k)
                }
                _ => None,
            }
        };
        match &target.kind {
            ExprKind::Name(n) if self.locals.contains_key(n) || (op == "=" && self.kind(n).is_none()) => {
                let cur = self.locals.get(n).cloned();
                match combine(cur) {
                    Some(x) => {
                        self.locals.insert(n.clone(), x);
                        true
                    }
                    None => false,
                }
            }
            ExprKind::Attr { obj, name, .. } => {
                let Some(key) = self.field(obj, name) else { return false };
                let cur = Some(self.read_field(&key, false));
                match combine(cur) {
                    Some(x) => {
                        self.fields.insert(key, x);
                        true
                    }
                    None => false,
                }
            }
            _ => false,
        }
    }

    fn read_field(&self, key: &(String, String), on_entry: bool) -> Lin {
        if !on_entry {
            if let Some(v) = self.fields.get(key) {
                return v.clone();
            }
        }
        Lin::sym(format!("{}.{}", key.0, key.1))
    }

    /// The value of an int expression; `on_entry`: as it was when the
    /// function was entered (inside `old()`).
    fn value(&self, e: &Expr, on_entry: bool) -> Option<Lin> {
        match &e.kind {
            ExprKind::Num { text, is_float: false } => Some(Lin::konst(text.parse::<i64>().ok()? as i128)),
            ExprKind::Name(n) => {
                if !on_entry {
                    if let Some(v) = self.locals.get(n) {
                        return Some(v.clone());
                    }
                } else if self.locals.contains_key(n) {
                    return None;
                }
                matches!(self.kind(n), Some(Kind::Int)).then(|| Lin::sym(n.clone()))
            }
            ExprKind::Attr { obj, name, .. } => {
                let key = self.field(obj, name)?;
                Some(self.read_field(&key, on_entry))
            }
            ExprKind::BinOp { op, left, right } => {
                let l = self.value(left, on_entry)?;
                let r = self.value(right, on_entry)?;
                match *op {
                    "+" => l.add(&r, 1),
                    "-" => l.add(&r, -1),
                    "*" => match (l.as_konst(), r.as_konst()) {
                        (Some(k), _) => r.scale(k),
                        (_, Some(k)) => l.scale(k),
                        _ => None,
                    },
                    _ => None,
                }
            }
            ExprKind::Unary { op: "-", operand } => self.value(operand, on_entry)?.scale(-1),
            ExprKind::Call { func, args } if matches!(&func.kind, ExprKind::Name(n) if n == "old") && args.len() == 1 => {
                self.value(&args[0].value, true)
            }
            _ => None,
        }
    }

    /// Is the condition true for every value on entry?
    fn holds(&self, e: &Expr) -> bool {
        match &e.kind {
            ExprKind::Bool(b) => *b,
            ExprKind::BoolOp { op: "and", left, right } => self.holds(left) && self.holds(right),
            ExprKind::BoolOp { op: "or", left, right } => self.holds(left) || self.holds(right),
            ExprKind::Compare { ops, operands } => {
                let mut vals = Vec::new();
                for o in operands {
                    match self.value(o, false) {
                        Some(v) => vals.push(v),
                        None => return false,
                    }
                }
                ops.iter().zip(vals.windows(2)).all(|(op, w)| {
                    let Some(d) = w[0].add(&w[1], -1) else { return false };
                    match (*op, d.as_konst()) {
                        ("==", Some(k)) => k == 0,
                        ("!=", Some(k)) => k != 0,
                        ("<", Some(k)) => k < 0,
                        ("<=", Some(k)) => k <= 0,
                        (">", Some(k)) => k > 0,
                        (">=", Some(k)) => k >= 0,
                        _ => false,
                    }
                })
            }
            _ => false,
        }
    }
}

/// An `expect` that holds whatever the arguments are (a constant
/// condition).
pub fn expect_holds(e: &Expr) -> bool {
    let types = Types { int_fields: HashMap::default() };
    let run = Run { params: &[], types: &types, rep: &HashMap::default(), locals: HashMap::default(), fields: HashMap::default() };
    run.holds(e)
}

/// An expression that cannot change anything: no calls except `old()`,
/// no pipelines, lambdas or tasks.
fn effect_free(e: &Expr) -> bool {
    match &e.kind {
        ExprKind::Num { .. } | ExprKind::Unit { .. } | ExprKind::Bool(_) | ExprKind::None | ExprKind::Name(_) => true,
        ExprKind::Call { func, args } => {
            matches!(&func.kind, ExprKind::Name(n) if n == "old") && args.iter().all(|a| effect_free(&a.value))
        }
        ExprKind::Coalesce { left, right } => effect_free(left) && effect_free(right),
        ExprKind::Str(_)
        | ExprKind::List(_)
        | ExprKind::Map(_)
        | ExprKind::Comprehension { .. }
        | ExprKind::Attr { .. }
        | ExprKind::Index { .. }
        | ExprKind::Unary { .. }
        | ExprKind::BinOp { .. }
        | ExprKind::BoolOp { .. }
        | ExprKind::Compare { .. }
        | ExprKind::Range { .. } => crate::checker::children(e).into_iter().all(effect_free),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::parse;

    fn proven(src: &str) -> Vec<bool> {
        let p = parse(src).unwrap();
        let types = Types::new(&p);
        let mut out = Vec::new();
        for s in &p.body {
            if let StmtKind::Fn(f) = &s.kind {
                out.extend(ensures(f, None, &types));
            }
        }
        out
    }

    const ACCOUNT: &str = "type Account:\n    balance: int\n";

    #[test]
    fn transfer_keeps_the_total() {
        let src = format!(
            "{ACCOUNT}fn transfer(source: Account, target: Account, amount: int):\n    expect amount > 0\n    ensure source.balance + target.balance == old(source.balance + target.balance)\n    source.balance -= amount\n    target.balance += amount\n"
        );
        assert_eq!(proven(&src), vec![true]);
    }

    #[test]
    fn aliasing_is_considered() {
        // true when a and b are different records, false when they are the same
        let src = format!(
            "{ACCOUNT}fn bump(a: Account, b: Account):\n    ensure a.balance == old(a.balance) + 1\n    a.balance += 1\n    b.balance += 1\n"
        );
        assert_eq!(proven(&src), vec![false]);
    }

    #[test]
    fn what_cannot_be_followed_is_checked_at_run_time() {
        let src = "fn f(x: int) -> int:\n    ensure x == old(x)\n    print(x)\n    return x\n";
        assert_eq!(proven(src), vec![false]);
        let src = "fn g(x: float):\n    ensure x == old(x)\n    pass\n";
        assert_eq!(proven(src), vec![false]);
        let src = "fn h(x: int):\n    ensure x + 1 > old(x)\n    var y = x * 2\n    y -= x\n";
        assert_eq!(proven(src), vec![true]);
    }

    #[test]
    fn a_contract_that_may_change_something_stops_the_proof() {
        // mutate(b) may change b.n before the ensure is checked
        let src = "type B:\n    n: int\nfn mutate(b: B) -> bool:\n    b.n += 1\n    return true\nfn f(b: B):\n    expect mutate(b)\n    ensure b.n == old(b.n)\n    pass\n";
        assert_eq!(proven(src), vec![false]);
        // the same through another ensure
        let src = "type B:\n    n: int\nfn mutate(b: B) -> bool:\n    b.n += 1\n    return true\nfn g(b: B):\n    ensure mutate(b)\n    ensure b.n == old(b.n)\n    pass\n";
        assert_eq!(proven(src), vec![false, false]);
        // old() and arithmetic change nothing
        let src = "type B:\n    n: int\nfn h(b: B):\n    expect b.n >= 0 and old(b.n) == b.n\n    ensure b.n == old(b.n) + 1\n    b.n += 1\n";
        assert_eq!(proven(src), vec![true]);
    }
}
