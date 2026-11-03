# Automation Studio: prototype

Record a person performing a test once, then replay it automatically as often as needed,
with a log line per action, a screenshot per step and the results written back to Excel.

How it works under the hood (architecture, recording, replay, locators, API, procedures):
[docs/Automation_Studio_Technical_Description.pdf](docs/Automation_Studio_Technical_Description.pdf)

A test starts either from an Excel test script (upload it) or from nothing (**Record without
Excel**). `samples/Outlook_Email.xlsx` (TS_OUT001) is a sample script for **Outlook on the web**;
it sends a **real email** from abhosale@acsesolutions.com to abhosale@acsesolutions.com.

## Start it (container)

Double-click **`start_container.bat`**. It will:

1. Start Docker Desktop if it isn't running.
2. Build the `automation-studio` image the first time (a few minutes).
3. Create or start the `automation-studio` container.
4. Wait until the app is healthy and open the dashboard at http://localhost:8010.

Other commands: `start_container.bat build | open | shell | stop | remove | logs | status`.

The dashboard asks for a username and password. On the first start the studio creates
ten users, `user01` to `user10`, and writes their passwords to `data/initial_passwords.txt`.
Hand them out, then delete that file (see **Users** below).

The container runs the dashboard and a real Chromium on a virtual screen. While you
sign in, record or run, the dashboard shows that browser live (noVNC, served by the
studio under `/vnc/` behind the same sign-in). Click inside the view to type into it.
`start_container.bat` publishes the port on `localhost` only. To serve several people,
see **Deploy for several users**.

The project folder is mounted into the container, so recordings, runs and the saved
browser sign-ins live in `data/` on your machine and survive container rebuilds.

## Record without Excel

1. Click **Record without Excel**, give the test a name and the URL where it starts.
2. For a site that needs a sign-in, click **Sign in** first.
3. Click **Record steps** and do the test. On the toolbar, name each step (optional), use
   **+ Add check**, click **Next step** to start a new step, and **Finish recording** at the end.
4. The steps, their test data (every typed value, named after its field) and an Excel test
   script are created from the recording. Download the script from the **Steps** tab.
5. Click **Run automated**. You can change any typed value in the run form, including `{now}`.

## Record from an Excel script: the Outlook sample

1. **Upload** `Outlook_Email.xlsx`. The first step's test data `URL: https://outlook.office.com/mail/`
   tells the studio which application to open. Every Excel script needs such a `URL` line.
2. **Sign in** (once). Outlook opens in the live view. Sign in as abhosale@acsesolutions.com,
   approve MFA, choose *Stay signed in*, then click **Save sign-in** on the toolbar.
   The session is kept in the browser profile under `data/profiles/outlook.office.com`.
   Your password is never recorded or stored by the studio.
3. **Record steps** while following the toolbar:
   - Step 1: wait for the mailbox, **+ Add check** on "Inbox", then **Next step**.
   - Step 2: click **New mail**, then **Next step**.
   - Step 3: click **To**, type the address and press **Enter**. Click **Add a subject** and
     type the subject exactly as shown on the toolbar (it includes the current time).
     Click the body and type the message. Then **Next step**.
   - Step 4: click **Send**, then **Next step**.
   - Step 5: open **Sent Items**, **+ Add check** on the subject of the email, then **Finish recording**.
4. **Run automated**. Each run sends one real email with a fresh time in the subject
   (`{now}` in the Excel), then checks that exact subject in Sent Items.

If Microsoft ends the session (password change, policy, long inactivity), step 1 fails with
a sign-in page in its screenshot. Click **Sign in again** and rerun.

## Deploy for several users

Use `docker-compose.yml` on a Linux server (or any Docker host) instead of `start_container.bat`.
The image carries the code; only `data/` is mounted.

```
cp .env.example .env                          # set STUDIO_DOMAIN, TZ, ...
docker compose --profile https up -d --build  # studio + Caddy (HTTPS on 443)
docker compose logs studio                    # first start: "Created 10 dashboard users"
cat data/initial_passwords.txt                # hand out, then: rm data/initial_passwords.txt
```

- **HTTPS.** Caddy gets a certificate for `STUDIO_DOMAIN` by itself when that name has public DNS.
  For an internal name, uncomment `tls internal` in `deploy/Caddyfile` (or use your company's
  certificate). Without the `https` profile the studio listens on `127.0.0.1:8010` only; put your own
  proxy in front of it. Don't publish it on the network over plain HTTP: the sign-in and every
  test's data would cross the network unencrypted.
