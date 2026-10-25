//! Runtime library of Nexo: every compiled program links it. It holds the
//! values, the arithmetic of section 17, collections, pipelines, the
//! standard library and the error machinery of section 3.

pub mod abi;
pub mod ai;
pub mod call;
pub mod cfg;
pub mod ci;
pub mod ci_export;
pub mod clib;
pub mod coll;
pub mod cow;
pub mod crypto;
pub mod data;
pub mod db;
pub mod dec;
pub mod flags;
pub mod fmt;
pub mod http;
pub mod infra;
pub mod infra_cloud;
pub mod json;
pub mod library;
pub mod num;
pub mod par;
pub mod pipe;
pub mod privacy;
pub mod pylib;
pub mod repl;
pub mod state;
pub mod stdlib;
pub mod stream;
pub mod sync;
pub mod text;
pub mod time;
pub mod types;
pub mod value;
pub mod web;
