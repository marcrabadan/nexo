//! Copy-on-write (section 7, rule 2) and the rule that keeps tasks from
//! racing on records.
//!
//! Lists, maps and sets are values: `b = a` shares the same object, and
//! the object is copied when one of its owners changes it while another
//! owner still holds it. The compiled code changes a collection through
//! the place that holds it (a variable, a field, an element): it asks for
//! a pointer to that place, makes the collection there unique, and then
//! changes it.
//!
//! Records are shared by reference. A task of `parallel` (or of `spawn`)
//! may only change records created after it started; the others can be
//! read by several tasks at once.

use nexo_diag::Diagnostic;

use crate::state::{diag, stamp, R};
use crate::value::*;

/// A copy of a collection that shares its elements (each one gets a new
/// owner, so changing an element copies it in turn).
pub fn shallow_copy(v: V) -> Option<Val> {
    Some(match v.get()? {
        Obj::List(items) => Val::new(Obj::List(Mut::new(items.borrow().clone()))),
        Obj::Map(m) => Val::new(Obj::Map(Mut::new(m.borrow().clone()))),
        Obj::Set(s) => Val::new(Obj::Set(Mut::new(s.borrow().clone()))),
        _ => return None,
    })
}

/// Make the collection stored at `slot` unique, copying it if another
/// owner holds it. Returns the (borrowed) value now in the slot.
///
/// # Safety
/// `slot` points to a live slot that owns its value.
pub unsafe fn make_unique(slot: *mut V) -> V {
    let v = *slot;
    if v.tag == T_OBJ && owners(v) > 1 {
        if let Some(copy) = shallow_copy(v) {
            *slot = copy.into_raw();
            release(v);
        }
    }
    *slot
}

/// Same for a value inside a container (`Val` slot).
pub fn make_unique_val(slot: &mut Val) -> V {
    let v = slot.v();
    if v.tag == T_OBJ && owners(v) > 1 {
        if let Some(copy) = shallow_copy(v) {
            *slot = copy;
        }
    }
    slot.v()
}

/// Changing a record: only the task that created it (or a later one in
/// the same line of tasks) may do it.
pub fn check_write(r: &Record, span: u32) -> R<()> {
    if r.birth.get() < stamp() {
        return Err(diag(Diagnostic::new_spanless("record_shared").arg("type", r.ty.name.clone()), span));
    }
    Ok(())
}

/// A copy in which every record is new (values that travel through a
/// channel, are stored in a `shared` variable or passed to `spawn`).
pub fn copy_records(v: V) -> Val {
    match v.get() {
        Some(Obj::Record(r)) => {
            let fields: Vec<Val> = r
                .fields
                .borrow()
                .iter()
                .map(|f| if f.tag == T_WEAK { Val::none() } else { copy_records(f.v()) })
                .collect();
            Val::new(Obj::Record(Record { ty: r.ty, fields: Mut::new(fields.into()), birth: std::cell::Cell::new(stamp()) }))
        }
        Some(Obj::List(items)) => {
            if !items.borrow().iter().any(|x| has_records(x.v())) {
                return Val::from(v);
            }
            list(items.borrow().iter().map(|x| copy_records(x.v())).collect())
        }
        Some(Obj::Map(m)) => {
            let m = m.borrow();
            if !m.entries.iter().any(|(_, x)| has_records(x.v())) {
                return Val::from(v);
            }
            let mut out = m.clone();
            for e in out.entries.iter_mut() {
                e.1 = copy_records(e.1.v());
            }
            Val::new(Obj::Map(Mut::new(out)))
        }
        Some(Obj::VariantValue(vv)) => {
            if !vv.fields.iter().any(|x| has_records(x.v())) {
                return Val::from(v);
            }
            Val::new(Obj::VariantValue(VariantValue {
                variant: vv.variant,
                fields: vv.fields.iter().map(|x| copy_records(x.v())).collect(),
            }))
        }
        Some(Obj::Error(e)) => {
            if !has_records(e.cause.v()) {
                return Val::from(v);
            }
            Val::new(Obj::Error(ErrorValue {
                kind: e.kind.clone(),
                message: e.message.clone(),
                cause: copy_records(e.cause.v()),
                line: e.line,
                span: e.span,
                fix: e.fix,
            }))
        }
        _ => Val::from(v),
    }
}

fn has_records(v: V) -> bool {
    match v.get() {
        Some(Obj::Record(_)) => true,
        Some(Obj::List(items)) => items.borrow().iter().any(|x| has_records(x.v())),
        Some(Obj::Map(m)) => m.borrow().entries.iter().any(|(_, x)| has_records(x.v())),
        Some(Obj::VariantValue(vv)) => vv.fields.iter().any(|x| has_records(x.v())),
        Some(Obj::Error(e)) => has_records(e.cause.v()),
        _ => false,
    }
}
