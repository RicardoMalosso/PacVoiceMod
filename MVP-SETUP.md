# Run the PacVoiceMod MVP

## Install once

1. Install 64-bit Python 3.12 with Tkinter on Windows 11.
2. Clone this repository and run the following in PowerShell from its root.

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m pip install "torch>=2.6,<3" --index-url https://download.pytorch.org/whl/cpu
   ```

   This PyTorch install is for restricted checkpoint validation. VCClient supplies its own CUDA runtime for conversion.
3. Extract the official [VCClient Windows CUDA build](https://huggingface.co/wok000/vcclient000/blob/1679ae1848c3cfdd11388626b1733bb782065156/vcclient_win_cuda_2.0.65-beta.zip) and start it in HTTP mode. Keep its server listening on `127.0.0.1:18888` and leave it running. PacVoiceMod uses a dedicated model slot, 90 by default, and sends PCM over loopback HTTP. Do not expose the VCClient server to the network.
4. Install [VB-CABLE](https://vb-audio.com/Cable/index.htm) using its administrator setup and reboot. The app's **Get VB-CABLE** button opens the official download page.

## Connect the mic, voice, and chat

Use the checklist on the right to track setup. The buttons at the top switch between Brazilian Portuguese (🇧🇷) and US English (🇨🇦). PacVoiceMod saves your language and checked steps.

1. Launch `.\.venv\Scripts\python.exe -m pacvoicemod` from the repo root. Select your physical microphone and **CABLE Input**.
2. Download a trusted RVC ZIP from [voice-models.com](https://voice-models.com/) and put it in **`PacVoiceMod/models/`** (for example, `PacVoiceMod/models/my-voice.zip`). Leave it zipped. **Import voice** opens that folder, and you can also browse to Downloads or any other folder. Import a ZIP with one model, or pick an extracted `.pth` and optionally its matching `.index`. If the archive has several candidates, extract it yourself and pick the pair. Model imports replace the configured VCClient slot. Checkpoint validation uses restricted loading. Import indexes only from a publisher you trust because VCClient parses them with its native FAISS loader.
3. Click **Start audio**. In OBS, add an **Audio Input Capture** source and select **CABLE Output**. Disable any duplicate raw mic capture in OBS. Confirm dry audio in OBS, then click **Test voice** and speak.
4. Click **Connect Twitch** and complete Device Code login in the browser as your channel owner. Type `!voice` in that channel as the broadcaster or a moderator. The effect defaults to 20 seconds and returns to the dry mic afterward. Use **Return to normal** to end it immediately.
5. On restart, start VCClient, click **Load saved slot**, then **Start audio** and reconnect Twitch. OBS keeps the same source. Twitch credentials stay in memory for the session.

This build uses Python/Tkinter and an external local VCClient process. Bits, Channel Points, a bundled driver installer, and automatic OBS configuration are follow-up milestones. The pinned Windows binary, live Twitch login, and GTX 1660 SUPER latency still require the hardware acceptance test. The dry path currently uses two 120 ms buffer blocks so it can substitute dry audio if the corresponding converted block misses its deadline.

## Record and compare a sample

1. Load a voice and click **Start audio**. Select your headphones under **Sample recorder**.
2. Click **Record sample** and speak. This enables the loaded voice for the test and records both the original mic and the corresponding audio sent to the virtual cable. Click **Stop recording** when done, or let it stop at 10 seconds.
3. Click **Play original** or **Play output** to compare the same take through the selected headphones or speakers. The status shows how much of the take was converted. The output recording includes dry audio whenever conversion fell back to the original mic.

The latest take is kept in memory until you record again or close PacVoiceMod. You can stop live audio and still play it back.

## If voice import says it cannot reach VCClient

Open the extracted VCClient folder and run its HTTP launcher (look for `start_http.bat` if your build includes it). Keep the VCClient console open and wait until it prints its local HTTP address. Check it from PowerShell before importing:

```powershell
Invoke-RestMethod http://127.0.0.1:18888/info
```

If VCClient prints a different HTTP port, use that port in the command and in PacVoiceMod. If the command still fails or VCClient closes immediately, run its launcher from an open PowerShell window and inspect the startup error there. A refused connection happens before the model ZIP is read.

## Local checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
