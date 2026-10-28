# sharectl

[![Verify](https://github.com/oburleigh/sharectl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/oburleigh/sharectl/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/github/oburleigh/sharectl/graph/badge.svg)](https://app.codecov.io/github/oburleigh/sharectl)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)
![Python 3.15](https://img.shields.io/badge/python-3.15-blue)

sharectl is the controller that runs at each Share PoP. Kubernetes keeps the pods running from the
manifests in Git. The controller handles what Kubernetes can't: the site losing its link to the
cloud, getting it back, and a partner taking back a VM we were running on. Every decision is logged
with what triggered it, the rule it applied and why. Network config isn't the controller's job:
configuration push applies each order as it arrives, and the controller is never in an order's path.

Version 1 runs against a simulated estate: three PoPs (Barcelona, Marseille and Mombasa), three cloud
regions, Cloudflare at the edge and one Kilima Fibre VM at Mombasa. The cloud, the chain,
configuration push, the SDN controller, the site log, AAA and telemetry are stubs.

## Install

You need [uv](https://docs.astral.sh/uv/) 0.13.0 or later and network access the first time. uv
fetches Python 3.15 and the dependencies pinned in `uv.lock`.

1. Install uv, or update an older one:

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh   # or, on an older uv: uv self update
   ```

2. Clone the repository:

   ```sh
   git clone https://github.com/oburleigh/sharectl.git
   cd sharectl
   ```

3. Install Python 3.15 and the dependencies:

   ```sh
   uv sync        # or, if you have make: make install
   ```

4. Check it runs:

   ```sh
   uv run sharectl --version
   ```

## Run the scenarios

From the repository root, with the scripts and `uv`, or with `make` if you have it:

| What | Script or `uv` | `make` |
| --- | --- | --- |
| The fibre cut, about 50 s: six hours of cut at 20 ms per 10 s tick | `./scripts/fibre-cut.sh` | `make fibre-cut` |
| The partner reclaim, about 2 s | `./scripts/partner-reclaim.sh` | `make partner-reclaim` |
| The controller, live, waiting for files | `uv run sharectl run --site mombasa` | `make run` |
| Both scenarios, at once unless `--tick-ms` paces them | `uv run sharectl demo --site mombasa` | `make demo` |
| The tests | `uv run pytest` | `make test` |

`make` reads `.env` when it exists and uses Mombasa when no site is set (see Settings). Flags go
after a script, or in `ARGS` for `make`: `./scripts/fibre-cut.sh --tick-ms 5` or
`make fibre-cut ARGS="--tick-ms 5"`.

Each script hands its scenario to the controller, starting one if none is running, and prints the
controller's lines as they happen. It ends with `<scenario>: pass` or `<scenario>: FAIL` and exits
0 only on a pass. To watch the controller on its own, start it with `make run` (or the `uv` command
above) in one terminal and run the scripts in another. `uv run sharectl scenario --site mombasa
scenarios/fibre-cut.toml` plays one file at once.

## Settings

Every command option has an environment variable that sets its default, and a flag on the command
line still wins. Copy `.env.example` to `.env` to set them: `make` reads `.env` when it exists, and
`uv run --env-file .env sharectl ...` does the same without `make`.

| Variable | Default | What it sets |
| --- | --- | --- |
| `SHARECTL_SITE` | none: required | The PoPs whose controllers run, comma-separated. The scripts and `make` use `mombasa`; a name that isn't a PoP in the inventory is rejected |
| `SHARECTL_INVENTORY` | `inventory` | Where the estate, services and rules are read from |
| `SHARECTL_SCENARIOS` | `scenarios` | Where `demo` finds the scenario files |
| `SHARECTL_OUT` | `out` | Where each play writes its files |
| `SHARECTL_SIGNAL` | `signal` | Where `run` waits for files and `play` hands them over |
| `SHARECTL_TICK_MS` | `20` for `run` and `play`, `0` for `demo` and `scenario` | Wall-clock milliseconds per 10 s tick; `0` plays at once |

Every PoP runs a controller, and nothing in the code names a site, so a deployed controller is
told which PoP it runs at, by its manifest, and reads everything else about that PoP from the
inventory. Without a site the commands stop and say so. The demo runs Mombasa's controller, the one
both scenarios are about; each play's first line names the controllers running. Run it with
`--site barcelona` and both scenarios fail, because every decision they expect is Mombasa's.

## What it prints

The screen shows the whole simulated estate, not what one controller sees. Lines starting
`site/mombasa` are the controller's: its polls of its node and its decisions. Every other line comes from a stub, at the site the
line names, running without the controller: `kubernetes at mombasa`, `config-push at barcelona`,
`aaa at mombasa`. `cloud coverage` is the cloud's check when Mombasa's log arrives, and `boot` says
where Kubernetes placed each site's pods. Mombasa's controller never sees another PoP's lines.

This is the partner reclaim (boot lines for the other five sites left out):

```
controllers: site/mombasa
boot mombasa: mba-kilima-01 4, mba-srv-1 6, mba-srv-2 6, mba-srv-3 6
10:00:01 [phase] Kilima gives ten minutes' notice
10:00:01 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
10:00:01 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: cordon mba-kilima-01 and evict 4 of its 4 pods, each service at or above its min; fence at 10:05:01, release at 10:08:01 -> applied
10:00:11 kubernetes at mombasa recreated sdn-controller on mba-srv-3
10:00:11 kubernetes at mombasa recreated aaa on mba-srv-1
10:00:11 kubernetes at mombasa recreated config-push on mba-srv-2
10:00:11 kubernetes at mombasa recreated telemetry-collector on mba-srv-1
10:05:01 [phase] the fence time
10:05:01 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
10:05:01 site/mombasa fence@v1 [reclaim.fence-time] fence: mba-kilima-01 holds 0 pods of Share's, deleted; identity revoked; node deleted; disk key revoked; disks wiped -> applied
10:08:01 [phase] the release time
10:08:01 site/mombasa release@v1 [reclaim.release-time] release-lease: release mba-kilima-01: its owner reclaimed it -> applied
10:10:01 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
summary: generations_applied 0, replays_ignored 0, decisions_merged 3, pods_recreated 4, pods_pending 0, pods_left_on_released 0
partner-reclaim: pass
```

This is the start of the fibre cut. Mombasa's controller polls its node every 10 s and shows the
first poll, any poll that differs, and the same result again every five minutes. The paths are up
until the cut at 03:12:05; the next poll finds every path down, and the controller acts 30 s later,
once the cut is confirmed:

```
03:00:01 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
03:02:01 [phase] Atlas's agreement goes final
03:02:11 config-push at barcelona applied gen 8 to bcn-node-1/eth-1-12 (above applied 7)
03:02:11 config-push at marseille applied gen 8 to mrs-node-1/eth-1-12 (above applied 7)
03:02:11 config-push at mombasa applied gen 8 to mba-node-1/eth-1-12 (above applied 7)
03:05:01 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
03:10:01 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
03:12:05 [phase] the fibre is cut
03:12:11 aaa at mombasa still answers from the local credential copy
03:12:11 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 down; uplink-b bfd 198.51.100.9 down; oob bfd 203.0.113.9 down; 0/3 regions reachable
03:12:41 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: 0/3 regions reachable for 30 s on all paths -> applied
03:12:41 site/mombasa wait@v1 [blocked] wait: site-bound services keep running on site resources; nothing moves, as a copy in the cloud could not reach the node -> applied
```

And this is the end of the fibre cut. When the link returns, nothing happens for five minutes:
configuration push, the site log and the controller each wait for the link to stay up that long, by
their own measure. Then configuration push applies the handoff that went final during the cut, and
Mombasa rejoins and reconciles in the same tick because it follows the same rule, not because one
waits for the other. A stale resend of that order is ignored:

```
09:12:05 [phase] the link returns
09:12:11 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
09:17:11 config-push at mombasa applied gen 1 to mba-node-1/eth-1-13 (above applied 0)
09:17:11 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
09:17:11 site/mombasa site-rejoin@v1 [cloud.reachable] exit-isolated-mode: data path stable for 300 s -> applied
09:17:11 site/mombasa resume@v1 [unblocked] resume: the site rejoined the cloud -> applied
09:17:31 config-push at mombasa ignored gen 1 for mba-node-1/eth-1-13: 1 <= applied 1
cloud coverage mba-node-1/eth-1-12 ifHCInOctets: 73 held, 3 resent, 76 expected, 76 unique, 3 duplicates dropped, 0 missing
summary: generations_applied 5, replays_ignored 1, decisions_merged 4, pods_recreated 0, pods_pending 0, pods_left_on_released 0
fibre-cut: pass
```

Each run writes `out/<scenario>/`: each PoP's own decision log (`site-<code>.jsonl`), the cloud's
merged copy (`decisions.jsonl`), the published events, what Kubernetes did, the coverage report and
the screen. Each decision is logged twice, as `proposed` and then as its `outcome`, in the platform's
`platform.decision` CloudEvents shape. The cloud merges records by id, so a resend never counts a
decision twice.

## Tests

```sh
uv run pytest
```

Most tests feed a snapshot of a site to the decision logic and check the decisions and their
reasons word for word. Both scenarios also run end to end, twice, and every output file has to match
byte for byte.

## What's in the code

| Module | What it does |
| --- | --- |
| `inventory.py` | Reads the estate, services and rules from `inventory/*.toml` and rejects a bad file at start |
| `model.py` | The types: sites, resources, services, rules, what a controller sees, remembers and decides |
| `decide.py` | The rules and the limits: given what a controller sees, what it should do and why |
| `controller.py` | One PoP's controller: looks at its site, decides, acts and logs |
| `log.py` | Turns decisions into log records and screen lines |
| `world.py` | The simulated estate: links between sites, notices, counters |
| `kube.py` | Stands in for Kubernetes: places, recreates and deletes pods by the placement rules |
| `stubs.py` | Stand-ins for the cloud, configuration push, the site log, AAA and telemetry, including the coverage check |
| `play.py` | Plays a scenario file and checks what happened against what it expects |
| `cli.py` | The `run`, `play`, `demo` and `scenario` commands |

## What the controller does in a fibre cut

It heals nothing, because nothing needs healing: the design heals itself, and would without a
controller. Everything Mombasa's node depends on already runs at the site. AAA answers from its
local copy, the collectors keep polling into the site log, configuration push keeps what it last
applied and picks its stream up again once the link has been up for five minutes, and the site log
holds every record until it can ship. Reconciliation is configuration push's and the site log's job.

The controller only decides and records. It sees the cut from network signals, decides that nothing
should move, which is the brief's `wait`, and logs why; it logs when the site has rejoined too. It
gates nothing: configuration push and the site log each measure the link for themselves. The
controller acts on nodes only where Kubernetes can't, such as a partner taking back a VM, which can
happen during a cut as well.

The signals are the network's, as the node reports them: each uplink's link, and over it a session
to each cloud region. One uplink down while the other is up isn't a cut. Both links up with every
region session down is, and the decision records how many data links were up, so the log shows
whether the fibre or the network beyond it failed. Each node's far-end peer address on each path is
in `inventory/sites.toml`, and the poll line names it. Nothing inside a region is probed, so a cloud
service failing never isolates a site.

## How it decides

Kubernetes places pods. The controller never does. The placement rules are what Argo CD would apply
from Git: a required trust level per service, a taint on the partner VM so only services that can
live with being reclaimed land there, copies spread across providers, one copy per node, and each
service's minimum copies as a PodDisruptionBudget. When the controller moves a service off a node,
it drains the node and Kubernetes recreates the pods somewhere else. Restarts are Kubernetes' job.

There's one controller per PoP, because the PoP is the part that has to keep working on its own
through a cut. The cloud side heals with Kubernetes and Cloudflare and has no controller here.

A controller only acts on its own site. A drain never takes a service below its minimum. Actions the
controller chooses to take are capped per hour; past the cap it refuses, pages once and leaves the
call to a person. A partner's notice isn't optional, so the drain, fence and release that follow it
aren't capped.

Network config is applied once, by configuration push through the SDN controller, not the
controller. Each order reaches the site as desired state on its stream from the cloud, with a
generation per handoff. Configuration push applies a generation only if it's higher than the one the
handoff has, whatever agreement it belongs to, so a resent or stale order is ignored. During a cut
the orders wait in the cloud. Once a data path has been up for five minutes by its own measure,
configuration push picks the stream up after the last order it recorded and hands what's new to the
SDN controller without waiting for the controller. It records an order only once the SDN controller
confirms it, so an order interrupted in between comes again; the SDN controller changes nothing the
second time, and the cloud drops the repeated confirmation. Confirmations go into the site log,
which ships once a data path has been up for five minutes by its own measure, whether the controller
is running or not. Counters work the same way: each reading is stored against the interval it was
scheduled for, so a late poll lands in the right interval and a resent one is dropped instead of
counted twice.

The rules are data. `inventory/rules.toml` holds every threshold and every reason the log prints, so
changing either needs no code. No site, node, handoff or service name is a value in the code, and a
test checks every constant. Two service names show up inside text: configuration push labels its
own output `config-push`, and the AAA stub's line is keyed `aaa_local`. The clock
is simulated in 10 s ticks with no threads and no randomness, so every run of a scenario gives the
same result.

## Stubs and what would replace them

| Interface | Stub | Real |
| --- | --- | --- |
| Kubernetes | Places pods by the rules above | Each PoP's Talos cluster, running the manifests Argo CD applies |
| Cloud | Writes the next generation per handoff to its site's stream, stores records and intervals, checks coverage when a site rejoins | The circuit workflow, the desired-state stream, ingestion and the reconciler |
| Chain | The scenario's `agreement-final` events | The contract, with chain indexing following finality |
| Configuration push | Takes the site's stream from the last order it recorded and hands only a higher generation to the SDN controller | Configuration push streaming from any cell |
| SDN controller | Commits a handoff's configuration when it differs from what's running | The SDN controller, each node's only configuration writer, committing under the NETCONF lock with a confirmed commit |
| Site log | Holds records and intervals until a data path has been up for five minutes, by its own measure, then ships them | Tables in the site's Postgres, written by each producer and committed on two servers, and a shipper that deletes a batch only once two providers hold it |
| AAA | Always answers from the site's credential copy, and says so once when the site loses the cloud | FreeRADIUS at the PoP, reading the credential copy that arrives with the site's desired state |
| Telemetry | A counter rising at a fixed rate | SNMP or gNMI polling |
| Fabric | Paths, the link, reclaim notices, revoking identities and disk keys, wiping disks | Probes and provider APIs. A partner VM's disks are encrypted under a key Share's KMS holds. The fence revokes the key, then runs `talosctl reset`, so the image cache, logs and volumes can't be read even if the owner copied the disk first |

## Why run anything on a partner VM

Version 1 places pods by count rather than CPU and memory. So after Kilima takes its VM back, all 22
of Mombasa's pods fit on Share's three servers again. If the servers really had that room, there'd be
no point running anything on a VM we don't trust and can lose at ten minutes' notice.

There are two good reasons to use it, and the scenario shows neither:

- **Cover for a dead server.** One copy per node means a three-copy service needs three nodes. With a
  Share server down, the VM is the third node for the SDN controller.
- **Capacity we don't have.** The brief describes Share's own cluster as "small, finite and slow to
  grow". If the servers can't take back what the VM was running, those pods stay pending, and the
  controller logs a `wait` and pages someone. That path is built and tested, but no scenario plays
  it.

Running short means running with less redundancy for a while. A drain never takes a service below its
minimum, so pods with nowhere to go stay pending, and each service runs with fewer copies until Share
leases another VM or adds a server. The desired copies stay as declared in Git, so Kubernetes places
the pending pods as soon as there's room and nobody has to scale anything back up. Only the owner's
deadline overrides the minimum: at the fence time anything still on the VM is deleted, and the log
says what was left.

Only services that can live with being reclaimed and need no more than attested trust run on the VM,
at most one copy each. The verifier probe, the site controller, site Postgres, SPIRE, Argo CD and the
two mirrors never do.

## What version 1 leaves out

- CPU and memory. Pods are placed by count, and when they can't be placed the controller waits and
  pages rather than finding more capacity.
- Attestation and the Talos join. Kilima's VM is declared attested.
- Fencing a dead Share server through its BMC.
- Verification, probation and standby takeover. Every resource starts active, and each PoP runs one
  controller.
- The cloud's own healing, activation, chain availability and settlement.

## Writing a scenario

A scenario is a TOML file in `scenarios/` with a `name`, a `start` and an `end`, optional `phases`
(headings on the screen), the events, and what should happen. The event kinds:

| Kind | Fields | Effect |
| --- | --- | --- |
| `agreement-final` | `agreement`, `version`, `handoffs` | The cloud writes the next generation for each handoff to its site's stream |
| `paths-down`, `paths-up` | `site`, `to` (site ids or `"*"`), `paths`, `layer` (`link`, the default, or `session`) | `link` takes those paths' links at the site down or up, and every session on them with them; `session` takes only the sessions to the sites in `to` |
| `reclaim-notice` | `resources`, `notice_minutes` | The owner reclaims those resources |
| `resource-silent` | `resource` | The resource stops answering, with no notice |
| `resend-desired` | `handoff`, `generation` | The cloud puts desired state on the stream again, as a stale cell would |

`[[expect]]` lists every decision in order (`at`, `controller`, `rule`, `action`, `outcome`,
`reason_contains`). A decision the file doesn't list fails the scenario. `[[expect_config_push]]`
lists what configuration push applies or ignores, in the same way (`at`, `handoff`, `generation`,
`outcome`). `[[expect_coverage]]` and `[expect_summary]` check the reconciliation. Expected values are worked out from the rules, never
copied from the program's output.

## What I'd do next

1. Lease replacement compute from a partner, within a budget, when pods can't be placed.
2. Fence a dead Share server through its BMC.
3. Real probes behind the health checks.

## Security

- There are no secrets. `.env` holds settings, not secrets, and git ignores it; `.env.example` lists
  them.
- The inventory, rules and scenarios are data. They're parsed and validated, never executed, and
  the controller never starts a shell.
- Files are written only under `out/` and `signal/`, and their names come from values checked
  against `^[a-z0-9-]+$`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go through [SECURITY.md](SECURITY.md).

## Licence

[MIT](LICENSE)
