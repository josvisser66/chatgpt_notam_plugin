# FAA NOTAM plugin for Codex

A local MCP server over **stdio**, with a Codex plugin package in `plugins/faa-notam`.
The FAA key, secret and environment URL come from one local TOML configuration file.
No hosted service or OpenAI API key is required.

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

## Local setup

Requires Python 3.11 or later on macOS or Linux. Run these in the project folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m notam_plugin init
```

`init` creates `config.toml` with owner-only permissions and never overwrites an
existing file. This checkout already has that file with empty credential
placeholders. Open it locally and replace `faa.key` and `faa.secret` with the
values FAA supplied separately. The FAA KEY is the OAuth client ID.

```toml
[faa]
key = "YOUR_FAA_KEY"
secret = "YOUR_FAA_SECRET"
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

Generate the launcher for this checkout:

```sh
.venv/bin/python scripts/configure_plugin.py
```

This writes `plugins/faa-notam/.mcp.json`, pointing to the checkout's virtual
environment and configuration file using absolute paths. Regenerate it if the
project moves or after cloning; this generated launcher is not committed to Git.
This is a local development plugin; copied plugin manifests still
depend on this checkout and its virtual environment.

The `faa-notam` plugin manifest is in
`plugins/faa-notam/.codex-plugin/plugin.json`. When made available in your personal
marketplace, install **FAA NOTAM** from Codex's plugin UI, then start a new task.
The server can initialize before credentials are filled in; its status tool
explains missing configuration.

You can also connect just the MCP server in your Codex configuration:

```toml
[mcp_servers.faa-notam]
command = "/absolute/path/to/chatgpt_notam_plugin/.venv/bin/python"
args = ["-m", "notam_plugin", "--config", "/absolute/path/to/config.toml", "serve"]
```

The server reserves stdout for MCP messages and sends diagnostics to stderr.
There is no listening HTTP port. FAA tokens are cached only in memory and renewed
before expiry. A rejected token triggers renewal; any retry still respects the
configured request interval.

## Search behavior

**Airport radius:** 0–100 nautical miles around the FAA airport reference point.
The query is geographic, not restricted to NOTAMs filed under that airport's ID.

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
searches and a real MCP stdio handshake. No live NMS test has been run without
the user's credentials. Public FAA NASR downloads and identifier lookup have
been verified separately.

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
