"""Conversation response orchestration.

Plain conversational requests use streaming LLM text -> sentence queue ->
streaming TTS. Tool/action requests use the existing complete tool-calling and
provider-fallback pipeline.
"""

from __future__ import annotations

import re
import queue
import threading
import time

from tony_core import brain, memory, overlay, speech

_END = object()


def _clean_for_tts(sentence: str) -> str:
    s = re.sub(r"https?://\S+", "", sentence or "")
    s = re.sub(r"[*`#_~]+", "", s)
    s = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _sentence_stream(msg: str):
    sentences: queue.Queue = queue.Queue(maxsize=12)
    result = {"answer": None}
    generation = speech.current_generation()

    def generate():
        try:
            def publish(sentence: str):
                if speech._stop_requested(generation):
                    return
                clean_s = _clean_for_tts(sentence)
                if not clean_s:
                    return
                while not speech._stop_requested(generation):
                    try:
                        sentences.put(clean_s, timeout=0.1)
                        overlay.ui_reply(clean_s)
                        return
                    except queue.Full:
                        continue
            result["answer"] = brain.ask_tony_stream(
                msg, publish, expected_generation=generation
            )
        except Exception as exc:
            print(f"  [stream turn failed: {str(exc)[:80]}]")
        finally:
            while not speech._stop_requested(generation):
                try:
                    sentences.put(_END, timeout=0.1)
                    break
                except queue.Full:
                    continue

    started_at = time.perf_counter()
    worker = threading.Thread(target=generate, name="tony-llm-stream", daemon=True)
    worker.start()

    first = _END
    while not speech._stop_requested(generation):
        try:
            first = sentences.get(timeout=0.1)
            break
        except queue.Empty:
            if not worker.is_alive():
                break
    if first is not _END:
        print(f"  [stream: first complete sentence ready in {time.perf_counter() - started_at:.2f}s]")

    def remaining():
        if first is not _END:
            yield first
        while not speech._stop_requested(generation):
            try:
                item = sentences.get(timeout=0.1)
            except queue.Empty:
                if not worker.is_alive():
                    break
                continue
            if item is _END:
                break
            yield item

    return first, remaining(), result, worker, generation


def respond_and_speak(msg: str, allow_barge_in: bool = True) -> str:
    """Generate and speak one answer; return the complete text for logging/UI."""
    speech.STOP["now"] = False

    if brain.can_stream_conversation(msg):
        first, chunks, result, worker, generation = _sentence_stream(msg)
        if first is not _END:
            speech.wait_ack()
            if speech._stop_requested(generation):
                worker.join(timeout=1.0)
                return ""
            speech.speak_stream(
                chunks,
                allow_barge_in=allow_barge_in,
                expected_generation=generation,
            )
            worker.join(timeout=3.0)
            return result["answer"] or first
        worker.join(timeout=1.0)
        if speech._stop_requested(generation):
            return ""
        if result["answer"]:
            return result["answer"]

    answer = brain.ask_tony(msg)
    spoken = memory.core_reply(answer, detail=memory.wants_detail(msg))
    overlay.ui_reply(spoken)
    speech.wait_ack()
    speech.speak(spoken, allow_barge_in=allow_barge_in)
    return answer