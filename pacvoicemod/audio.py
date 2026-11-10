from collections import deque
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time

import numpy as np
import sounddevice as sd

from .engine import FRAMES, RATE

SAMPLE_SECONDS = 10
SAMPLE_FRAMES = SAMPLE_SECONDS * RATE


def endpoints():
    devices = sd.query_devices()
    inputs, outputs, playback = {}, {}, {}
    for index, device in enumerate(devices):
        if sd.query_hostapis(device["hostapi"])["name"] != "Windows WASAPI":
            continue
        name = device["name"]
        if device["max_input_channels"] and "cable output" not in name.casefold():
            inputs[f"{name} [{index}]"] = index
        if device["max_output_channels"]:
            choices = outputs if "cable input" in name.casefold() else playback
            choices[f"{name} [{index}]"] = index
    return inputs, outputs, playback


def play_sample(blocks, track, device, start_frame=0):
    samples = np.concatenate([block[track] for block in blocks])
    channels = min(2, sd.query_devices(device)["max_output_channels"])
    output = np.repeat(samples[start_frame:, None], channels, axis=1)
    sd.play(output, RATE, device=device, extra_settings=sd.WasapiSettings(auto_convert=True))
    return sd.get_stream()


def stop_playback():
    sd.stop()


class Audio:
    def __init__(self, engine, mic, output):
        self.engine = engine
        self.lock = Lock()
        self.closed = Event()
        self.jobs = Queue(maxsize=1)
        self.dry = deque()
        self.wet = {}
        self.sequence = 0
        self.generation = 0
        self.requested = None
        self.ends = None
        self.failures = 0
        self.sample = []
        self.sample_frames = 0
        self.sample_converted_frames = 0
        self.recording = False
        self.levels = (0, 0)
        self.dropouts = 0
        self.message = "Dry mic"
        settings = sd.WasapiSettings(auto_convert=True)
        if output is None:
            self.stream = sd.InputStream(device=mic, samplerate=RATE, blocksize=FRAMES,
                                         channels=1, dtype="int16", extra_settings=settings,
                                         callback=self.local_callback)
        else:
            channels = min(2, sd.query_devices(output)["max_output_channels"])
            self.stream = sd.Stream(device=(mic, output), samplerate=RATE, blocksize=FRAMES,
                                    channels=(1, channels), dtype="int16",
                                    extra_settings=(settings, settings), callback=self.callback)
        try:
            self.stream.start()
        except Exception:
            self.stream.close()
            raise
        self.worker = Thread(target=self.convert, daemon=True)
        self.worker.start()

    def activate(self, duration):
        with self.lock:
            if not self.recording and self.requested is None and not self.closed.is_set():
                self.begin_effect(duration)

    def begin_effect(self, duration):
        self.generation += 1
        self.requested = time.monotonic()
        self.duration = duration
        self.message = "Waiting for converted audio"

    def start_sample(self):
        with self.lock:
            if self.recording or self.requested is not None or self.closed.is_set():
                return False
            self.sample = []
            self.sample_frames = self.sample_converted_frames = 0
            self.recording = True
            self.begin_effect(SAMPLE_SECONDS)
            return True

    def stop_sample(self):
        with self.lock:
            self.finish_sample()

    def finish_sample(self):
        if self.recording:
            self.recording = False
            if self.requested is not None:
                self.reset("Dry mic")

    def sample_status(self):
        with self.lock:
            return self.recording, self.sample_frames, self.sample_converted_frames

    def sample_tracks(self):
        with self.lock:
            return tuple(self.sample)

    def reset(self, message):
        self.generation += 1
        self.requested = self.ends = None
        self.failures = 0
        self.wet.clear()
        self.message = message

    def cancel(self):
        with self.lock:
            self.recording = False
            self.reset("Dry mic")

    def convert(self):
        while not self.closed.is_set():
            try:
                sequence, generation, pcm = self.jobs.get(timeout=0.1)
            except Empty:
                continue
            try:
                converted = self.engine.convert(pcm)
            except Exception:
                with self.lock:
                    if self.generation == generation:
                        self.reset("Conversion failed. Dry mic restored.")
                continue
            with self.lock:
                if self.generation == generation and sequence >= self.sequence - 2:
                    self.wet[sequence] = np.frombuffer(converted, dtype="<i2")

    def callback(self, input_data, output_data, frames, timing, status):
        rendered = self.process(input_data, frames, status)
        if rendered is None:
            output_data.fill(0)
        else:
            output_data[:] = rendered[:, None]

    def local_callback(self, input_data, frames, timing, status):
        self.process(input_data, frames, status)

    def process(self, input_data, frames, status):
        if status:
            self.dropouts += 1
        current = input_data[:, 0].copy()
        self.dry.append(current)
        if len(self.dry) <= 2:
            return None
        dry = self.dry.popleft()
        now = time.monotonic()
        with self.lock:
            sequence = self.sequence
            self.sequence += 1
            rendered = dry
            converted = False
            if self.ends is not None and now >= self.ends:
                self.reset("Dry mic")
            if self.requested is not None:
                try:
                    self.jobs.put_nowait((sequence, self.generation, current.tobytes()))
                except Full:
                    pass
                wet = self.wet.pop(sequence - 2, None)
                for late in tuple(self.wet):
                    if late < sequence - 2:
                        del self.wet[late]
                if wet is not None:
                    rendered = wet
                    converted = True
                    self.failures = 0
                    if self.ends is None:
                        self.ends = now + self.duration
                        self.message = "Voice active"
                elif self.ends is not None:
                    self.failures += 1
                    if self.failures >= 3:
                        self.reset("Conversion too slow. Dry mic restored.")
                elif now - self.requested > 2:
                    self.reset("Conversion did not start. Dry mic restored.")
            if self.recording:
                count = min(frames, SAMPLE_FRAMES - self.sample_frames)
                self.sample.append((dry[:count], rendered[:count]))
                self.sample_frames += count
                if converted:
                    self.sample_converted_frames += count
                if self.sample_frames >= SAMPLE_FRAMES:
                    self.finish_sample()
            self.levels = tuple(int(np.max(np.abs(block.astype(np.int32)))) for block in (current, rendered))
        return rendered

    def close(self):
        self.closed.set()
        self.cancel()
        self.stream.close()
        self.worker.join(timeout=2)
