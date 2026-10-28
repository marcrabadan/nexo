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
pub mod docdb;
pub mod mongo;
pub mod elastic;
pub mod pg;
pub mod mysql;
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
pub mod patterns;
pub mod pipe;
pub mod policy;
pub mod security;
pub mod privacy;
pub mod pylib;
pub mod repl;
pub mod state;
pub mod stdlib;
pub mod stream;
pub mod stream_cloud;
pub mod stream_net;
pub mod kafka;
pub mod amqp;
pub mod websocket;
pub mod sync;
pub mod text;
pub mod time;
pub mod types;
pub mod value;
pub mod web;

/// A program built for the web (D175) has no dynamic libraries: `use c`
/// and `use python` find none and fail as for a library that is missing.
#[cfg(target_family = "wasm")]
mod wasm {
    use std::ffi::{c_char, c_void};

    #[no_mangle]
    pub extern "C" fn dlopen(_file: *const c_char, _mode: i32) -> *mut c_void {
        std::ptr::null_mut()
    }

    #[no_mangle]
    pub extern "C" fn dlsym(_handle: *mut c_void, _name: *const c_char) -> *mut c_void {
        std::ptr::null_mut()
    }
}
