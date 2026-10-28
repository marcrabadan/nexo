#!/bin/sh
# The runtime of `nexo build --target web` (D175): the runtime library built
# for wasm32-wasip1 and what linking a program needs, in target/web/:
#   libnexo_runtime.a                 the runtime, for WebAssembly
#   crt1-command.o, libc.a            Rust's WASI C library
#   libwasi-emulated-*.a, libm.a      what the bundled SQLite calls
#   libclang_rt.builtins-wasm32.a     clang's builtins
#   rust-lld                          a wasm linker as new as that libc (a link
#                                     to the toolchain's, which loads its libLLVM)
# Needs `rustup target add wasm32-wasip1`, clang and wasi-libc (its headers
# and emulation libraries, for SQLite). Run from anywhere.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
out="$here/target/web"
sysroot="$here/target/wasi-sysroot"
rust=$(rustc --print sysroot)/lib/rustlib
contained="$rust/wasm32-wasip1/lib/self-contained"
wasi=${WASI_LIBC:-/usr/lib/wasm32-wasi}
headers=${WASI_HEADERS:-/usr/include/wasm32-wasi}
builtins=$(clang --print-resource-dir)/lib/wasi/libclang_rt.builtins-wasm32.a

# a sysroot for the C code of the runtime (SQLite): wasi-libc's headers
rm -rf "$sysroot"
mkdir -p "$sysroot/lib/wasm32-wasi"
ln -s "$headers" "$sysroot/include"
cp "$contained/crt1-command.o" "$contained/libc.a" "$sysroot/lib/wasm32-wasi/"
cp "$wasi"/libwasi-emulated-*.a "$wasi/libm.a" "$sysroot/lib/wasm32-wasi/"

# SQLite without threads, as libsqlite3-sys builds it for "wasm32-wasi"
WASI_SYSROOT="$sysroot" \
LIBSQLITE3_FLAGS="-USQLITE_THREADSAFE -DSQLITE_THREADSAFE=0 -DLONGDOUBLE_TYPE=double -D_WASI_EMULATED_MMAN -D_WASI_EMULATED_GETPID -D_WASI_EMULATED_SIGNAL -D_WASI_EMULATED_PROCESS_CLOCKS" \
    cargo build --manifest-path "$here/Cargo.toml" -p nexo-runtime --lib --release --target wasm32-wasip1

rm -rf "$out"
mkdir -p "$out"
cp "$here/target/wasm32-wasip1/release/libnexo_runtime.a" "$sysroot/lib/wasm32-wasi/"* "$builtins" "$out/"
ln -s "$rust/$(rustc -vV | sed -n "s/^host: //p")/bin/rust-lld" "$out/rust-lld"
echo "$out"
