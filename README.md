# PacVoiceMod

PacVoiceMod is a planned Windows 11 voice changer for Twitch streamers. It will capture a physical microphone, apply voice effects locally, and send the result to OBS through a persistent virtual microphone. Between effects, OBS receives the streamer's normal voice.

## Architecture

- **Desktop app.** Use C# and WPF for the audio setup wizard, meters, effect presets, Twitch login, and a global return-to-normal hotkey.
- **Audio host.** Capture the microphone and render to the virtual cable using WASAPI shared mode. Keep the dry path independent of Twitch and the inference process. Use bounded buffers and account for sample-rate differences and clock drift between devices. Keep optional headphone monitoring off by default.
- **RVC worker.** Reuse the real-time RVC pipeline from [w-okada/voice-changer (VCClient)](https://github.com/w-okada/voice-changer) in a supervised local Python process. Pin a known-good revision after the Windows hardware test. Keep one model warm and send PCM over local IPC. Reuse upstream's buffering and crossfades, and discard late audio. Return to dry audio if CUDA is unavailable or the worker fails. Preserve the [MIT notice](https://github.com/w-okada/voice-changer/blob/master/LICENSE) and verify model-weight licenses independently.
- **Helium preset.** Use local pitch and formant shifting. A streamer can use this effect without loading an RVC model.

## Importing models from Voice Models

Support standard RVC v2 inference checkpoints from [voice-models.com](https://voice-models.com/) in the first release. Also test standard RVC v1 exports with the pinned engine. The catalog links to external hosts such as Hugging Face and Google Drive. The first release uses local downloads so the same importer works regardless of host.

- In **Models → Import**, accept a downloaded `.zip` or a standalone `.pth` with an optional matching FAISS `.index`. Look inside nested ZIP folders. If there are multiple plausible checkpoints or indexes, ask the user to choose. Allow conversion without an index and disable index blending in that case.
- Identify the RVC version, sample rate, and pitch configuration from the checkpoint. Check that a supplied index has a compatible feature dimension. Reject training checkpoints, unsupported model families, and malformed inputs with a reason the user can act on.
- Limit archive size and expansion, and reject entries that escape the import directory. Use [restricted PyTorch weight loading](https://docs.pytorch.org/docs/stable/notes/serialization.html#torch-load-with-weights-only-true) with no unrestricted pickle fallback. Treat indexes as untrusted input too. Validate them in a restricted worker before registering a preset.
- Save validated files locally so the preset still works after a restart and without access to the download host. Let the streamer preview the imported voice before assigning a Twitch trigger.
- Make release acceptance depend on importing actual catalog downloads and running live mic-to-OBS conversion. Cover ZIPs with and without an index, standalone weights, nested folders, and clear errors for corrupt or incompatible files. Check the usage terms of test models before using them publicly or redistributing them.

## In-app audio setup

| Step | What the app does |
| --- | --- |
| Choose a microphone | Enumerate physical recording endpoints, show a live meter, and save the selected Windows endpoint ID. |
| Install or detect VB-CABLE | Reuse an installed cable pair. Otherwise, launch the official [VB-CABLE installer](https://vb-audio.com/Cable/index.htm) with Windows elevation and resume setup after reboot. Follow [VB-Audio's distribution terms](https://vb-audio.com/Services/licensing.htm) if bundling it. |
| Verify the audio route | Open **CABLE Input** for playback and briefly read **CABLE Output** to confirm the test signal arrived. Keep Windows default devices as they are. |
| Configure OBS | Connect to [OBS WebSocket](https://github.com/obsproject/obs-websocket), create or reuse a **PacVoiceMod Mic** Audio Input Capture source in the selected scene, and set it to **CABLE Output**. Flag duplicate raw-mic capture. If WebSocket is unavailable, show the exact manual selection. |
| Test and recover | Provide dry and processed previews through headphones, reopen the saved endpoints after restart, and prompt for repair if an endpoint disappears or changes. |

**Signal path** · Physical mic → PacVoiceMod → CABLE Input → CABLE Output → OBS.

## Twitch triggers

Use [Twitch public-client device-code login](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/) and store tokens using Windows credential protection. Subscribe to [`channel.bits.use`](https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/#channelbitsuse) over EventSub WebSocket with `bits:read`. Match Cheer events for the connected broadcaster against an explicit command and a configurable minimum Bits amount. For example, `Cheer100 !helium` activates the helium preset for 20 seconds.

Deduplicate by EventSub message ID and reconnect using [Twitch's WebSocket rules](https://dev.twitch.tv/docs/eventsub/handling-websocket-events/). Twitch can miss deliveries during a disconnect, so expose **Ready**, **Paused**, and **Disconnected** status. Start timing when processed audio reaches the output. Allow one effect at a time, extend matching effects up to a cap, and keep a short queue with expiry for other effects. Include **Test Effect**, **Pause Redeems**, and **Return to Normal** controls.

## Milestones

1. **Prove the audio route.** Run one RVC model with w-okada, VB-CABLE, and OBS on Windows 11 with the GTX 1660 SUPER. Measure mic-to-virtual-mic latency, dropouts, and peak VRAM while streaming.
2. **Build the wizard and dry audio path.** Test driver setup on clean Windows, app and OBS restarts, USB mic reconnection, and duplicate capture. Validate helium through the same route.
3. **Integrate the RVC worker.** Verify catalog imports, model warm-up, clean effect transitions, bounded latency, and dry audio recovery if the worker crashes during speech. Repeat the streaming benchmark with PacTTSClone active.
4. **Integrate Twitch and package.** Test synthetic Cheers, duplicate deliveries, reconnects, and a live Cheer. Validate setup and reboot recovery with the installer.

Latency and VRAM are open until measured on the target hardware.
