# Outer Docker seccomp policy

`seccomp.json` is the unchanged Apache-2.0 Moby default profile at
https://github.com/moby/profiles/blob/4b8214fc4971e06172938a1f1554ccff6647ee80/seccomp/default.json
Its license is retained in `SECCOMP-LICENSE`.

The profile defaults to errno denial, includes argument/capability restrictions,
and permits the Landlock, seccomp, openat2 and pidfd calls needed to install
Shield's narrower worker filter. `clone3` reports ENOSYS without CAP_SYS_ADMIN
so libc can use the constrained clone fallback. io_uring is not allowed.
No `seccomp=unconfined` or privileged-container fallback is provided.
