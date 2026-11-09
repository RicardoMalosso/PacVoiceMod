import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from .audio import Audio, SAMPLE_SECONDS, endpoints, play_sample, stop_playback
from .engine import Engine, EngineUnavailable, RATE
from .locale import translate
from .twitch import is_command, listen
from .vcclient import LocalVCClient, StartupError, launch_command

CONFIG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PacVoiceMod" / "settings.json"
MODELS = Path(__file__).resolve().parents[1] / "models"
CABLE = "https://vb-audio.com/Cable/index.htm"


def language_flags():
    # Tk on Windows may display flag emoji as two letters.
    brazil = tk.PhotoImage(width=24, height=16)
    brazil.put("#009b3a", to=(0, 0, 24, 16))
    for y, width in enumerate((2, 6, 10, 14, 18, 14, 10, 6, 2), 4):
        brazil.put("#ffdf00", to=(12 - width // 2, y, 12 + width // 2, y + 1))
    for y, width in enumerate((4, 6, 8, 8, 6, 4), 6):
        brazil.put("#002776", to=(12 - width // 2, y, 12 + width // 2, y + 1))
    brazil.put("#ffffff", to=(9, 8, 12, 9))
    brazil.put("#ffffff", to=(12, 9, 15, 10))

    canada = tk.PhotoImage(width=24, height=16)
    canada.put("#ffffff", to=(0, 0, 24, 16))
    canada.put("#d80621", to=(0, 0, 6, 16))
    canada.put("#d80621", to=(18, 0, 24, 16))
    leaf = (".....#.....", "....###....", "....###....", ".#..###..#.",
            ".##.###.##.", "###########", ".#########.", "..#######..",
            "...#####...", ".....#.....", ".....#.....")
    for y, row in enumerate(leaf, 2):
        for x, pixel in enumerate(row, 7):
            if pixel == "#":
                canada.put("#d80621", to=(x, y))
    return brazil, canada


class App:
    def __init__(self, root):
        self.root = root
        self.audio = None
        self.engine = None
        self.preparing = False
        self.twitch_stop = None
        self.events = SimpleQueue()
        self.devices = {}
        self.playback_devices = {}
        self.last_sample = ()
        self.last_sample_status = (False, 0, 0)
        try:
            saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.vcclient = LocalVCClient(CONFIG.with_name("vcclient.log"))
        self.vcclient_folder = saved.get("vcclient_folder", "")
        self.language = saved.get("language", "en-US")
        self.localized_widgets = []
        self.localized_statuses = {}
        self.checklist = {}
        root.title("PacVoiceMod")
        root.protocol("WM_DELETE_WINDOW", self.close)
        languages = ttk.Frame(root, padding=(14, 8, 14, 0))
        languages.grid(row=0, columnspan=2, sticky="w")
        self.flags = language_flags()
        for icon, name, language in zip(self.flags, ("Português", "English"), ("pt-BR", "en-US")):
            ttk.Button(languages, text=name, image=icon, compound="left",
                       command=lambda selected=language: self.set_language(selected)).pack(side="left")
        panel = ttk.Frame(root, padding=14)
        panel.grid(row=1, column=0, sticky="nsew")
        panel.columnconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        self.mic = tk.StringVar(value=saved.get("mic", ""))
        self.output = tk.StringVar(value=saved.get("output", ""))
        self.playback = tk.StringVar(value=saved.get("playback", ""))
        self.sample_status = tk.StringVar(value="No sample recorded")
        self.port = tk.StringVar(value=saved.get("port", "18888"))
        self.slot = tk.StringVar(value=saved.get("slot", "90"))
        self.command = tk.StringVar(value=saved.get("command", "!voice"))
        self.duration = tk.StringVar(value=saved.get("duration", "20"))
        self.restricted = tk.BooleanVar(value=saved.get("restricted", True))
        self.audio_status = tk.StringVar(value="Audio stopped")
        self.model_status = tk.StringVar(value="Choose the extracted VCClient CUDA folder.")
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
            self.localized(ttk.Label, panel, label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            widget.grid(row=row, column=1, sticky="ew")
        self.localized(ttk.Checkbutton, panel, "Broadcaster and moderators only", variable=self.restricted).grid(
            row=6, column=1, sticky="w")

        actions = ttk.Frame(panel)
        actions.grid(row=7, columnspan=2, sticky="w", pady=8)
        for index, (label, action) in enumerate([
            ("Refresh devices", self.refresh), ("Get VB-CABLE", lambda: webbrowser.open(CABLE)),
            ("Import voice", self.pick_model), ("Load saved slot", self.prepare),
            ("Set up VCClient", self.select_vcclient),
            ("Start audio", self.start_audio), ("Stop audio", self.stop_audio),
            ("Connect Twitch", self.connect_twitch), ("Disconnect Twitch", self.disconnect_twitch),
            ("Test voice", self.activate), ("Return to normal", self.cancel),
        ]):
            self.localized(ttk.Button, actions, label, command=action).grid(
                row=index // 3, column=index % 3, sticky="ew", padx=2, pady=2)
        for row, variable in enumerate([self.model_status, self.audio_status, self.twitch_status, self.meters], 8):
            ttk.Label(panel, textvariable=variable, wraplength=520).grid(row=row, columnspan=2, sticky="w")
        self.localized(ttk.Label, panel, "Model ZIPs go in models/ or can be selected from Downloads.",
                  wraplength=520).grid(row=12, columnspan=2, sticky="w")
        recorder = self.localized(ttk.LabelFrame, panel, "Sample recorder", padding=8)
        recorder.grid(row=13, columnspan=2, sticky="ew", pady=(10, 0))
        self.localized(ttk.Label, recorder, "Headphones / speakers").grid(row=0, column=0, sticky="w")
        self.playback_outputs = ttk.Combobox(recorder, textvariable=self.playback,
                                             state="readonly", width=36)
        self.playback_outputs.grid(row=0, column=1, columnspan=2, sticky="ew")
        for index, (label, action) in enumerate([
            ("Record sample", self.record_sample), ("Stop recording", self.stop_sample),
            ("Stop playback", stop_playback), ("Play original", lambda: self.play(0)),
            ("Play output", lambda: self.play(1)),
        ]):
            self.localized(ttk.Button, recorder, label, command=action).grid(
                row=1 + index // 3, column=index % 3, sticky="ew", padx=2, pady=2)
        ttk.Label(recorder, textvariable=self.sample_status, wraplength=520).grid(
            row=3, columnspan=3, sticky="w")
        setup = self.localized(ttk.LabelFrame, root, "Setup checklist", padding=10)
        setup.grid(row=1, column=1, sticky="ne", padx=(0, 14), pady=14)
        self.localized(ttk.Label, setup, "Check each step as you finish it.").pack(anchor="w", pady=(0, 6))
        for key, step in [
            ("cable", "Install VB-CABLE and reboot"),
            ("engine", "Choose the extracted VCClient CUDA folder"),
            ("model", "Import a voice ZIP from models/"),
            ("devices", "Select your mic and CABLE Input"),
            ("record", "Start audio and record a test"),
            ("obs", "Select CABLE Output in OBS"),
            ("twitch", "Connect Twitch and test the chat command"),
        ]:
            checked = tk.BooleanVar(value=saved.get("checklist", {}).get(key, False))
            self.checklist[key] = checked
            self.localized(ttk.Checkbutton, setup, step, variable=checked).pack(anchor="w", pady=2)
        for status in (self.audio_status, self.model_status, self.twitch_status, self.sample_status):
            self.set_status(status, status.get())
        self.refresh()
        self.root.after(100, self.poll)
        if self.vcclient_folder:
            self.root.after_idle(self.prepare)

    def tr(self, message, **values):
        return translate(self.language, message, **values)

    def localized(self, widget_type, parent, message, **options):
        widget = widget_type(parent, text=self.tr(message), **options)
        self.localized_widgets.append((widget, message))
        return widget

    def set_status(self, variable, message, **values):
        self.localized_statuses[str(variable)] = (variable, message, values)
        variable.set(self.tr(message, **values))

    def set_language(self, language):
        self.language = language
        for widget, message in self.localized_widgets:
            widget.configure(text=self.tr(message))
        for variable, message, values in self.localized_statuses.values():
            variable.set(self.tr(message, **values))

    def refresh(self):
        try:
            microphones, outputs, playback = endpoints()
        except Exception as exc:
            self.set_status(self.audio_status, str(exc))
            return
        self.devices = microphones | outputs
        self.playback_devices = playback
        for variable, widget, choices in [(self.mic, self.inputs, microphones),
                                          (self.output, self.outputs, outputs),
                                          (self.playback, self.playback_outputs, playback)]:
            widget["values"] = tuple(choices)
            if variable.get() not in choices:
                name = variable.get().rsplit(" [", 1)[0]
                matches = [item for item in choices if item.rsplit(" [", 1)[0] == name]
                variable.set(matches[0] if len(matches) == 1 else
                             next(iter(choices)) if len(choices) == 1 else "")
        if not outputs:
            self.set_status(self.audio_status, "CABLE Input missing. Install VB-CABLE, reboot, then refresh.")

    def save(self):
        values = {key: getattr(self, key).get() for key in
                  ("mic", "output", "playback", "port", "slot", "command", "duration", "restricted")}
        values["vcclient_folder"] = self.vcclient_folder
        values["language"] = self.language
        values["checklist"] = {key: checked.get() for key, checked in self.checklist.items()}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG.with_suffix(".tmp")
        temporary.write_text(json.dumps(values), encoding="utf-8")
        temporary.replace(CONFIG)

    def pick_model(self):
        weights = filedialog.askopenfilename(
            title=self.tr("Import RVC ZIP or checkpoint"), initialdir=MODELS,
            filetypes=[(self.tr("RVC voice"), "*.zip *.pth")],
        )
        if not weights:
            return
        weights = Path(weights)
        index = ""
        if weights.suffix.lower() == ".pth" and messagebox.askyesno(self.tr("Index"), self.tr("Add an optional .index file?")):
            index = filedialog.askopenfilename(filetypes=[(self.tr("RVC index"), "*.index")])
        if messagebox.askyesno(self.tr("Trust model files"), self.tr(
                "VCClient loads these model files. Only continue if you trust the "
                "publisher and both files. Import replaces the selected VCClient slot.")):
            self.prepare(weights, Path(index) if index else None)

    def select_vcclient(self, weights=None, index=None):
        if self.audio or self.preparing:
            messagebox.showinfo(self.tr("Model"), self.tr(
                "Stop audio and wait for any current import before loading a voice."))
            return
        selected = filedialog.askdirectory(
            title=self.tr("Choose the extracted VCClient CUDA folder"),
            initialdir=self.vcclient_folder or None,
        )
        if not selected:
            return
        try:
            launch_command(Path(selected), 18888)
        except StartupError as exc:
            messagebox.showerror(self.tr("Model"), self.tr(exc.message, **exc.details))
            return
        self.vcclient_folder = selected
        self.checklist["engine"].set(True)
        self.prepare(weights, index)

    def prepare(self, weights=None, index=None):
        if self.preparing or self.audio:
            messagebox.showinfo(self.tr("Model"), self.tr("Stop audio and wait for any current import before loading a voice."))
            return
        try:
            port, slot = int(self.port.get()), int(self.slot.get())
            if not 1 <= port <= 65535 or not 0 <= slot <= 99:
                raise ValueError(self.tr("Enter a valid port and a slot from 0 to 99."))
            self.save()
        except (ValueError, OSError) as exc:
            messagebox.showerror(self.tr("Model"), str(exc))
            return
        folder = Path(self.vcclient_folder) if self.vcclient_folder else None
        self.engine = None
        self.preparing = True
        self.set_status(self.model_status, "Loading and warming the RVC voice…")

        def load():
            engine = Engine(port, slot)
            try:
                self.vcclient.ensure_running(
                    engine, folder, port,
                    lambda: self.events.put(("model_status", ("Starting VCClient…",))),
                )
                if self.vcclient.closed.is_set():
                    return
            except EngineUnavailable:
                self.events.put(("choose_vcclient", (weights, index)))
                return
            except StartupError as exc:
                self.events.put(("model", (None, exc.message, exc.details)))
                return
            except Exception as exc:
                self.events.put(("model", (None, str(exc), {})))
                return
            self.events.put(("model_status", ("Loading and warming the RVC voice…",)))
            try:
                engine.prepare(weights, index)
            except EngineUnavailable as exc:
                self.events.put(("model", (None, exc.message, {"address": exc.address})))
            except Exception as exc:
                self.events.put(("model", (None, str(exc), {})))
            else:
                self.events.put(("model", (engine, "RVC slot {slot} ready", {"slot": slot})))
        Thread(target=load, daemon=True).start()

    def start_audio(self):
        if self.audio or self.preparing:
            return
        try:
            self.save()
            self.audio = Audio(self.engine, self.devices[self.mic.get()], self.devices[self.output.get()])
        except Exception as exc:
            self.set_status(self.audio_status, "Audio could not start. Check mic and cable selections. {error}", error=exc)

    def stop_audio(self):
        if self.audio:
            self.audio.close()
            self.remember_sample()
            self.audio = None
        self.set_status(self.audio_status, "Audio stopped")
        self.set_status(self.meters, "")

    def record_sample(self):
        if not self.audio or not self.engine:
            messagebox.showinfo(self.tr("Sample"), self.tr("Load a voice and start audio first."))
            return
        stop_playback()
        if not self.audio.start_sample():
            messagebox.showinfo(self.tr("Sample"), self.tr("Wait for the current effect or recording to finish."))
            return
        self.last_sample = ()
        self.last_sample_status = (False, 0, 0)

    def stop_sample(self):
        if self.audio:
            self.audio.stop_sample()
            self.remember_sample()

    def remember_sample(self):
        status = self.audio.sample_status()
        if not status[0] and status[1]:
            self.last_sample = self.audio.sample_tracks()
            self.last_sample_status = status

    def play(self, track):
        if self.audio and self.audio.sample_status()[0]:
            messagebox.showinfo(self.tr("Playback"), self.tr("Stop recording first."))
            return
        if self.audio:
            self.remember_sample()
        if not self.last_sample:
            messagebox.showinfo(self.tr("Playback"), self.tr("Record a sample first."))
            return
        device = self.playback_devices.get(self.playback.get())
        if device is None:
            messagebox.showinfo(self.tr("Playback"), self.tr("Select your headphones or speakers."))
            return
        try:
            play_sample(self.last_sample, track, device)
        except Exception as exc:
            messagebox.showerror(self.tr("Playback"), str(exc))

    def activate(self):
        if not self.audio or not self.engine:
            return
        try:
            duration = int(self.duration.get())
            if not 1 <= duration <= 300:
                raise ValueError()
        except ValueError:
            self.set_status(self.twitch_status, "Enter an effect duration between 1 and 300 seconds.")
            return
        self.audio.activate(duration)

    def cancel(self):
        if self.audio:
            self.audio.cancel()

    def connect_twitch(self):
        if self.twitch_stop is not None:
            return
        if not self.command.get().startswith("!") or len(self.command.get().split()) != 1:
            messagebox.showerror(self.tr("Twitch"), self.tr("Enter a command such as !voice."))
            return
        try:
            self.save()
        except OSError as exc:
            messagebox.showerror(self.tr("Twitch"), str(exc))
            return
        stop = self.twitch_stop = Event()
        self.set_status(self.twitch_status, "Connecting to Twitch…")

        def run():
            try:
                listen(stop, lambda msg, **details: self.events.put(("status", (stop, msg, details))),
                       lambda event, user: self.events.put(("chat", (stop, event, user))))
            except Exception as exc:
                # Exceptions may include HTTP context. Keep token-bearing requests out of UI logs.
                self.events.put(("status", (stop, "Twitch error: {error}. Reconnect to try again.", {"error": type(exc).__name__})))
            finally:
                self.events.put(("finished", (stop,)))
        Thread(target=run, daemon=True).start()

    def disconnect_twitch(self):
        if self.twitch_stop:
            self.twitch_stop.set()
            self.twitch_stop = None
        self.set_status(self.twitch_status, "Twitch disconnected")

    def poll(self):
        for _ in range(100):
            try:
                kind, values = self.events.get_nowait()
            except Empty:
                break
            if kind == "choose_vcclient":
                self.preparing = False
                self.set_status(self.model_status, "Choose the extracted VCClient CUDA folder.")
                self.select_vcclient(*values)
            elif kind == "model_status":
                self.set_status(self.model_status, values[0])
            elif kind == "model":
                self.engine, status, details = values
                self.preparing = False
                self.set_status(self.model_status, status, **details)
            elif values[0] is self.twitch_stop:
                if kind == "status":
                    self.set_status(self.twitch_status, values[1], **values[2])
                elif kind == "chat" and is_command(values[1], values[2], self.command.get(), self.restricted.get()):
                    self.activate()
                elif kind == "finished":
                    self.twitch_stop = None
        if self.audio:
            if not self.audio.stream.active:
                self.stop_audio()
                self.set_status(self.audio_status, "Audio device stopped. Refresh devices and start audio again.")
            else:
                left, right = self.audio.levels
                self.set_status(self.audio_status, self.audio.message)
                self.set_status(self.meters, "Mic {mic}% · Output {output}% · Audio buffer warnings {warnings}",
                                mic=left * 100 // 32768, output=right * 100 // 32768,
                                warnings=self.audio.dropouts)
        recording, frames, converted = self.audio.sample_status() if self.audio else (False, 0, 0)
        if recording:
            self.set_status(self.sample_status, "Recording {seconds:.1f} / {limit} s", seconds=frames / RATE, limit=SAMPLE_SECONDS)
        else:
            _, frames, converted = (False, frames, converted) if frames else self.last_sample_status
            if frames:
                detail = (self.tr("{seconds:.1f} s converted", seconds=converted / RATE) if converted else
                          self.tr("No converted audio reached the output"))
                self.set_status(self.sample_status, "Sample {seconds:.1f} s · {detail}", seconds=frames / RATE, detail=detail)
            else:
                self.set_status(self.sample_status, "No sample recorded")
        self.root.after(100, self.poll)

    def close(self):
        stop_playback()
        self.disconnect_twitch()
        self.stop_audio()
        self.vcclient.close()
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
