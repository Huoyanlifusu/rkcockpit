"""Bounded byte output with absolute cursors and UTF-8 safe snapshots."""
import codecs


class OutputBuffer:
    """Keep the newest ``limit`` bytes and expose absolute byte cursors.

    The buffer stores subprocess bytes unchanged.  While a process is running,
    an incomplete UTF-8 sequence at the tail is withheld until the next append;
    callers therefore never render a replacement character merely because a
    multibyte character crossed two ``read()`` chunks.
    """

    def __init__(self, limit):
        if int(limit) <= 0:
            raise ValueError("limit must be positive")
        self.limit = int(limit)
        self._data = bytearray()
        self._base_offset = 0
        self._end_offset = 0
        self._pending_len = 0

    @property
    def base_offset(self):
        return self._base_offset

    @property
    def end_offset(self):
        return self._end_offset

    @property
    def truncated(self):
        return self._base_offset > 0

    def append(self, chunk):
        chunk = bytes(chunk)
        if not chunk:
            return
        self._data.extend(chunk)
        self._end_offset += len(chunk)
        excess = len(self._data) - self.limit
        if excess > 0:
            del self._data[:excess]
            self._base_offset += excess
            # Do not retain the continuation half of a valid UTF-8 character
            # at the new ring head.  The buffer may be up to three bytes below
            # its byte limit, which is preferable to rendering mojibake.
            while self._data and self._data[0] & 0xC0 == 0x80:
                del self._data[0]
                self._base_offset += 1
        # UTF-8 is at most four bytes wide.  Inspecting only the ring tail is
        # enough to find an incomplete sequence and keeps delta polling O(delta)
        # rather than decoding the whole 512 KiB ring on every request.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        decoder.decode(bytes(self._data[-4:]), final=False)
        pending, _flag = decoder.getstate()
        self._pending_len = len(pending)

    def snapshot(self, offset=None, final=False):
        """Return ``(text, next_offset, base_offset, reset)``.

        ``offset`` is an absolute byte cursor previously returned by this
        method.  A stale cursor (older than the retained ring) resets to the
        current base; a future cursor is invalid and raises ``ValueError``.
        """
        visible_len = len(self._data) if final else\
            len(self._data) - self._pending_len
        visible_end = self._base_offset + visible_len

        reset = False
        if offset is None:
            start = self._base_offset
        elif offset < self._base_offset:
            start = self._base_offset
            reset = True
        elif offset > visible_end:
            raise ValueError("offset exceeds current output")
        else:
            start = offset

        rel_start = start - self._base_offset
        if rel_start < visible_len and\
                self._data[rel_start] & 0xC0 == 0x80:
            raise ValueError("offset is not on a UTF-8 boundary")
        visible = bytes(self._data[rel_start:visible_len])
        text = visible.decode("utf-8", "replace")
        return text, visible_end, self._base_offset, reset