- **Another app already on 443?** Set `STUDIO_HTTPS_PORT=8443` (or any free port) in `.env`; the URL
  becomes `https://<domain>:8443`. Keep 80 free and reachable: the certificate is issued through it.
- **Firewall.** Open 80 and 443 (or your `STUDIO_HTTPS_PORT`) only, in the server's firewall and in
  the cloud provider's (on Oracle Cloud: an ingress rule in the subnet's security list). VNC (5900) never leaves the container, and port 6080 is no
  longer used.
- **Backups.** Back up `data/`: test cases, recordings, runs, saved sign-ins, `users.json`,
  `secret_key` and `logs/audit.jsonl`.
- **Updates.** `git pull` then `docker compose --profile https up -d --build`. Wait until no
  recording or run is in progress: a restart ends it.

### Users

Ten users (`user01`..`user10`) and an `admin` are created on the first start, each with a short
password such as `mint27`. Manage them with
`docker compose exec studio python -m app.users <command>` (or `.venv\Scripts\python -m app.users`
without Docker):

| Command | Does |
|---|---|
| `list` | Lists the users |
| `add <name> [password]` | Adds a user (lowercase letters, digits, `.`, `_`, `-`); generates a password when none is given |
| `add-admin <name> [password]` | Adds an admin |
| `passwd <name> [password]` | Sets a new password. That user's open sessions end |
| `remove <name>` | Removes a user. Their open sessions end |

Only salted PBKDF2 hashes are stored (`data/users.json`). A sign-in lasts 12 hours
(`STUDIO_SESSION_HOURS`). Five wrong passwords for one username lock it for 15 minutes.
Signing out ends the session on the server too, so a copied cookie stops working.

### Admin screen

An admin also has **Admin** in the top bar: every test case by everyone with who created and recorded
it, its runs, its last run and its status (not recorded, recorded, passed, failed, stopped), with
filters by creator, status and text; and a table of what each user has created, recorded and run, and
when they were last active. Click a test case to open it.

### Who sees what

Each test case belongs to the user who uploaded or created it. A user sees, opens, records, runs,
deletes and exports only their own test cases, with their runs, screenshots and live logs; other users'
test cases are not shown at all. An admin sees and can act on everyone's. Test cases created before
sign-ins existed have no owner, so only an admin sees them.

### Who did what

Every test case shows who uploaded or created it, the recording shows who recorded it, and
*Run history* shows who started each run (and who stopped it, if someone did). **Activity** in the top
bar lists every sign-in, failed sign-in, upload, new test, sign-in session, recording, run, stop,
deletion and issue-log export with the user, time and IP address, newest first. It is read from
`data/logs/audit.jsonl`, which is only appended to. Changed run data is listed by name only, never
by value.
A user's Activity shows their own entries; an admin's shows everyone's.

Only one sign-in, recording or run can use the browser at a time, for all users together. While
someone else's session runs, the status pill names them (not their test) and the buttons wait.

