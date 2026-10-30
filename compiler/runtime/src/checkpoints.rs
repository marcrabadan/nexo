//! D151 checkpoint commit hooks; confined state supplies durable synchronization.
pub fn commit(
    write_and_sync: impl FnOnce() -> std::io::Result<()>,
    replace: impl FnOnce() -> std::io::Result<()>,
    sync_directory: impl FnOnce() -> std::io::Result<()>,
) -> std::io::Result<()> {
    write_and_sync()?;
    replace()?;
    sync_directory()
}
