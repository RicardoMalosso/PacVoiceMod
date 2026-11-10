# Add Twitch triggers to w-okada

Keep using the official w-okada interface to import a voice, choose its pitch and quality, and start conversion. PacVoiceMod Companion controls which mic OBS broadcasts. It uses OBS WebSocket, so it works independently of w-okada's private API.

## Set up w-okada and OBS once

1. In w-okada, select your physical mic as input and **CABLE Input** as output. Start conversion and leave it running. The selected voice is the one Twitch will activate. Keep headphone monitoring off if you hear a duplicate.
2. In OBS, use two separate audio inputs in the scene. One captures the physical microphone and the other captures **CABLE Output**. Name the converted input **w-okada**, or enter the exact name you chose in the companion. Keep the original mic unmuted and the converted input muted before testing.
3. In OBS, open **Tools → WebSocket Server Settings**. Enable the server and note its port and password. The default port is **4455**.
4. Run the companion from the repo root. If the existing PacVoiceMod environment is already installed, reuse it.

   ```powershell
   .\.venv\Scripts\python.exe -m pacvoicemod.companion
   ```

   For a fresh checkout, create the environment first:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

5. Enter the two OBS source names and the WebSocket password. Click **Test effect**, speak, and check that OBS switches to the converted source and then returns to the original mic. **Return to normal** cancels the effect early.

## Connect Twitch

- **Chat:** Click **Connect Twitch**, authorize with your broadcaster account, and type `!voice` in your chat. The default duration is 20 seconds. By default, only you and your moderators can use the command.
- **Bits:** Set **Minimum Bits** before connecting. For example, set it to 100 to activate the voice on Cheers of at least 100 Bits.
- **Channel Points:** Enter the title or ID of an existing custom reward before connecting. Use the ID if you want the rule to survive a rename. OBS switches when the reward is redeemed.

Reconnect Twitch after changing the Bits or reward settings. All three trigger types use the voice currently selected in w-okada and the same configured duration. Additional triggers during an active effect are ignored. The companion keeps the OBS password in memory and saves the other settings when it closes.

The companion leaves w-okada running and switches OBS inputs. Check that OBS receives a converted signal before enabling paid triggers. If OBS is unavailable when an effect ends, the app retries restoration. If retries fail, mute the converted input and unmute the original mic in OBS.

## Implementation decisions

- Keep w-okada's UI, model manager, CUDA engine, and audio settings.
- Keep PacVoiceMod's existing Twitch authorization and EventSub code. Add optional Bits and Channel Points subscriptions.
- Use OBS WebSocket to select which input is heard. Preserve the existing experimental PacVoiceMod recorder in its separate entry point.
- Verify the OBS WebSocket flow with an authenticated loopback test server. Live Twitch authorization, a real Cheer or redemption, and the VB-CABLE route still need a Windows streaming test.