## Run without Docker (development)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python run.py
```

The browser then opens as a normal window instead of in the live view.

## Issue log (testing on several devices)

Each tester's studio keeps the details needed to look into a problem from another computer.
Export them as a ZIP and send it to whoever investigates:

- **Export issue log** (top bar): every run on this device that did not pass, every sign-in,
  recording and run session log, and the studio's own log. `/api/issue-log?include_passed=true`
  includes passed runs too.
- **Issue log** next to a run in *Run history* (or **Download issue log** after a failed run): that run only.

Open `issues.xlsx` first. It has one row per problem (failed or stopped steps, crashes, fallback
locators, recording warnings) and each row links to its screenshot, page HTML, trace and full log.
Every row and run names the device, OS user, studio version and browser, so ZIPs from several
testers can be compared side by side. Each run folder contains:

| File | What it holds |
|---|---|
| `log.txt` | The run as a report: device, versions, test data, each step, and each failure with the page URL, browser errors, Playwright's full error and the traceback |
| `trace.zip` | Playwright trace of a run that did not pass: every action with the page before and after it, network, console. Open with `playwright show-trace trace.zip` or at https://trace.playwright.dev |
| `failure/*.html` | The page's HTML when a step failed or was stopped |
| `browser_log.json` | Console errors, script errors, failed requests and HTTP errors during the run |
| `screenshots/`, `results.xlsx`, `run.json` | As shown in the dashboard; `run.json` has every event |

On disk, session logs are in `data/logs/sessions/` and the studio's log is `data/logs/app.log`.
Deleting a test case keeps its session logs.
Passwords (test data whose key contains "pass", and password fields typed while recording) are
masked in all JSON and text files. Screenshots, page HTML and `results.xlsx` show the application
as it was, so they can include what the test typed. A trace never holds a password typed into a password
field: it starts once no typed password is on screen (so it leaves out the sign-in), and any copy that
still reaches it, such as the sign-in request, is replaced with `******`.

Settings: `STUDIO_TRACE=failures` (default), `always` or `off`. In the container, `start_container.bat` passes
the PC's name and user as `STUDIO_DEVICE_NAME` / `STUDIO_OS_USER`; for a container created before
this change, run `start_container.bat remove` and then start it again.

## How it works

| Piece | File | Role |
|---|---|---|
| Excel reader/writer | `app/excel_io.py` | Reads the team's template (one sheet per test case, `Key: value` test data); writes results into a copy |
| Browser launcher | `app/browser.py` | One persistent profile per site, so a sign-in is reused |
| Recorder | `app/recorder.py`, `app/recorder.js` | Injects the step toolbar and captures clicks, typing (form fields and rich-text editors), Enter and checks per Excel step, with several locator candidates each. Also runs the sign-in session |
| Replayer | `app/replayer.py` | Runs the recording: tries locators most-stable-first, substitutes Excel data (`{now}` tokens resolved per run), waits for page loads, screenshots each step |
| Live log | `app/events.py` | Event stream from the worker thread to the dashboard (Server-Sent Events) |
| Issue log | `app/diagnostics.py` | Device and versions, failure capture (page, browser errors, trace), session logs, `app.log`, ZIP export with `issues.xlsx` |
| Dashboard | `app/main.py`, `app/static/` | Upload, sign in, record, run, live monitor, live browser view, history, downloads |
| Container | `Dockerfile`, `docker/entrypoint.sh`, `start_container.bat` | Xvfb virtual screen + noVNC live view + the studio |

Test data tokens: `{now}` (2026-09-25 14:32:10), `{date}`, `{time}`, `{stamp}` (20260925-143210).

## Tests

The tests other than `auth_test.py` drive the API without signing in (`STUDIO_LOGIN=off`, which the
studio only accepts while it listens on 127.0.0.1).

```
.venv\Scripts\python tests\auth_test.py          # sign-in, lockout, audit log, refused requests, live view relay
.venv\Scripts\python tests\smoke_test.py         # upload, record, run, changed data, failure
.venv\Scripts\python tests\rich_editor_test.py   # Outlook-style editors, recipient Enter, {now} subjects
.venv\Scripts\python tests\freeform_test.py      # record without Excel: steps, test data, Excel script, runs
.venv\Scripts\python tests\menu_scroll_test.py   # Fusion-style menus: script-driven groups, scroll-loaded items
.venv\Scripts\python tests\roundtrip_test.py     # record, replay on a fresh page, compare the end state: rows, iframes,
                                                 # tabs, web components, keys, double-click, hover menus, covered fields, uploads
.venv\Scripts\python tests\recording_fixes_test.py # Fusion issue-log cases: list left open over OK, a choice the next
                                                 # field resets, a double-click recorded as two clicks, no password in traces
```

The studio ships no application under test. The tests serve their own pages; `tests/demo_site.py`
is the small multi-page site the smoke and free-form tests record against.

## Prototype limits

- One sign-in, recording or run at a time, shared by all users.
- One saved sign-in per site, shared by all users: everyone's recordings and runs act in the
  application under test as whoever clicked **Save sign-in**. Use a test account there, not a person's
  own account.
- Users are trusted with what the browser can reach: a test can open any http(s) address the server
  can, including internal ones.
- Users are local to the studio (no single sign-on). A user can act only on their own test cases;
  an admin on everyone's (the Activity log records who did what).
- Recording against Outlook depends on Microsoft's current page structure. If a run
  fails after an Outlook update, re-record that test.
- If a password is typed during recording and is not in the Excel test data, it is
  stored as typed. For Outlook this doesn't happen, because sign-in is a separate step.
- A recorded file upload stores the file's name only. Put the file in `data/uploads/` for runs to attach it.
- Not recorded yet: an address typed into the browser's address bar, Back/Reload, and the
  browser's own confirm/alert windows.
- Steps are recorded by a person. The AI conversion of existing scripts
  (see the solution design) is the next stage.
