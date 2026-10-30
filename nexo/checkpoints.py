"""D151 commit hooks; confined agent state supplies durable synchronization."""

def commit(write_and_sync, replace, sync_directory):
    write_and_sync()
    replace()
    sync_directory()
