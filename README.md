# PacVoiceMod

PacVoiceMod is a planned Windows 11 voice changer for Twitch streamers. It will capture a physical microphone, apply voice effects locally, and send the result to OBS through a persistent virtual microphone. Between effects, OBS receives the streamer's normal voice.

## MVP

The first implementation is available as a Windows source build. See [MVP setup](MVP-SETUP.md) for prerequisites and the mic-to-OBS test. This build uses Python/Tkinter and connects to VCClient as a separate loopback process.

The first Windows build proves one complete path. Import one voice from [voice-models.com](https://voice-models.com/), route the physical mic through PacVoiceMod to OBS, and use a Twitch chat command to turn that voice on for a set duration.

**Engine.** Start with the original [w-okada/voice-changer (VCClient)](https://github.com/w-okada/voice-changer), a widely used MIT-licensed real-time RVC implementation with roughly 21,000 GitHub stars. Smoke-test its official Windows CUDA build in local device mode on the GTX 1660 SUPER with a catalog download. Pin the revision and build that pass. Reuse its streaming conversion in the app's local worker.

| Step | MVP behavior |
| --- | --- |
| Import one voice | Select a downloaded RVC v2 ZIP or an extracted `.pth` plus optional `.index`. Validate it, save it locally, and warm it before enabling the chat trigger. |
| Set up audio | Select the physical mic, detect VB-CABLE, and open the official installer if needed. Show input and output meters. PacVoiceMod writes to **CABLE Input**. In OBS, add **Audio Input Capture** and select **CABLE Output** once. |
| Connect Twitch | Sign in as the broadcaster using public-client Device Code login. Ship Client ID `sorb998iik1sbba6xfbqfr6e7pn9s1`. The registered `http://localhost` URL is unused by Device Code login. Request only `user:read:chat` and subscribe to `channel.chat.message` over EventSub WebSocket. |
| Trigger the voice | Default to exact command `!voice` for 20 seconds, broadcaster and moderators only, with both settings configurable. Start the timer when converted audio reaches the output. Ignore repeat triggers while active in the MVP. A local test button works without Twitch. |
| Return to normal | Send the dry mic through the same OBS source before and after the effect. Let the streamer cancel immediately. Return to dry audio if inference fails and keep the dry route working if Twitch disconnects. |

**Acceptance test.** On Windows 11, import a real catalog download, speak into OBS with the dry mic, type the command in live Twitch chat, hear the converted voice in OBS, and verify automatic return to dry audio. Restart PacVoiceMod and OBS and repeat without reimporting the model or recreating the OBS source. Measure mic-to-OBS latency, dropouts, and peak VRAM while OBS is streaming.

Bits, Channel Points, multiple presets, and automatic OBS source creation follow after this end-to-end test. The sections below describe the broader plan.

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

In **Twitch → Triggers**, let the streamer add any number of enabled rules. Each rule selects one imported RVC voice model or DSP preset and a duration in seconds. The same voice can have separate rules and durations for each trigger type.

| Trigger | Example | Matching |
| --- | --- | --- |
| Bits Cheer | `Cheer100 !robot` enables Robot for 30 seconds | Match a configured minimum Bits amount and a selector. Allow an amount-only rule when the streamer wants any qualifying Cheer to activate a default voice. An explicit selector takes priority over the default. |
| Chat command | `!robot` enables Robot for 10 seconds | Match an exact configured command, with optional viewer-role restrictions and per-user and channel cooldowns. |
| Channel Points | Redeem **Robot Voice** to enable Robot for 20 seconds | Select an existing custom reward from the connected channel and match its reward ID so renaming it preserves the rule. Twitch controls the points cost. |

Use [Twitch public-client device-code login](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/) and Windows credential protection. Subscribe over EventSub WebSocket using the broadcaster's user token. Use [`channel.bits.use`, `channel.chat.message`, and `channel.channel_points_custom_reward_redemption.add`](https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/) with `bits:read`, `user:read:chat`, and `channel:read:redemptions`, respectively. Request scopes for enabled trigger types. Handle Cheers only through the Bits rule path so a Cheer that contains a chat command cannot activate twice. Match each event to at most one rule, reject ambiguous rule configurations, and deduplicate EventSub deliveries and redemptions.

Send all three trigger types through the same effect controller. Start the timer when processed audio reaches the output, then return to the normal mic when it expires. Allow one active effect at a time. Repeated activations of the same voice can extend it up to a cap. Other effects wait in a bounded queue with expiry. Show the current voice, time remaining, and skipped or expired activations. Provide **Test Effect**, **Pause Triggers**, and **Return to Normal**. Return to Normal immediately clears the queue and bypasses the effect.

Show **Ready**, **Paused**, or **Disconnected** and reconnect using [Twitch's WebSocket rules](https://dev.twitch.tv/docs/eventsub/handling-websocket-events/). Twitch can miss deliveries during a disconnect. The Channel Points milestone listens to existing custom rewards. Streamers manage fulfillment and refunds in Twitch.

## Milestones

1. **Prove the audio route.** Run one RVC model with w-okada, VB-CABLE, and OBS on Windows 11 with the GTX 1660 SUPER. Measure mic-to-virtual-mic latency, dropouts, and peak VRAM while streaming.
2. **Build the wizard and dry audio path.** Test driver setup on clean Windows, app and OBS restarts, USB mic reconnection, and duplicate capture. Validate helium through the same route.
3. **Integrate the RVC worker.** Verify catalog imports, model warm-up, clean effect transitions, bounded latency, and dry audio recovery if the worker crashes during speech. Repeat the streaming benchmark with PacTTSClone active.
4. **Integrate Twitch and package.** Test all three trigger types with synthetic and live events. Cover duplicate deliveries, a Cheer containing a chat command, renamed rewards, cooldowns, reconnects, and return to dry audio when the timer expires. Validate setup and reboot recovery with the installer.

Latency and VRAM are open until measured on the target hardware.
