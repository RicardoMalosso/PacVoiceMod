"""Twitch companion for the official w-okada desktop UI."""

import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
import tkinter as tk
from tkinter import messagebox, ttk

from .obs import run_effect
from .twitch import BITS, POINTS, is_command, listen
from .vcclient import BUILD, start_desktop


VC_INSTALL = Path(__file__).resolve().parents[1] / "runtime" / BUILD
SETTINGS = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PacVoiceMod" / "companion.json"


def paid_trigger_matches(kind, event, minimum_bits, reward):
    if kind == BITS:
        return minimum_bits > 0 and event.get("type") == "cheer" and event["bits"] >= minimum_bits
    if kind == POINTS:
        reward_info = event["reward"]
        return bool(reward) and reward.casefold() in {
            reward_info["id"].casefold(), reward_info["title"].casefold(),
        }
    return False


class Companion:
    def __init__(self, root):
        self.root = root
        self.events = SimpleQueue()
        self.twitch_stop = None
        self.effect_stop = None
        self.closing = False
        self.desktop_stop = Event()
        self.desktop_process = None
        self.desktop_starting = False
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        root.title("PacVoiceMod · Twitch for w-okada")
        root.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(root, padding=14)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(1, weight=1)
        self.fields = {}
        for row, (key, label, default) in enumerate([
            ("dry", "Original mic in OBS", "Mic/Aux"),
            ("wet", "w-okada input in OBS", "w-okada"),
            ("port", "OBS WebSocket port", "4455"),
            ("password", "OBS WebSocket password", ""),
            ("command", "Twitch command", "!voice"),
            ("duration", "Effect duration (seconds)", "20"),
            ("bits", "Minimum Bits (0 disables)", "0"),
            ("reward", "Points reward title or ID (optional)", ""),
        ]):
            variable = tk.StringVar(value=default if key == "password" else saved.get(key, default))
            self.fields[key] = variable
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            entry = ttk.Entry(panel, textvariable=variable, width=32)
            if key == "password":
                entry.configure(show="•")
            entry.grid(row=row, column=1, sticky="ew")
        self.restricted = tk.BooleanVar(value=saved.get("restricted", True))
        ttk.Checkbutton(panel, text="Broadcaster and moderators only",
                        variable=self.restricted).grid(row=8, columnspan=2, sticky="w")
        buttons = ttk.Frame(panel)
        buttons.grid(row=9, columnspan=2, sticky="w", pady=8)
        for index, (label, command) in enumerate([
            ("Connect Twitch", self.connect_twitch), ("Disconnect Twitch", self.disconnect_twitch),
            ("Test effect", self.trigger), ("Return to normal", self.cancel),
            ("Open w-okada", self.open_desktop),
        ]):
            ttk.Button(buttons, text=label, command=command).grid(
                row=index // 2, column=index % 2, sticky="ew", padx=2, pady=2)
        self.twitch_status = tk.StringVar(value="Twitch disconnected")
        self.effect_status = tk.StringVar(value="Ready for an OBS test")
        self.desktop_status = tk.StringVar()
        for row, variable in enumerate((self.desktop_status, self.twitch_status, self.effect_status), 10):
            ttk.Label(panel, textvariable=variable, wraplength=490).grid(
                row=row, columnspan=2, sticky="w")
        root.after(100, self.poll)
        root.after_idle(self.open_desktop)

    def open_desktop(self):
        if self.desktop_starting or self.closing:
            return
        if self.desktop_process and self.desktop_process.poll() is None:
            self.desktop_status.set("w-okada is already running.")
            return
        self.desktop_starting = True
        self.desktop_status.set("Looking for PacVoiceMod's w-okada installation…")

        def status(message, completed=0, total=0):
            if total:
                message = f"{message} {min(100, completed * 100 // total)}%"
            elif completed:
                message = f"{message} {completed // 1024**2} MiB"
            self.events.put(("desktop_status", None, message))

        def run():
            try:
                process = start_desktop(VC_INSTALL, SETTINGS.with_name("vcclient.log"),
                                        self.desktop_stop, status)
            except Exception as exc:
                self.events.put(("desktop_done", None, f"Could not open w-okada: {exc}"))
            else:
                self.events.put(("desktop_done", process, "w-okada launcher started. Set your voice and start conversion there."))

        Thread(target=run, daemon=True).start()

    def read_settings(self):
        settings = {key: field.get().strip() for key, field in self.fields.items()}
        settings["password"] = self.fields["password"].get()
        settings["restricted"] = self.restricted.get()
        return settings

    def save(self, settings):
        settings = {key: value for key, value in settings.items() if key != "password"}
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings), encoding="utf-8")
        temporary.replace(SETTINGS)

    def trigger(self):
        if self.effect_stop or self.closing:
            return
        settings = self.read_settings()
        try:
            port = int(settings["port"])
            seconds = int(settings["duration"])
            dry, wet = settings["dry"], settings["wet"]
            if not (1 <= port <= 65535 and 1 <= seconds <= 300 and dry and wet and dry != wet):
                raise ValueError()
        except ValueError:
            self.effect_status.set("Enter two different OBS inputs, a valid port, and a duration of 1–300 seconds.")
            return
        stop = self.effect_stop = Event()
        self.effect_status.set("Switching OBS audio…")

        def run():
            try:
                run_effect(port, settings["password"], dry, wet, seconds, stop,
                           lambda msg: self.events.put(("effect", stop, msg)))
            except Exception as exc:
                self.events.put(("effect_done", stop, str(exc)))
            else:
                self.events.put(("effect_done", stop, "Original mic restored"))
        Thread(target=run, daemon=True).start()

    def cancel(self):
        if self.effect_stop:
            self.effect_stop.set()

    def connect_twitch(self):
        if self.twitch_stop or self.closing:
            return
        settings = self.read_settings()
        if not settings["command"].startswith("!") or len(settings["command"].split()) != 1:
            messagebox.showerror("Twitch", "Enter a command such as !voice.")
            return
        try:
            minimum_bits = int(settings["bits"])
            if minimum_bits < 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("Twitch", "Enter 0 or a positive minimum Bits amount.")
            return
        reward = settings["reward"]
        stop = self.twitch_stop = Event()
        self.twitch_status.set("Connecting to Twitch…")

        def run():
            try:
                listen(stop,
                       lambda msg, **values: self.events.put(("twitch", stop, msg.format(**values))),
                       lambda event, user: self.events.put(("chat", stop, (event, user))),
                       extra_trigger=lambda kind, event: self.events.put(
                           ("paid", stop, (kind, event, minimum_bits, reward))),
                       bits=minimum_bits > 0, points=bool(reward))
            except Exception as exc:
                self.events.put(("twitch", stop, f"Twitch error: {type(exc).__name__}"))
            finally:
                self.events.put(("twitch_done", stop, None))
        Thread(target=run, daemon=True).start()

    def disconnect_twitch(self):
        if self.twitch_stop:
            self.twitch_stop.set()
            self.twitch_stop = None
        self.twitch_status.set("Twitch disconnected")

    def poll(self):
        for _ in range(100):
            try:
                kind, operation, value = self.events.get_nowait()
            except Empty:
                break
            if kind == "desktop_status":
                self.desktop_status.set(value)
            elif kind == "desktop_done":
                self.desktop_starting = False
                self.desktop_process = operation
                self.desktop_status.set(value)
            elif operation is self.effect_stop:
                self.effect_status.set(value)
                if kind == "effect_done":
                    self.effect_stop = None
            elif operation is self.twitch_stop:
                if kind == "twitch":
                    self.twitch_status.set(value)
                elif kind == "twitch_done":
                    self.twitch_stop = None
                elif kind == "paid" and paid_trigger_matches(*value):
                    self.trigger()
                elif kind == "chat":
                    event, user = value
                    settings = self.read_settings()
                    if is_command(event, user, settings["command"], settings["restricted"]):
                        self.trigger()
        if self.desktop_process and self.desktop_process.poll() not in (None, 0):
            self.desktop_process = None
            self.desktop_status.set("w-okada exited. See %LOCALAPPDATA%\\PacVoiceMod\\vcclient.log.")
        if self.closing and self.effect_stop is None:
            self.root.destroy()
        else:
            self.root.after(100, self.poll)

    def close(self):
        if self.closing:
            return
        try:
            self.save(self.read_settings())
        except OSError as exc:
            messagebox.showerror("Settings", str(exc))
        self.closing = True
        self.desktop_stop.set()
        self.disconnect_twitch()
        self.cancel()


def main():
    root = tk.Tk()
    Companion(root)
    root.mainloop()


if __name__ == "__main__":
    main()
