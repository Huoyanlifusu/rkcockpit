"""Utilities for host.service.fs."""
import os

from host.task.transfer import ProgressReader, ProgressWriter, TransferJobStore
from host.transport import LocalTransport, TransportError


def _size(transport, path):
    try:
        return int(transport.stat(path).get("size") or 0)
    except Exception:
        return 0


def fs_copy(jobs, transport, device_name, src, dest, total_hint=0,
            cleanup=None):
    """Handle fs copy."""
    src = os.path.abspath(os.path.expanduser(src))
    probe = LocalTransport()
    try:
        total = total_hint or int(probe.stat(src).get("size") or 0)
    except Exception:
        raise TransportError("上位机文件不存在: %s" % src)
    finally:
        probe.close()

    def run(job):
        job["bytes_total"] = total
        local = LocalTransport()
        try:
            with local.open_read(src) as fh:
                transport.upload(ProgressReader(fh, job), dest, total,
                                 job=job)
        finally:
            local.close()

    return jobs.submit(device_name, "upload", os.path.basename(src),
                       src, dest, run, cleanup=cleanup)


def fs_copyfrom(jobs, transport, device_name, src, dest):
    """Handle fs copyfrom."""
    probe = LocalTransport()
    try:
        # Validate now; the actual open below repeats the descriptor-relative
        # resolution after the job starts.
        probe._location(dest, write=True)
    finally:
        probe.close()
    total = _size(transport, src)

    def run(job):
        job["bytes_total"] = total
        local = LocalTransport()
        try:
            with local.open_write(dest) as fh:
                transport.download(src, ProgressWriter(fh, job), job=job)
        finally:
            local.close()

    return jobs.submit(device_name, "download", os.path.basename(src),
                       src, dest, run)


def fs_raw_upload(jobs, transport, device_name, path, name, reader):
    """Handle fs raw upload."""
    name = os.path.basename(str(name))
    dest = os.path.join(path.rstrip("/"), name)
    total = getattr(reader, "size_hint", 0)

    def run(job):
        job["bytes_total"] = total or _size(transport, dest)
        transport.upload(ProgressReader(reader, job), dest, total, job=job)

    return jobs.submit(device_name, "raw_upload", name, "-", dest, run)
