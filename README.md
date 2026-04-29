# AirTag tracker navigation (web UI)

This repo is a small bundle you can run on your own computer or server: a map in the browser that shows where your AirTag (or other Find My accessory) was last seen, using Apple’s location reports.

If you are new to GitHub: **GitHub is just a website that stores your project folder in the cloud.** “Clone” means download a copy. “Push” means upload your changes. You do not need to be an expert—follow the steps in order.

---

## What you need before you start

1. **Python 3.10 or newer** (3.12 is fine). Check with: `python3 --version`
2. **Your Apple ID** (the same one that owns the AirTag in the Find My app)
3. **A way to get device keys** — today that usually means **a Mac** logged into your Apple ID, with Find My working, so you can export one JSON file per accessory. There is no magic “download device.json from iCloud” button; the file is built from key material on your Mac. If you already have a compatible JSON from another tool, you can use that instead.

---

## The two JSON files (plain English)

### `account.json` — “you proved you’re logged into Apple”

When the app talks to Apple’s servers, Apple expects a **logged-in session**, not just your password typed every time.

- The first time you run the login flow, you type your Apple ID email and password (and 2FA code if asked).
- The program saves the session into **`account.json`** next to the app.
- Next time, it reloads that file so you do not log in again unless the session expires.

Think of it like a **saved login cookie** in a browser, but in a file. **Anyone with this file could impersonate your Apple session.** Do not email it, do not put it in Discord, and do not upload it to GitHub.

### `device.json` — “which physical tag are we asking about?”

Your AirTag does not send its location straight to this app. Other people’s iPhones pass anonymous encrypted hints to Apple. To ask Apple “decode the hints meant for *my* tag,” the app needs the **cryptographic keys** that were created when the tag was paired. Those keys are what get exported into **`device.json`** (one accessory per file).

If this file is wrong or incomplete, you will get no location—or the wrong tag.

---

## Part A — Get this repo onto your machine

On GitHub, green **Code** button → copy the HTTPS link (looks like `https://github.com/Lithiumwow/Airtag-tracker-navigation.git`).

In a terminal:

```bash
cd ~/Downloads   # or wherever you keep projects
git clone https://github.com/Lithiumwow/Airtag-tracker-navigation.git
cd Airtag-tracker-navigation
```

You now have this folder on disk. That is all “cloning” means.

---

## Part B — Install the Python package (one time)

```bash
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e .
```

`pip install -e .` reads `pyproject.toml` and installs the **FindMy** library from the included `findmy/` source tree, plus dependencies like `aiohttp`. The example web app imports that library.

---

## Part C — Get `device.json`

**Typical path (Mac + Terminal):**

1. On the Mac where your AirTag works in the Find My app, open Terminal.
2. Go to your cloned repo folder (same place you ran `pip install -e .`).
3. Run:

   ```bash
   source .venv/bin/activate
   python3 -m findmy decrypt --out-dir airtag-export
   ```

4. You may get a password prompt for your **login keychain** — that is normal; the keys live there.
5. Inside `airtag-export/` you should see one `.json` file per accessory. Pick the one that matches your AirTag (open it in a text editor and check the name fields if needed).
6. Copy that file to the **root** of the repo and name it **`device.json`**:

   ```bash
   cp airtag-export/Something.json ./device.json
   ```

**macOS 15+:** Apple changed how some keys are stored. If decrypt fails, read the upstream docs for “BeaconStore” / plist helpers — see **Credits** below for the main FindMy.py documentation.

**No Mac?** You still need key material from a Mac at least once, or a JSON someone generated for you on compatible hardware. This is an Apple limitation, not something this repo can bypass.

---

## Part D — Get `account.json` (first login)

The web example logs in using the helper in `examples/_login.py`.

1. Put **`device.json`** in the repo root (see Part C).
2. Start the app once (next section). **If `account.json` does not exist**, the program will ask in the terminal for:
   - email  
   - password  
   - which 2FA method  
   - the code  

3. After a successful login, **`account.json`** appears in the repo root. Keep it secret.

If you already have a valid `account.json` from another machine, you can copy it here instead of logging in again—same warning: treat it like a password.

---

## Part E — Run the web app

Easiest:

