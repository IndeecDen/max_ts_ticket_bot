"""Per-job OS lock: released by the OS even after process termination."""
import os


class DeliveryBusy(RuntimeError):
    pass


class DeliveryLock:
    def __init__(self, database_path, job_id):
        directory = database_path.parent / (database_path.name + '.delivery-locks')
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / f'{job_id}.lock').open('a+b')
        try:
            self.file.seek(0, 2)
            if self.file.tell() == 0:
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise DeliveryBusy('Задание сейчас отправляется или восстанавливается.') from None

    def close(self):
        if not self.file.closed:
            self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
