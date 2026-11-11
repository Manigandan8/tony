"""One-owner microphone capture with fan-out subscriptions."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 480                 # 30 ms; valid WebRTC-VAD frame duration
SAMPLE_WIDTH_BYTES = 2              # int16 PCM
DEFAULT_QUEUE_FRAMES = 100          # 3 seconds of buffering per consumer


@dataclass(frozen=True, slots=True)
class AudioPacket:
    sequence: int
    captured_at: float
    pcm: bytes


class AudioSubscription:
    """A private queue over the shared microphone stream."""

    def __init__(self, hub: "AudioHub", name: str, max_frames: int):
        self._hub = hub
        self.name = name
        self._queue: queue.Queue[AudioPacket] = queue.Queue(maxsize=max_frames)
        self._closed = False
        self._pending = bytearray()
        self.dropped_frames = 0

    def read_packet(self, timeout: Optional[float] = 1.0) -> AudioPacket:
        if self._closed:
            raise RuntimeError(f"Audio subscription '{self.name}' is closed")
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"No microphone frame for {self.name}") from exc

    def read(self, timeout: Optional[float] = 1.0) -> bytes:
        return self.read_packet(timeout).pcm

    def read_samples(self, sample_count: int, timeout: Optional[float] = 1.0) -> bytes:
        if sample_count <= 0:
            return b""
        wanted = sample_count * SAMPLE_WIDTH_BYTES
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self._pending) < wanted:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0.0:
                raise TimeoutError(f"Timed out collecting {sample_count} samples for {self.name}")
            packet = self.read_packet(remaining)
            self._pending.extend(packet.pcm)
        result = bytes(self._pending[:wanted])
        del self._pending[:wanted]
        return result

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._hub.unsubscribe(self)
            self._pending.clear()

    def __enter__(self) -> "AudioSubscription":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class AudioHub:
    """Own the sole microphone InputStream and distribute PCM frames."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stream = None
        self._device = None
        self._subscribers: set[AudioSubscription] = set()
        self._sequence = 0
        self._last_status = ""
        self._started_at = 0.0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._stream is not None

    @property
    def device(self):
        with self._lock:
            return self._device

    @property
    def last_status(self) -> str:
        with self._lock:
            return self._last_status

    def start(self, device=None) -> None:
        with self._lock:
            current = self._stream
            current_device = self._device
            if current is not None and current_device == device:
                return
            self._stream = None
            self._device = None
        self._close_stream(current)

        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice is required for Tony's shared microphone") from exc

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=device,
            callback=self._on_audio,
        )
        try:
            stream.start()
        except Exception:
            self._close_stream(stream)
            raise

        with self._lock:
            self._stream = stream
            self._device = device
            self._started_at = time.monotonic()
            self._last_status = ""

    def restart(self, device=None) -> None:
        self.stop()
        self.start(device=device)

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            self._device = None
        self._close_stream(stream)

    @staticmethod
    def _close_stream(stream) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def subscribe(self, name: str, max_frames: int = DEFAULT_QUEUE_FRAMES) -> AudioSubscription:
        with self._lock:
            if self._stream is None:
                raise RuntimeError("AudioHub is not running")
            sub = AudioSubscription(self, name, max(4, int(max_frames)))
            self._subscribers.add(sub)
            return sub

    def unsubscribe(self, sub: AudioSubscription) -> None:
        with self._lock:
            self._subscribers.discard(sub)

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if status:
            with self._lock:
                self._last_status = str(status)
        try:
            pcm = bytes(indata)
        except Exception:
            return

        with self._lock:
            self._sequence += 1
            packet = AudioPacket(self._sequence, time.monotonic(), pcm)
            subscribers = tuple(self._subscribers)

        for sub in subscribers:
            if sub._closed:
                continue
            try:
                sub._queue.put_nowait(packet)
            except queue.Full:
                try:
                    sub._queue.get_nowait()
                    sub.dropped_frames += 1
                except queue.Empty:
                    pass
                try:
                    sub._queue.put_nowait(packet)
                except queue.Full:
                    sub.dropped_frames += 1


_HUB = AudioHub()


def get_audio_hub() -> AudioHub:
    return _HUB