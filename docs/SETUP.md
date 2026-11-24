# Setting up PLC Command Center on your own Mac mini

PLC Command Center watches your PLCs (pumps, tanks, sensors…), keeps charts of the readings, and
texts you when something goes wrong, by **iMessage** (a group chat) and/or **Telegram**. It runs
quietly in the background on a Mac mini, and you check on it from any phone or computer on your network.

This guide assumes no programming experience. Plan on about 30 minutes. Whenever it says
**Terminal**, open the *Terminal* app (Applications → Utilities → Terminal), paste the line shown,
and press Return.

> It only ever **reads** from your PLCs. It never writes values or changes anything in them.

---

## 1. What you need

- A **Mac mini** (any recent one; Apple silicon or Intel) running **macOS 13 Ventura or newer**,
  left on all the time and connected to the same network as your PLCs.
- An **Apple ID** signed in to the Messages app on that Mac (for iMessage alerts).
- Optional: a **Telegram** account (for Telegram alerts).
- Apple's free **Command Line Tools**, which include the Python the app uses. In Terminal:

  ```
  xcode-select --install
  ```

  Click **Install** in the window that pops up and wait for it to finish. (If it says they're already
  installed, you're good.) Check it worked: `python3 --version` should print `Python 3.9` or newer.

Keep the Mac awake and logged in, because the app runs in your user account:

- **System Settings → Energy** (or *Energy Saver*): turn on **Prevent automatic sleeping…** and
  **Start up automatically after a power failure**.
- **System Settings → Users & Groups → Automatically log in as**: pick your user. After a power cut, the
  Mac comes back by itself and the alarms start again.

## 2. Get the project onto the Mac

Put the project folder in your home folder and name it **`PLCCommandCenter`**. Either:

- **Download it with git** (if you were given a link):

  ```
  git clone <the link you were given> ~/PLCCommandCenter
  ```

- **Or unzip** the `PLCCommandCenter.zip` you were given, and drag the folder into your home folder
  (the one with the house icon in Finder).

> **For whoever shares the project:** send a clean copy, not your live folder. From your own project
> folder, run `git archive --format=zip -o ~/Desktop/PLCCommandCenter.zip HEAD`. That zip never
> includes your `.env` (passwords and tokens), your database, your devices, your dashboard layout,
> or your logs.

## 3. Run the installer

In Terminal:

```
cd ~/PLCCommandCenter
scripts/install.sh
```

The installer:

1. sets up its own private Python environment and downloads what it needs (a minute or two),
2. **asks you to choose a dashboard password**. Type it twice. Nothing shows on screen while you
   type; that's normal. You need this password to change settings on the dashboard.
3. runs its self-tests, which never send any real messages,
4. creates an empty database, starts two background services (the *poller*, which talks to the PLCs
   and sends alarms, and the *web* dashboard), and checks that both are running.

When it finishes it prints **`v2 is live`** and the dashboard address. The two services start again
by themselves whenever the Mac restarts. (They're named `com.plccommandcenter.poller` and
`com.plccommandcenter.web`. The names don't matter, so leave them as they are.)

## 4. Open the dashboard

On the Mac, open Safari and go to **http://localhost:8085**.

From a phone or another computer on the same Wi-Fi, use the Mac's name or IP address, e.g.
**http://mac-mini.local:8085** or **http://192.168.1.50:8085**. You'll find both in
**System Settings → General → Sharing** (the "Local hostname") or **System Settings → Network → Details**.

To change anything, tap the **lock** icon (or Settings → **Unlock**) and enter your dashboard password.
It's remembered only until you close that browser tab.

### Changing the dashboard password later

```
open -e ~/PLCCommandCenter/.env
```

Change the line `DASHBOARD_PASSWORD='…'`, save, close TextEdit, then restart the dashboard:

```
launchctl kickstart -k gui/$(id -u)/com.plccommandcenter.web
```

## 5. iMessage alarms

### 5a. Messages must be signed in

Open the **Messages** app on the Mac mini and sign in with an Apple ID (**Messages → Settings →
iMessage**). It's best to use an Apple ID that is *not* one of the people receiving the alarms, so
you can tell the alarms apart. A spare Apple ID just for the Mac works well.

### 5b. Make the alarm group chat

1. In Messages (on the Mac or an iPhone signed in to the same Apple ID), start a **new group
   conversation** with everyone who should get alarms. A group needs at least two other people
   besides the Mac's Apple ID.
2. Send a first message, then **name the group**: in the conversation tap/click the group icon at the
   top, then **Change Name** (iPhone) or the **ⓘ** button → *Group name* (Mac).
   The default name the app uses is **`PLC Alarms`**.
3. Make sure the group shows up in **Messages on the Mac mini**.
4. On the dashboard: **Settings → iMessage alarm group**. If you used a different name, type exactly
   the same name and tap **Save**. Tap **Check**: it should say **✓ Group found in Messages**.
5. Tap **Send test message**. The first time, macOS asks **"Python" wants access to control
   "Messages"**: click **Allow**. The test message should appear in the group.

Missed that pop-up? Go to **System Settings → Privacy & Security → Automation → Python** and turn on
**Messages**.

### 5c. Full Disk Access (so replies can acknowledge an alarm)

When an alarm escalates, anyone can **reply in the group** (for example "ack" or "got it") to silence
further reminders. To see those replies the app needs to read the Messages history, and macOS only
allows that with **Full Disk Access**:

1. **System Settings → Privacy & Security → Full Disk Access**.
2. Click **+** (enter your Mac password), then press **⌘ Command + Shift + G** and paste this path:

   ```
   /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app
   ```

   Select **Python.app** and click **Open**, then make sure its switch is **on**.
   (The installer prints this exact path too. If your Python version is different, use the path it
   printed.)
3. Restart the poller so it picks up the permission:

   ```
   launchctl kickstart -k gui/$(id -u)/com.plccommandcenter.poller
   ```

You can also acknowledge an alarm with the **Acknowledge** button on the dashboard's Overview page.

## 6. Telegram alarms (optional, recommended)

Telegram is set up entirely on the dashboard. There are no files to edit.

1. In Telegram, open a chat with **@BotFather** (the official one, with the blue check mark), send
   **`/newbot`**, and answer its two questions (a display name, then a username ending in `bot`).
   BotFather replies with a **token** that looks like `123456789:AAH…`. Copy it, and keep it private.
2. On the dashboard: **Settings → Telegram alerts**. Unlock, paste the token into **Bot token**, and
   tap **Save token**.
3. In Telegram, make a **group** for alarms (or use one you already have), **add your new bot** to it,
   and **send any message** in the group (e.g. "hello"). To get alarms privately instead, open a chat
   with your bot and tap **Start**.
4. Back on the dashboard, tap **Find my chat**. Your group appears in the list. Tap **Add**. Add as
   many groups or people as you like; each one gets every alarm. Use **Remove** to stop one.
   - *"No messages yet"*: send another message in the group, wait a few seconds, and tap Find again.
   - *"webhook"*: this bot is already used by another program. Make a new bot just for this app.
   - Know the chat ID already? Type it in the box (e.g. `-1001234567890`) and tap **Add**.
5. Tap **Send test**. The test message should arrive within a few seconds.

The **switch** at the top of the card turns Telegram alarms on and off without losing the setup.
Changes take effect immediately; nothing needs restarting. Telegram only *sends* alarms. To
acknowledge an alarm, reply in the iMessage group or use the dashboard.

## 7. Add your PLCs

Go to the **Devices** tab, unlock, and tap **+ Device**. Pick the type, give it a name, and enter its
address. Then open the device and tap **+ Tag** for each value you want to watch (name, address, units,
and optional scaling). Within a minute the device shows **online** and values appear on the Overview.

| Type | Host | Port (blank = default) | Tag address examples |
|---|---|---|---|
| **Allen-Bradley SLC / MicroLogix** | PLC IP, e.g. `192.168.1.10` | 44818 | `N7:6`, `F8:2`, `B3:0` |
| **Allen-Bradley ControlLogix / CompactLogix** | PLC IP | 44818 | tag names: `PumpFlow`, `Tank[0]` |
| **Siemens S7** (1200/1500/300) | PLC IP; Rack `0`, Slot `1` (S7-300: slot 2) | 102 | `DB6.REAL108`, `DB1.INT4`, `DB1.DBX0.0` |
| **Modbus TCP** | device IP; *Unit ID* (usually 1) | 502 | `HR4800`, `IR200`, `CO5`, `DI3` (1-based) |
| **OPC UA** | `opc.tcp://192.168.1.50:4840` (+ username/password and security if the server needs it) | 4840 | NodeIds: `ns=2;s=Pump.GPM`, `ns=3;i=1002` |
| **MQTT** | broker IP or name (+ username/password, TLS if needed) | 1883 (8883 with TLS) | a topic, e.g. `farm/tank/level`, plus an optional JSON path like `data.level` |

Tips:

- **Siemens S7-1200/1500:** in TIA Portal, allow **PUT/GET** access (CPU → Protection & Security) and turn
  off **Optimized block access** on the data blocks you read.
- **Modbus:** if 32-bit numbers look scrambled, toggle **Word swap** on the device.
- **MQTT:** a device shows offline if no message arrives for *Offline if no message for (s)*
  (default 300 s).
- **Alarms:** in a tag, turn on **Alarm**, then set a **Low alarm (below)** and/or **High alarm (above)**
  value. You get a message when the value crosses the limit and a "CLEAR" when it comes back. If a
  device stops answering for 3 polls, you get a **COMM LOSS** message. **Settings → Alarm switches**
  lets you silence one tag quickly.
- **Overview:** tap **Customize** to choose which tags show, as value boxes or charts.

## 8. Put the icon on your iPhone home screen

On the iPhone, open the dashboard address in **Safari**, tap the **Share** button (square with an arrow),
then **Add to Home Screen** → **Add**. It opens full-screen like an app. (On Android/Chrome: menu → *Add
to Home screen*.)

## 9. Checking on it and fixing things

| Problem | What to do |
|---|---|
| Dashboard won't load | `launchctl kickstart -k gui/$(id -u)/com.plccommandcenter.web` |
| No values / device offline | Check the IP and port; make sure the Mac can reach the PLC (`ping 192.168.1.10`). The device card shows the last error. |
| iMessage test fails with "Not authorized" | Section 5b, Automation permission. |
| Replies don't acknowledge alarms | Section 5c, Full Disk Access, then restart the poller. |
| Telegram test fails | Re-check the token (Save token, then Find my chat); make sure the bot is still in the group. |
| After a Mac restart nothing runs | Turn on automatic login (section 1). |

Logs live in `~/PLCCommandCenter/logs/` (`plccc_poller.log` is the most useful). To restart the alarm
service: `launchctl kickstart -k gui/$(id -u)/com.plccommandcenter.poller`.

**Updating** to a newer version: replace the project files (or `git pull`), then run
`scripts/install.sh` again. Your `.env`, database, devices and settings are kept.

**Removing it:** `scripts/uninstall.sh` stops and removes the background services (your data stays in
the folder; delete the folder to remove everything).

**Keep private:** the `.env` file (your password) and the `data/` folder (your readings, devices and
Telegram token). Never send those to anyone.

**Away from home:** the dashboard is meant for your local network. To reach it from outside, use a VPN
such as Tailscale rather than opening port 8085 on your router.
