# AirTag tracker (map in your browser)

This project runs a small **web server** on your computer. You open a page in the browser and see a **map** with where your AirTag (or another Find My accessory) was last reported—using Apple’s systems and this codebase, not by “hacking” anyone’s phone.

---

## New to GitHub?

GitHub stores a **copy** of a project online. You **clone** that copy to your PC (download it with git). When you change files and want to save them to GitHub, you **commit** and **push**. You don’t need to memorize everything—just follow the steps in order, same as installing a game: one window, then the next.

**This repo:** [https://github.com/Lithiumwow/Airtag-tracker-navigation.](https://github.com/Lithiumwow/Airtag-tracker-navigation.)  
Clone URL (HTTPS): `https://github.com/Lithiumwow/Airtag-tracker-navigation..git` — the repo name ends with a dot, so the URL has **two** dots before `.git`.

---

## What you need first

| Thing | Why |
|--------|-----|
| **Python 3.10+** | Runs the app. Check: `python3 --version` (Mac/Linux) or `py -3 --version` (Windows). |
| **Your Apple ID** | Same account that owns the AirTag in the Find My app. |
| **A Mac (usually)** | Apple doesn’t hand you `device.json` from a website. The keys for your tag live in the Apple ecosystem; exporting them is normally done on a Mac where Find My already works. No Mac means you need someone else’s export or another tool that produced a compatible file—there’s no generic “download from iCloud” button. |

---

## The two small files that aren’t “code”

Everything in `findmy/` and `examples/` is **public** code you can share. Two files are **private** and stay on your machine only:

### `device.json` — “which tag is this?”

Your tag doesn’t phone this program directly. Random iPhones nearby send **blips** to Apple; Apple needs to know **which** blips belong to **your** tag. For that, the program uses **keys** created when the tag was paired. Those keys end up in a JSON file—here we call it **`device.json`** and keep it in the **project root** (same folder as `README.md`).

Wrong file = wrong tag or no location.

### `account.json` — “Apple trusts this login”

Talking to Apple’s servers needs a **session** (like staying logged in), not typing your password every time. The first time you go through login in the app, it saves that session as **`account.json`**. Next runs reuse it until Apple expires it.

**Treat both files like passwords.** If someone gets them, they can mess with your Apple account session or impersonate your setup. Don’t put them in Discord, email, or GitHub.

---

## Step 1 — Download the project

On the repo page, green **Code** → copy the HTTPS link.

**Mac or Linux (Terminal):**

```bash
cd ~/Downloads   # or any folder you like
git clone https://github.com/Lithiumwow/Airtag-tracker-navigation..git
cd "Airtag-tracker-navigation."
```

**Windows — folder `D:\Github Projects\airtag`**

Command Prompt:

```cmd
mkdir "D:\Github Projects" 2>nul
cd /d "D:\Github Projects"
git clone https://github.com/Lithiumwow/Airtag-tracker-navigation..git airtag
cd airtag
```

PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path "D:\Github Projects" | Out-Null
Set-Location "D:\Github Projects"
git clone https://github.com/Lithiumwow/Airtag-tracker-navigation..git airtag
Set-Location airtag
```

Already have the folder? Open a terminal there:

```cmd
cd /d "D:\Github Projects\airtag"
```

**Cursor / VS Code:** File → **Open Folder** → pick that project folder.

**Check git is pointed at GitHub:**

```bash
git remote -v
```

You should see `origin` and the GitHub URL.

**If you only had a ZIP** (no `.git` folder): unzip, then in that folder:

```bash
git init
git remote add origin https://github.com/Lithiumwow/Airtag-tracker-navigation..git
```

---

## Step 2 — Python environment and install (one time)

The project installs a library named **FindMy** from the `findmy/` folder plus its dependencies (`pyproject.toml` lists them).

**Mac / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

**Windows:**

```cmd
py -3 -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -e .
```

`-e` means “editable”: you can edit code under `findmy/` and the install picks it up without reinstalling.

---

## Step 3 — Get `device.json`

Do this on the **Mac** where the AirTag shows up in Find My.

1. Open **Terminal**, `cd` into the project folder (same place you ran `pip install -e .`).
2. Activate the venv and export keys:

   ```bash
   source .venv/bin/activate
   python3 -m findmy decrypt --out-dir airtag-export
   ```

3. macOS may ask for your **login keychain** password—that’s normal; the keys are stored there.
4. Look inside **`airtag-export/`**. You’ll see one `.json` per accessory. Open files in a text editor if you need to see names and pick the right tag.
5. Copy the one you want to the **root** of the project and name it exactly **`device.json`**.

   ```bash
   cp airtag-export/WhateverItWasNamed.json ./device.json
   ```

6. If you use Windows for the web app, copy `device.json` to the Windows project root too (USB, cloud you trust, etc.).

**macOS 15+:** storage changed for some keys. If `decrypt` fails, check the upstream FindMy.py docs (BeaconStore / plist)—links are under **Credits**.

**No Mac at all?** You still need a valid key export from somewhere; Apple doesn’t publish an official “export button” for this repo to call.

---

## Step 4 — Get `account.json`

1. Put **`device.json`** in the project root.
2. Run the web app (next section). The first time, **`account.json` doesn’t exist yet**, so the program will ask in the **terminal** for Apple ID login stuff: email, password, how you want 2FA, and the code.
3. After a good login, **`account.json`** appears next to `device.json`.

Already logged in on another machine? You can copy **`account.json`** over instead—same security rules as copying a password manager export.

To log in again from scratch later, you’d remove `account.json` and run the app again (only do that if you understand Apple may treat it as a new device/session).

---

## Step 5 — Run the web app

**Linux / macOS — helper script:**

```bash
source .venv/bin/activate
chmod +x RUN_WEB_TRACKER.sh
./RUN_WEB_TRACKER.sh
```

**Linux / macOS — by hand:**

```bash
source .venv/bin/activate
python examples/web_tracker_app.py --airtag-json ./device.json --host 0.0.0.0 --port 8008
```

**Windows** (venv activated):

```cmd
python examples\web_tracker_app.py --airtag-json .\device.json --host 0.0.0.0 --port 8008
```

Then open a browser:

- This PC: **http://localhost:8008**
- Phone on same Wi‑Fi: **http://YOUR_PC_IP:8008** (find your IP in system network settings)

**Heads-up:** the map page has **no password**. Anyone who can open that URL sees what you see. Don’t forward port 8008 to the whole internet without a VPN, tunnel, or proxy with auth. First location can take a while if the tag hasn’t been near anyone’s iPhone lately.

---

## Step 6 — Change the name in the browser tab

The tab title is plain HTML. In **`examples/web_tracker_app.py`**, find:

```html
<title>FindMy Web Tracker - Live Location</title>
```

Change the text between `<title>` and `</title>`, save, restart the Python process.

Optional: **`RUN_WEB_TRACKER.sh`** prints “FindMy Web Tracker” in a few places—you can edit those strings so the terminal messages match your name too.

---

## What belongs on GitHub vs what doesn’t

**Push:** source code, `README.md`, `LICENSE.md`, `pyproject.toml`, tests, `examples/`, `findmy/`, `scripts/`, `docs/`, CI under `.github/`, etc.

**Never commit:** `account.json`, `device.json`, `ani_libs.bin`, location caches, exports under `airtag-export/`, plist/key dumps, backup copies of those files. This repo’s `.gitignore` blocks the usual names—before `git add`, run `git status` and make sure nothing secret is listed as “new file”.

---

## If something breaks

| Symptom | Try |
|--------|-----|
| Can’t find `device.json` | File must be in the **project root**, spelled **`device.json`**. |
| Map stays empty | Tag offline, no iPhones nearby, or Apple hasn’t processed reports yet—wait and refresh. |
| Login errors | Wrong password, extra Apple security step, or expired session—may need to remove `account.json` and log in again (know the risks). |
| Port in use | Use another port: `--port 8010` or stop the old Python process. |

More depth: [FindMy.py documentation](http://docs.mikealmel.ooo/FindMy.py/).

---

## Optional: run as a Linux service

See **`scripts/findmy-webtracker.service`**. Edit paths inside the file to match where you cloned the repo and which user runs it, then install with systemd the usual way for your distro.

---

## Credits

This repo bundles code built around **[FindMy.py](https://github.com/malmeloo/FindMy.py)** (Mike Almeloo and contributors, MIT). Upstream stands on work including:

- **seemoo-lab** — [OpenHaystack](https://github.com/seemoo-lab/openhaystack/)
- **JJTech0130** — [pypush](https://github.com/JJTech0130/pypush)
- **biemster** — [FindMy](https://github.com/biemster/FindMy)
- **Dadoum** — [pyprovision](https://github.com/Dadoum/pyprovision), [anisette](https://github.com/Dadoum/anisette-v3-server)
- **nythepegasus** — [GrandSlam](https://github.com/nythepegasus/grandslam) (SMS 2FA)

The **web example** (`examples/web_tracker_app.py`) also uses:

- **[Leaflet](https://leafletjs.com/)** (maps, via CDN)
- **Tiles** — [OpenStreetMap](https://www.openstreetmap.org/copyright); dark style **© [CARTO](https://carto.com/attributions)**
- **Routing demo** — [OSRM](https://project-osrm.org/) public server (`router.project-osrm.org`); fine for demos, not a guaranteed SLA
- **Font** — [Chakra Petch](https://fonts.google.com/specimen/Chakra+Petch)

Redistribute under the same license obligations as the originals where applicable.

---

## License

See **`LICENSE.md`** (MIT for the bundled FindMy library and examples).

---

## Repository

- **This project:** [https://github.com/Lithiumwow/Airtag-tracker-navigation.](https://github.com/Lithiumwow/Airtag-tracker-navigation.)
- **Upstream docs:** [FindMy.py documentation](http://docs.mikealmel.ooo/FindMy.py/)
