# FAA NOTAM plugin for Codex

A local MCP server over **stdio**, with a Codex plugin package in `plugins/faa-notam`.
The FAA key, secret and environment URL come from one local TOML configuration file.
No hosted service or OpenAI API key is required.

Download or clone this repository into any directory you choose. Setup detects that folder's
location; no username or developer-specific path needs to be edited in the code.
Each user supplies their own FAA credentials and installs the plugin locally.

Start with [FAA API access](#register-with-the-faa-for-api-access), then
[download and configure](#local-setup), and [connect to Codex](#connect-to-codex).
The default environment is **pre-production**. Its results are test data and must
not be treated as a production flight briefing. This is an independent integration,
not an FAA-issued plugin.

## What you can ask

- “Show NOTAMs within 25 NM of KSEA.”
- “Find NOTAMs in a corridor 10 NM on each side of KSEA–KPDX.”
- “Search KSFO, VPAAQ, 37.5/-122.3, then KSJC, within 5 NM of the route.”
- “Use 5,500 feet MSL and omit NOTAMs whose upper limits are below that altitude.”
- “Get the NOTAM checklist for KDFW.”

Routes accept a mixture of airport FAA/ICAO identifiers, FAA waypoint identifiers
(including VFR waypoints) and explicit decimal latitude/longitude objects. The
server resolves names from the current public FAA NASR airport and waypoint
files. It returns candidates for ambiguous identifiers instead of guessing.

## Register with the FAA for API access

This plugin uses the **NOTAM Management Service distribution API (NMS-API)**.
Each person or organization needs FAA-authorized API credentials for the selected
environment; downloading the plugin does not grant FAA access.

1. Visit the FAA's [NMS page](https://www.faa.gov/about/initiatives/notam) and find
   **Get API Access and Documentation**. The FAA currently directs access requests
   to [7-AWA-NAIMES@faa.gov](mailto:7-AWA-NAIMES@faa.gov). Its
   [NMS FAQ](https://www.faa.gov/about/initiatives/notam/faqs) confirms this contact
   for the NMS-API distribution interface. These instructions were checked on
   September 24, 2026; follow the FAA page if the process changes.
2. Email that address to request **NMS-API distribution access**. To help FAA route
   your request, we suggest including your name, contact information, organization
   (or that you are an individual developer), intended use, and whether you need
   pre-production testing, production access, or both. These are suggested details,
   not a claim about mandatory FAA application fields.
3. Follow the onboarding instructions FAA sends you. Ask for the current API
   documentation, authorized environment URL, credentials, and account request
   limits. FAA determines eligibility and approval; this project cannot promise
   an approval time or production access.
4. Enter the issued **KEY (OAuth client ID)** in `faa.key`, **SECRET (OAuth client
   secret)** in `faa.secret`, and the matching host in `faa.environment_url`, as
   shown below. Use the credentials issued for that environment. Changing the
   host alone does not grant production access.

NMS website sign-in through Login.gov or MyAccess is a separate flow; those login
details do not belong in this plugin's KEY/SECRET fields. Keep API credentials in
your local configuration file, not in GitHub issues or chat. The local configuration
check confirms the file is usable; your first NOTAM search tests FAA authentication.

## Local setup

Requires macOS or Linux, Python 3.11 or later, Codex, and your own FAA NMS-API
credentials. Windows setup is not covered by these scripts. Internet access is
needed to install Python dependencies and query FAA. Git is optional when using a
ZIP download. The command-line installation option needs the
[Codex CLI](https://developers.openai.com/codex/cli) on your `PATH`, with support for
`codex plugin add`; the app-only option below does not need the CLI.

### 1. Download the source

Choose one:

- **ZIP:** Open the [repository](https://github.com/josvisser66/chatgpt_notam_plugin),
  choose **Code → Download ZIP**, and extract it. Open a terminal in the extracted
  folder (usually `chatgpt_notam_plugin-main`). A source ZIP supplied by the
  maintainer instead extracts to `faa-notam-plugin-<version>`.
- **Git:** Run the following in your preferred parent directory:

```sh
git clone https://github.com/josvisser66/chatgpt_notam_plugin.git
cd chatgpt_notam_plugin
```

Download the **whole repository**, not just `plugins/faa-notam`: the Python server
and setup scripts are needed. Keep the folder at a permanent location; the installed
plugin uses it. A ZIP download works without Git or any Codex authoring skills.

### 2. Install and configure

Run these commands from the downloaded or cloned folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m notam_plugin init
```

`init` creates `config.toml` with owner-only permissions and never overwrites an
existing file. Open the newly created file locally and replace `faa.key` and
`faa.secret` with the values FAA supplied separately. The FAA KEY is the OAuth
client ID. Select the environment for which FAA issued those credentials:

```toml
[faa]
key = "REPLACE_WITH_FAA_KEY"
secret = "REPLACE_WITH_FAA_SECRET"
environment_url = "https://api-staging.cgifederal-aim.com"
response_format = "GEOJSON"

[service]
state_file = "state/rate-limits.sqlite3"
downloads_directory = "state/downloads"
```

`config.toml`, state, cached navigation records, and downloads are ignored by Git.
The plugin package contains launcher paths only; it does not contain credentials.

| Environment | `environment_url` |
| --- | --- |
| Pre-production (default) | `https://api-staging.cgifederal-aim.com` |
| Production | `https://api-nms.aim.faa.gov` |
| FIT | `https://api-fit.cgifederal-aim.com` |

Use the host only, without `/nmsapi` or `/v1`. Authentication is derived as
`{environment_url}/v1/auth/token`; data requests use
`{environment_url}/nmsapi/v1/...`. An optional `faa.auth_url` accommodates a
different authorization URL supplied by FAA. No environment fallback occurs.
Change the file and restart the MCP server to change environments or credentials.

See `config.example.toml` for all optional settings. Relative state paths resolve
beside the configuration file. `NOTAM_CONFIG` or `--config /absolute/path/config.toml`
can select a different file; `--config` takes precedence.

Check configuration without making an FAA request:

```sh
.venv/bin/python -m notam_plugin check-config
```

## Connect to Codex

Choose one installation method. Both check the selected Python runtime and local
configuration before registering the plugin; neither sends your credentials to FAA
during setup.

### With the Codex CLI

From your downloaded or cloned folder, run:

```sh
.venv/bin/python scripts/configure_plugin.py --install
```

This command completes the connection, including the registration step:

1. Generates `plugins/faa-notam/.mcp.json` with the detected Python and config paths.
2. Copies the plugin manifest and launcher to `~/plugins/faa-notam`.
3. Adds FAA NOTAM to your personal marketplace at
   `~/.agents/plugins/marketplace.json`, preserving other plugins and settings.
4. Installs or refreshes FAA NOTAM through `codex plugin add`.

The installer is included in this repository; it does not require Codex authoring
skills or files from the developer's computer. It copies no credentials. Generated
absolute paths are local to each installation and ignored by Git. Paths with spaces
are supported. The installed plugin continues to use this checkout and its `.venv`.

### With the Codex app only

If you do not have the Codex CLI, run:

```sh
.venv/bin/python scripts/configure_plugin.py --register-only
```

This prepares the same local plugin and personal marketplace, without running the
CLI. Restart the Codex app, open **Plugins**, select your personal marketplace
(normally **Personal**), then install or refresh **FAA NOTAM**. Registration makes
the plugin discoverable; the app installation step activates it.

### Verify the connection

**Start a new Codex task** after installation, then ask:

> Check my NOTAM plugin configuration.

Then try “Show NOTAMs within 25 NM of KSEA.” The status check validates local
configuration; the first search also tests FAA authentication. The default is
pre-production, so its results are test data. If the plugin does not appear in a
new task, restart Codex and check that FAA NOTAM is enabled in the plugin UI.
See [OpenAI's local plugin guidance](https://developers.openai.com/plugins/build/plugins).

### Store configuration outside the checkout

To keep configuration and saved state separate from source code, choose a path
before running `init` (or copy your existing configuration there):

```sh
export NOTAM_CONFIG="$HOME/.config/faa-notam/config.toml"
.venv/bin/python -m notam_plugin init
# Edit the file at $NOTAM_CONFIG locally, then:
.venv/bin/python -m notam_plugin check-config
.venv/bin/python scripts/configure_plugin.py --install
```

The installer uses `--config` first, then `NOTAM_CONFIG`, then the checkout's
`config.toml`. It saves the selected absolute path in the launcher, so Codex does
not need that environment variable. To select an existing file explicitly:

```sh
.venv/bin/python scripts/configure_plugin.py --install --config "$NOTAM_CONFIG"
```

### Direct MCP connection

Use this alternative instead of installing the packaged plugin, to avoid duplicate
tools. From the checkout, with `config.toml` in that folder:

```sh
codex mcp add faa-notam -- "$(pwd)/.venv/bin/python" -m notam_plugin --config "$(pwd)/config.toml" serve
```

For a different configuration file, replace `"$(pwd)/config.toml"` with its quoted
absolute path. Start a new task after connecting. If using a client without the
Codex CLI, run `.venv/bin/python scripts/configure_plugin.py` and copy the generated
server's `command` and `args` into that client's stdio MCP settings. Running this
script without either connection option only generates the launcher; it does not
connect Codex or validate the credentials. The MCP server can start without valid
credentials and explain the configuration problem through its status tool.

The server reserves stdout for MCP messages and sends diagnostics to stderr.
There is no listening HTTP port. FAA tokens are cached only in memory and renewed
before expiry. A rejected token triggers renewal; any retry still respects the
configured request interval.

## Updating, moving, and developing a checkout

For a Git clone, run these from the checkout you want the installed plugin to use:

```sh
git pull --ff-only
.venv/bin/python -m pip install -e .
.venv/bin/python scripts/configure_plugin.py --install
```

Then start a new Codex task. If you selected an external configuration file,
provide the same `--config` option or `NOTAM_CONFIG` when reinstalling.
Reinstallation updates the existing FAA NOTAM plugin; it does not create another
entry or change other plugins. If you have multiple checkouts, the last one you
install is the one Codex uses. The installer reports its location.

For a ZIP installation, extract the new download to a permanent folder, create its
`.venv`, and install the package as above. Keep your existing configuration file
and pass its absolute path with `--config` when reconnecting. An external config
also keeps rate-limit history and saved searches in the same place across updates.
App-only users should substitute `--register-only` for `--install`, then refresh
the plugin through the app and start a new task.

After moving or renaming a checkout, recreate `.venv` at the new location and
repeat package installation and `--install`. Python virtual environments should
not be moved between locations. Keep your existing configuration, and skip `init`
when it already exists. Do not remove a checkout while its installed plugin is
still using it.

For development, install the test tools with
`.venv/bin/python -m pip install -e '.[dev]'`. The editable installation loads code
from that checkout, so a new server process picks up source changes. Re-run the
installer after changing launcher paths or the plugin manifest. The generated
installation version changes to refresh Codex's cache; the tracked manifest stays
unchanged. Local configuration, generated launchers, and state stay out of Git.

## Share a source ZIP

Maintainers can create a source-only download from the repository root:

```sh
python3 scripts/build_release.py
```

This writes `dist/faa-notam-plugin-<version>.zip` and a matching `.zip.sha256`
checksum. The archive includes the README, example configuration, server, plugin
manifest, installer, and tests. An explicit file allowlist excludes `config.toml`,
`.env` files, `.venv`, `.git`, generated launchers, downloaded NOTAMs, and runtime
state. Share this archive instead of compressing your configured working folder.
Recipients follow the ZIP setup instructions above and supply their own credentials.
The command builds local files; it does not publish a GitHub release.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| `python3` is too old or `venv` is unavailable | Install Python 3.11+ with its venv support, then recreate `.venv`. |
| Setup says the configuration or environment is not ready | Run `.venv/bin/python -m pip install -e .`, then `check-config` using the same `--config` path as setup. Replace the example KEY/SECRET. |
| Codex CLI is missing or lacks `plugin add` | Use `--register-only` and install through the app, or update the CLI. |
| FAA NOTAM does not appear | Restart the app, select your personal marketplace in Plugins, install/enable FAA NOTAM, and start a new task. |
| Configuration is valid but FAA rejects authentication | Confirm the KEY/SECRET and host match the environment FAA approved. Ask the FAA API-access contact if access has not been enabled. |
| A query is rate limited | Wait the returned `retry_after_seconds`; resume route searches with their existing `search_id`. |
| A result contains a local path instead of notices | The complete result was saved because of its size. Ask Codex to read the returned file; this does not mean there are no NOTAMs. |
| The downloaded folder was moved | Recreate its `.venv` and reconnect using `--install` or `--register-only` with the correct configuration path. |

## Search behavior

**Airport radius:** 0–100 nautical miles around the FAA airport reference point.
The query is geographic, not restricted to NOTAMs filed under that airport's ID.
The FAA response can include regional records whose supplied geometry is outside
the requested radius; the airport-radius tool preserves that response rather than
applying the route tool's geometry filter. Do not assume every returned record is
physically inside the circle. Check geometry and retain uncertain relevance separately.

**Flight corridor:** more than zero and at most 99 NM on each side of consecutive
WGS84 geodesic route segments, with circular end caps. The server covers the
corridor with overlapping FAA circles (maximum radius 100 NM), deduplicates
NOTAM IDs, and checks points, lines, polygons and geometry collections against
the corridor. The geometry calculation uses short local projections, densified
boundaries and a conservative 100-meter tolerance. Candidates without usable
geometry are returned as `unclassified_notams`, not discarded.

**Altitude:** optional on airport-radius, corridor and general searches. Supply
`{"feet":5500,"reference":"MSL"}`; MSL is the default. For AGL use `"AGL"`;
for FL180 supply `{"feet":18000,"reference":"FL"}`. Only upper limits strictly
below the requested altitude in the same reference are excluded. Equal, above,
unlimited and uncertain limits remain. Upper limits can come from the structured
`upperLimit`, an explicit vertical range in NOTAM text, or `maximumFl` for a
flight-level comparison. FAA unqualified feet are interpreted as MSL; meter
values require a reference. The default Q-line value 999 is not a usable ceiling.
No terrain elevation or pressure conversion is inferred. Notices without a
usable ceiling, such as many runway/service notices, remain. Results include
exclusion counts and reasons for retained uncertainty. The filter removes only
areas wholly below your altitude; it does not exclude areas wholly above it.

**Completeness and time:** each tool reports the FAA environment and retrieval
time. FAA source content, validity times and cancellation fields are preserved.
A route's `coverage_complete` is true only when every planned circle has been
queried successfully. It describes spatial query coverage, not a guarantee that
every FAA notice has mappable geometry. Route results combine observations from
the reported first/last retrieval times; they are not an atomic snapshot. A
completed search ID returns its dated result. Start a new search for fresh data.

## FAA limits and route continuation

The supplied FAQ specifies one pre-production request per second, a production
data pull interval of three minutes, at most one delta pull per three minutes,
and at most one bulk pull per day. Bulk limits are shared across classifications.
The default content interval is 0.5 seconds. Unknown/custom environment hosts
use the conservative production data interval. Overrides in `[limits]` should
match the rate FAA has approved for your account.

Rate reservations persist in SQLite across server restarts and local processes
using the same state file. Separate computers or separate state files do not
share accounting. FAA `Retry-After` responses are persisted too. Requests are
not automatically retried in a tight loop.

A route call processes up to five successful circles. Brief pre-production
waits are handled internally; longer waits return a `search_id`,
`coverage_complete: false`, and `retry_after_seconds`. After the delay, use
`continue_notam_route_search` to resume. Completed circles are saved, so restarting
the server does not lose progress. Production routes can take several request
intervals. A failure or partial result must never be presented as “no NOTAMs.”

Large JSON results are saved in full to the configured downloads directory;
the tool returns a file path and explicitly says the response was omitted.
Bulk content paths expire after about five minutes and are fetched through the
authenticated FAA content endpoint. Downloaded gzip files remain compressed.

## Tools and development

| Tool | Purpose |
| --- | --- |
| `get_notam_status` | Check configuration, without network or secret output |
| `resolve_notam_location` | Look up FAA airport and waypoint coordinates |
| `notams_near_airport` | Geographic radius search, optional altitude |
| `notams_along_route` | Start corridor search, optional altitude |
| `continue_notam_route_search` | Resume a saved search |
| `search_notams` | FAA filters, GeoJSON/AIXM; altitude requires GeoJSON |
| `get_notam_checklist` | Retrieve identifiers and modification times |
| `get_location_series` | Retrieve location mappings or five-day deltas |
| `get_notam_initial_load` | Request a protected AIXM bulk content path |
| `download_notam_content` | Save authenticated bulk content locally |

The tool schemas are exported to `docs/mcp-tools.json`. Checks:

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src scripts tests
```

Tests use synthetic FAA responses and cover authentication, secret handling,
filter dependencies, rate limits, downloads, geometry, altitude, resumable
searches, portable installation, and a real MCP stdio handshake. Automated tests
do not contact FAA or require credentials. Public FAA NASR downloads and
identifier lookup have been verified separately.

## Sources

The implementation uses the supplied `nms-api.yaml` version 1.0.18 (revised
2026-02-12), FAQ, and examples as API reference material. It follows the newer
FAQ/specification for authenticated relative content paths; older signed-GCS-URL
examples are not followed off-host. Credentials embedded in a supplied example
are not adopted as this user's credentials or copied into the plugin.

- [FAA public NASR data](https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/)
- [FAA NOTAM altitude conventions](https://www.faa.gov/air_traffic/publications/atpubs/notam_html/chap4_section_2.html)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
- [OpenAI MCP plugin guidance](https://developers.openai.com/plugins/build/mcp-server)