```bash
source .venv/bin/activate
chmod +x RUN_WEB_TRACKER.sh
./RUN_WEB_TRACKER.sh
```

Or manually:

```bash
source .venv/bin/activate
python examples/web_tracker_app.py --airtag-json ./device.json --host 0.0.0.0 --port 8008
```

Then open a browser:

- On the same machine: **http://localhost:8008**
- From a phone on the same Wi‑Fi: **http://YOUR_COMPUTER_IP:8008**

There is **no login screen on the website** — anyone who can reach that URL can see the map. Do not expose port 8008 to the whole internet without a VPN, SSH tunnel, or reverse proxy with authentication.

Default refresh is periodic; first location can take a while if the tag was quiet.

---

## Change the app name (browser tab title)

The name you see on the browser tab comes from the HTML `<title>` tag inside `examples/web_tracker_app.py`.

Search for:

```html
<title>FindMy Web Tracker - Live Location</title>
```

Change the text between `<title>` and `</title>` to whatever you want, save the file, and restart the Python process.

Optional: the shell script `RUN_WEB_TRACKER.sh` prints “FindMy Web Tracker” in a few `echo` lines—you can edit those messages too so your terminal output matches your project name.

---

## Optional: run in the background with systemd (Linux)

See `scripts/findmy-webtracker.service`. Edit paths inside the unit file to match where you cloned the repo and which user should run the service, then install it the usual systemd way (`/etc/systemd/system/`, `daemon-reload`, `enable`, `start`). Exact commands depend on your distro—look up “systemd service tutorial” if you have never done this.

---

## What not to upload to GitHub

Your repo should contain **code only**. Never commit:

- `account.json`
- `device.json`
- `ani_libs.bin`
- `location_history.json` / caches

This repo’s `.gitignore` already tries to block those. Before `git add`, skim `git status` and make sure no secret files appear.

---

## Troubleshooting (short)

| Problem | Things to check |
|--------|------------------|
| “device.json not found” | File must sit next to `RUN_WEB_TRACKER.sh`, named exactly `device.json`. |
| No location | Tag offline, nobody with an iPhone walked nearby, or Apple has not processed reports yet—wait and retry. |
| Login errors | Wrong password, extra Apple security step, or session expired—delete `account.json` and log in again (know the risks). |
| Port busy | Change the port in the command line (`--port 8010`) or stop the old Python process. |

For deeper detail, the upstream project docs are listed below.

---

## Credits

This bundle is built around **[FindMy.py](https://github.com/malmeloo/FindMy.py)** by Mike Almeloo and contributors (MIT License). The library stands on work from many people; the upstream project credits include:

- **seemoo-lab** — [OpenHaystack](https://github.com/seemoo-lab/openhaystack/) and related research  
- **JJTech0130** — [pypush](https://github.com/JJTech0130/pypush)  
- **biemster** — [FindMy](https://github.com/biemster/FindMy)  
- **Dadoum** — [pyprovision](https://github.com/Dadoum/pyprovision), [anisette](https://github.com/Dadoum/anisette-v3-server)  
- **nythepegasus** — [GrandSlam](https://github.com/nythepegasus/grandslam) (SMS 2FA)  

The **web UI example** (`examples/web_tracker_app.py`) adds:

- **[Leaflet](https://leafletjs.com/)** — map interaction (via unpkg CDN)  
- **Map tiles** — [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors; dark tiles **© [CARTO](https://carto.com/attributions)**  
- **Routing / timeline snapping** — public **[OSRM](https://project-osrm.org/)** demo server (`router.project-osrm.org`) — good for demos, not a guaranteed production SLA  
- **Font** — [Chakra Petch](https://fonts.google.com/specimen/Chakra+Petch) via Google Fonts  

If you redistribute, keep license files and credit notices where required.

---

## License

The FindMy library and examples follow the terms in `LICENSE.md` (MIT). Your own modifications should stay compliant if you share them.

---

## Repository

Upstream library and docs: [FindMy.py documentation](http://docs.mikealmel.ooo/FindMy.py/)

This fork / deployment repo: **https://github.com/Lithiumwow/Airtag-tracker-navigation**
#   A i r t a g - t r a c k e r - n a v i g a t i o n .  
 