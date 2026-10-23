//! `nexo.sync` (section 5): channels, `spawn`, `select` and `shared`.

use crate::pipe::Items;
use crate::state::R;
use crate::value::*;

pub struct Channel {}

pub fn iterate(_c: &'static Channel, _v: V) -> Items {
    Box::new(std::iter::empty())
}

pub fn call_method(_c: &'static Channel, _obj: V, _name: &str, _args: &[V], _names: &[Option<&str>], _span: u32) -> R<Val> {
    Ok(Val::none())
}
