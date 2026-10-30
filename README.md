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

The container runs the dashboard and a real Chromium on a virtual screen. While you
sign in, record or run, the dashboard shows that browser live (noVNC on port 6080).
Click inside the view to type into it. Both ports are published on `localhost` only.

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

```
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

- One sign-in, recording or run at a time; single user; no login on the dashboard.
- The live view has no password, so it is bound to localhost only.
- Recording against Outlook depends on Microsoft's current page structure. If a run
  fails after an Outlook update, re-record that test.
- If a password is typed during recording and is not in the Excel test data, it is
  stored as typed. For Outlook this doesn't happen, because sign-in is a separate step.
- A recorded file upload stores the file's name only. Put the file in `data/uploads/` for runs to attach it.
- Not recorded yet: an address typed into the browser's address bar, Back/Reload, and the
  browser's own confirm/alert windows.
- Steps are recorded by a person. The AI conversion of existing scripts
  (see the solution design) is the next stage.
