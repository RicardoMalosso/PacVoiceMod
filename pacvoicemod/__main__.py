import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from .audio import Audio, endpoints
from .engine import Engine
from .twitch import is_command, listen

CONFIG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PacVoiceMod" / "settings.json"
CABLE = "https://vb-audio.com/Cable/index.htm"


class App:
    def __init__(self, root):
        self.root = root
        self.audio = None
        self.engine = None
        self.preparing = False
        self.twitch_stop = None
        self.events = SimpleQueue()
        self.devices = {}
        try:
            saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        root.title("PacVoiceMod")
        root.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(root, padding=14)
        panel.grid(sticky="nsew")
        panel.columnconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        self.mic = tk.StringVar(value=saved.get("mic", ""))
        self.output = tk.StringVar(value=saved.get("output", ""))
        self.port = tk.StringVar(value=saved.get("port", "18888"))
        self.slot = tk.StringVar(value=saved.get("slot", "90"))
        self.command = tk.StringVar(value=saved.get("command", "!voice"))
        self.duration = tk.StringVar(value=saved.get("duration", "20"))
        self.restricted = tk.BooleanVar(value=saved.get("restricted", True))
        self.audio_status = tk.StringVar(value="Audio stopped")
        self.model_status = tk.StringVar(value="Start the local VCClient CUDA server first.")
        self.twitch_status = tk.StringVar(value="Twitch disconnected")
        self.meters = tk.StringVar(value="")
        self.inputs = ttk.Combobox(panel, textvariable=self.mic, state="readonly", width=45)
        self.outputs = ttk.Combobox(panel, textvariable=self.output, state="readonly", width=45)

        for row, (label, widget) in enumerate([
            ("Microphone", self.inputs), ("Virtual output", self.outputs),
            ("VCClient HTTP port", ttk.Entry(panel, textvariable=self.port)),
            ("Dedicated model slot (0–99)", ttk.Entry(panel, textvariable=self.slot)),
            ("Chat command", ttk.Entry(panel, textvariable=self.command)),
            ("Effect duration (seconds)", ttk.Entry(panel, textvariable=self.duration)),
        ]):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            widget.grid(row=row, column=1, sticky="ew")
        ttk.Checkbutton(panel, text="Broadcaster and moderators only", variable=self.restricted).grid(
            row=6, column=1, sticky="w")

        actions = ttk.Frame(panel)
        actions.grid(row=7, columnspan=2, sticky="w", pady=8)
        for index, (label, action) in enumerate([
            ("Refresh devices", self.refresh), ("Get VB-CABLE", lambda: webbrowser.open(CABLE)),
            ("Import voice", self.pick_model), ("Load saved slot", self.prepare),
            ("Start audio", self.start_audio), ("Stop audio", self.stop_audio),
            ("Connect Twitch", self.connect_twitch), ("Disconnect Twitch", self.disconnect_twitch),
            ("Test voice", self.activate), ("Return to normal", self.cancel),
        ]):
            ttk.Button(actions, text=label, command=action).grid(
                row=index // 3, column=index % 3, sticky="ew", padx=2, pady=2)
        for row, variable in enumerate([self.model_status, self.audio_status, self.twitch_status, self.meters], 8):
            ttk.Label(panel, textvariable=variable, wraplength=520).grid(row=row, columnspan=2, sticky="w")
        self.refresh()
        self.root.after(100, self.poll)

    def refresh(self):
        try:
            microphones, outputs = endpoints()
        except Exception as exc:
            self.audio_status.set(str(exc))
            return
        self.devices = microphones | outputs
        for variable, widget, choices in [(self.mic, self.inputs, microphones),
                                          (self.output, self.outputs, outputs)]:
            widget["values"] = tuple(choices)
            if variable.get() not in choices:
                name = variable.get().rsplit(" [", 1)[0]
                matches = [item for item in choices if item.rsplit(" [", 1)[0] == name]
                variable.set(matches[0] if len(matches) == 1 else
                             next(iter(choices)) if len(choices) == 1 else "")
        if not outputs:
            self.audio_status.set("CABLE Input missing. Install VB-CABLE, reboot, then refresh.")

    def save(self):
        values = {key: getattr(self, key).get() for key in
                  ("mic", "output", "port", "slot", "command", "duration", "restricted")}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG.with_suffix(".tmp")
        temporary.write_text(json.dumps(values), encoding="utf-8")
        temporary.replace(CONFIG)

    def pick_model(self):
        weights = filedialog.askopenfilename(filetypes=[("RVC voice", "*.zip *.pth")])
        if not weights:
            return
        weights = Path(weights)
        index = ""
        if weights.suffix.lower() == ".pth" and messagebox.askyesno("Index", "Add an optional .index file?"):
            index = filedialog.askopenfilename(filetypes=[("RVC index", "*.index")])
        if messagebox.askyesno("Trust model files",
                               "VCClient loads these model files. Only continue if you trust the "
                               "publisher and both files. Import replaces the selected VCClient slot."):
            self.prepare(weights, Path(index) if index else None)

    def prepare(self, weights=None, index=None):
        if self.preparing or self.audio:
            messagebox.showinfo("Model", "Stop audio and wait for any current import before loading a voice.")
            return
        try:
            port, slot = int(self.port.get()), int(self.slot.get())
            if not 1 <= port <= 65535 or not 0 <= slot <= 99:
                raise ValueError("Enter a valid port and a slot from 0 to 99.")
            self.save()
        except (ValueError, OSError) as exc:
            messagebox.showerror("Model", str(exc))
            return
        self.engine = None
        self.preparing = True
        self.model_status.set("Loading and warming the RVC voice…")

        def load():
            engine = Engine(port, slot)
            try:
                engine.prepare(weights, index)
            except Exception as exc:
                self.events.put(("model", (None, str(exc))))
            else:
                self.events.put(("model", (engine, f"RVC slot {slot} ready")))
        Thread(target=load, daemon=True).start()

    def start_audio(self):
        if self.audio or self.preparing:
            return
        try:
            self.save()
            self.audio = Audio(self.engine, self.devices[self.mic.get()], self.devices[self.output.get()])
        except Exception as exc:
            self.audio_status.set(f"Audio could not start. Check mic and cable selections. {exc}")

    def stop_audio(self):
        if self.audio:
            self.audio.close()
            self.audio = None
        self.audio_status.set("Audio stopped")
        self.meters.set("")

    def activate(self):
        if not self.audio or not self.engine:
            return
        try:
            duration = int(self.duration.get())
            if not 1 <= duration <= 300:
                raise ValueError()
        except ValueError:
            self.twitch_status.set("Enter an effect duration between 1 and 300 seconds.")
            return
        self.audio.activate(duration)

    def cancel(self):
        if self.audio:
            self.audio.cancel()

    def connect_twitch(self):
        if self.twitch_stop is not None:
            return
        if not self.command.get().startswith("!") or len(self.command.get().split()) != 1:
            messagebox.showerror("Twitch", "Enter a command such as !voice.")
            return
        try:
            self.save()
        except OSError as exc:
            messagebox.showerror("Twitch", str(exc))
            return
        stop = self.twitch_stop = Event()
        self.twitch_status.set("Connecting to Twitch…")

        def run():
            try:
                listen(stop, lambda msg: self.events.put(("status", (stop, msg))),
                       lambda event, user: self.events.put(("chat", (stop, event, user))))
            except Exception as exc:
                # Exceptions may include HTTP context. Keep token-bearing requests out of UI logs.
                self.events.put(("status", (stop, f"Twitch error: {type(exc).__name__}. Reconnect to try again.")))
            finally:
                self.events.put(("finished", (stop,)))
        Thread(target=run, daemon=True).start()

    def disconnect_twitch(self):
        if self.twitch_stop:
            self.twitch_stop.set()
            self.twitch_stop = None
        self.twitch_status.set("Twitch disconnected")

    def poll(self):
        for _ in range(100):
            try:
                kind, values = self.events.get_nowait()
            except Empty:
                break
            if kind == "model":
                self.engine, status = values
                self.preparing = False
                self.model_status.set(status)
            elif values[0] is self.twitch_stop:
                if kind == "status":
                    self.twitch_status.set(values[1])
                elif kind == "chat" and is_command(values[1], values[2], self.command.get(), self.restricted.get()):
                    self.activate()
                elif kind == "finished":
                    self.twitch_stop = None
        if self.audio:
            if not self.audio.stream.active:
                self.stop_audio()
                self.audio_status.set("Audio device stopped. Refresh devices and start audio again.")
            else:
                left, right = self.audio.levels
                self.audio_status.set(self.audio.message)
                self.meters.set(f"Mic {left * 100 // 32768}% · Output {right * 100 // 32768}% · "
                                f"Audio buffer warnings {self.audio.dropouts}")
        self.root.after(100, self.poll)

    def close(self):
        self.disconnect_twitch()
        self.stop_audio()
        try:
            self.save()
        finally:
            self.root.destroy()


def main():
    if os.name != "nt":
        raise SystemExit("PacVoiceMod requires Windows WASAPI.")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
