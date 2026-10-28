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
configuration push, the SDN controller, the site log, AAA and telemetry are stubs. The controller
sees and changes its site only through the APIs a deployed one would use, each a dummy here that
answers in the real API's shape: gNMI on its network node, the Kubernetes API, Kilima's lease API,
SPIRE, Share's KMS and the Talos machine API (see What the controller calls).

## How it works

The controller runs all the time, one per PoP. It learns what it needs in two ways:

- **By polling its network node.** Every 10 s it asks the node over gNMI for each path's BFD peer
  and the BGP session to each cloud region. That is how it finds a fibre cut: the paths go down,
  the next poll sees it, and 30 s later it decides the site is cut off.
- **By being told.** A partner that wants its VM back sends the controller a notice. That is how
  it hears about a reclaim. From the notice until it has released the VM, it reads the pods from
  Kubernetes, because those are what it moves; the rest of the time it doesn't read Kubernetes.

Each time it polls it also checks its own deadlines: the fence and release times, which count back
from the partner's deadline, and the five minutes a data path has to stay up before a rejoin.

When it acts, it makes the calls a person would: cordon and evict through the Kubernetes API, then
at the fence ban the node's SPIRE agent, delete the node, disable its disk key in KMS and reset it
through Talos, then release the lease. Every call shows on screen with the status that came back.

Some things are deliberately not the controller's job. Configuration push applies network orders,
and the site log's shipper sends the site's records to the cloud. Each checks the link for itself:
it waits until the link has been up for five minutes without a drop, so a flapping link holds
everything until it settles. The controller never tells either of them when to go. It makes the
same check for its own decision that the site has rejoined, which is why all three happen at the
same moment.

In version 1 the outside world is simulated and every endpoint is a dummy that answers in the
real API's shape. The scenario scripts play the outside world: they cut the fibre, send Kilima's
notice, and let simulated time pass.

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

## Run it

From the repository root. Every `make` target has a plain command beside it if you don't have
`make`.

The controller is a long-running process, the one a real PoP would run. The scenario scripts are
the outside world: they only send events and let simulated time pass. Run them in two terminals.

1. In terminal 1, start the controller:

   ```sh
   make run                # or: uv run sharectl run --site mombasa
   ```

   It runs until you press Ctrl-C. It polls its node every 10 s of simulated time and prints a
   healthy poll every five simulated minutes, so you can see it is up and watching.

2. In terminal 2, run a scenario script:

   ```sh
   make fibre-cut          # or ./scripts/fibre-cut.sh
   make partner-reclaim    # or ./scripts/partner-reclaim.sh
   ```

   - `make fibre-cut` cuts every path between Mombasa and the cloud, waits out six hours of
     simulated time, then brings the link back. Nobody tells the controller; it finds the cut by
     polling its node. About a minute.
   - `make partner-reclaim` has Kilima give ten minutes' notice that it wants its VM back. The
     notice goes to the controller, which drains, fences and releases the VM.
     A few seconds.

   A script also prints the controller's lines as they come, so you can follow either terminal.
   The controller keeps running when the script ends. With no controller running, a script sends
   nothing and stops with `no controller is running: start it first with make run`.

You can also send events by hand from terminal 2. For example, cut one uplink and bring it back,
and the controller sees the other uplink is still up and does nothing:

```sh
uv run sharectl emit paths-down site=mombasa 'to=*' paths=uplink-a
uv run sharectl emit paths-up site=mombasa 'to=*' paths=uplink-a
```

The event kinds and their fields are the same as a scenario file's (see Writing a scenario).

### What each command is

| Command | What it is |
| --- | --- |
| `make run` | The controller. Runs until Ctrl-C |
| `make fibre-cut`, `make partner-reclaim` | The outside world. Sends a scenario's events to the running controller; with none running, sends nothing and says so |
| `uv run sharectl emit <kind> field=value ...` | Sends one event: a partner's notice to the controller itself, anything else into the simulated estate |
| `make demo` | The check. Plays both scenario files on their own clock, at once, and compares every decision with what the file expects. Exits 0 only if both pass |
| `make test` | The tests |

The scripts also use `sharectl wait <minutes>`, which shows the controller's new lines until its
clock has moved that far on.

`make` reads `.env` when it exists and uses Mombasa when no site is set (see Settings). Flags go
after a script, or in `ARGS` for `make`: `./scripts/fibre-cut.sh --tick-ms 5` or
`make fibre-cut ARGS="--tick-ms 5"`. The clock keeps running while each command starts, about eight
simulated minutes per second of start-up at the default pace, so the gaps between a script's events
are a little longer than it asks for. `make demo` has no such drift: it plays each file on its own
clock, so its times are the scenario's exactly. `uv run sharectl play --site mombasa
scenarios/fibre-cut.toml` does the same for one file at a live pace, and `sharectl scenario` at
once.

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
| `SHARECTL_SIGNAL` | `signal` | Where the live controller keeps its socket, `controller.sock`, which `emit` and `wait` connect to |
| `SHARECTL_TICK_MS` | `20` for `run` and `play`, `0` for `demo` and `scenario` | Wall-clock milliseconds per 10 s tick; `0` plays at once |

Every PoP runs a controller, and nothing in the code names a site, so a deployed controller is
told which PoP it runs at, by its manifest, and reads everything else about that PoP from the
inventory. Without a site, `run`, `play`, `demo` and `scenario` stop and say so. The demo runs Mombasa's controller, the one
both scenarios are about; each play's first line names the controllers running. Run it with
`--site barcelona` and both scenarios fail, because every decision they expect is Mombasa's.

## What it prints

The screen shows the whole simulated estate, not what one controller sees. Lines starting
`site/mombasa` are the controller's: its polls of its node, each call it makes to an endpoint with
the status that came back, and its decisions. Every other line comes from a stub, at the site the
line names, running without the controller: `kubernetes at mombasa`, `config-push at barcelona`,
`aaa at mombasa`, `site-log at mombasa`. `cloud coverage` is the cloud's check when Mombasa's log arrives, and `boot` says
where Kubernetes placed each site's pods. Mombasa's controller never sees another PoP's lines.

This is the partner reclaim as `sharectl play` shows it, on the scenario's own clock (boot lines for
the other five sites left out). The calls come before the decision they carry out:

```
controllers: site/mombasa
boot mombasa: mba-kilima-01 4, mba-srv-1 6, mba-srv-2 6, mba-srv-3 6
10:00:01 [phase] Kilima gives ten minutes' notice
10:00:01 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
10:00:01 site/mombasa PATCH /api/v1/nodes/mba-kilima-01 {"spec": {"unschedulable": true}} -> 200
10:00:01 site/mombasa POST /api/v1/namespaces/share/pods/sdn-controller-1/eviction {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": "sdn-controller-1", "namespace": "share"}} -> 201
10:00:01 site/mombasa POST /api/v1/namespaces/share/pods/aaa-1/eviction {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": "aaa-1", "namespace": "share"}} -> 201
10:00:01 site/mombasa POST /api/v1/namespaces/share/pods/config-push-1/eviction {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": "config-push-1", "namespace": "share"}} -> 201
10:00:01 site/mombasa POST /api/v1/namespaces/share/pods/telemetry-collector-1/eviction {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": "telemetry-collector-1", "namespace": "share"}} -> 201
10:00:01 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: cordon mba-kilima-01 and evict 4 of its 4 pods, each service at or above its min; fence at 10:05:01, release at 10:08:01 -> applied
10:00:11 kubernetes at mombasa recreated sdn-controller on mba-srv-3
10:00:11 kubernetes at mombasa recreated aaa on mba-srv-1
10:00:11 kubernetes at mombasa recreated config-push on mba-srv-2
10:00:11 kubernetes at mombasa recreated telemetry-collector on mba-srv-1
10:05:01 [phase] the fence time
10:05:01 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
10:05:01 site/mombasa spire.api.server.agent.v1.Agent/BanAgent {"id": {"trustDomain": "share.example", "path": "/spire/agent/k8s_psat/mombasa/mba-kilima-01"}} -> OK
10:05:01 site/mombasa DELETE /api/v1/nodes/mba-kilima-01 -> 200
10:05:01 site/mombasa TrentService.DisableKey {"KeyId": "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"} -> 200
10:05:01 site/mombasa machine.MachineService/Reset on mba-kilima-01 {"graceful": false, "reboot": false, "mode": "ALL"} -> OK
10:05:01 site/mombasa fence@v1 [reclaim.fence-time] fence: mba-kilima-01 holds 0 pods of Share's, deleted; identity revoked; node deleted; disk key revoked; disks wiped -> applied
10:08:01 [phase] the release time
10:08:01 site/mombasa POST /v1/leases/mba-kilima-01/release -> 204
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
their own measure. Then configuration push applies the handoff that went final during the cut, the
site log's shipper sends everything it held, and Mombasa's controller says the site has rejoined,
all at the same moment because each follows the same rule, not because one waits for another. The
shipper is in charge of its own sending; the controller ships nothing. A stale resend of the order
is ignored:

```
09:12:05 [phase] the link returns
09:12:11 site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; uplink-b bfd 198.51.100.9 up, bgp 3/3 established; oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable
09:17:11 config-push at mombasa applied gen 1 to mba-node-1/eth-1-13 (above applied 0)
09:17:11 site-log at mombasa ships what it held, its own check having seen the link stable for 300 s: decision records 4, events 1, intervals 73, resent 3
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
| `controller.py` | One PoP's controller: polls its node, reads its cluster and leases, decides, acts through the endpoints and logs |
| `endpoints.py` | The dummy endpoints: gNMI, the Kubernetes API, Kilima's lease API, SPIRE, KMS and Talos, answering in each real API's shape |
| `log.py` | Turns decisions into log records and screen lines |
| `world.py` | The simulated estate: links between sites, counters |
| `kube.py` | Stands in for Kubernetes: places, recreates and deletes pods by the placement rules |
| `stubs.py` | Stand-ins for the cloud, configuration push, the site log, AAA and telemetry, including the coverage check |
| `play.py` | Plays a scenario file and checks what happened against what it expects, or runs the estate live and takes the events sent to it |
| `cli.py` | The `run`, `wait`, `emit`, `play`, `demo` and `scenario` commands |

## What the controller calls

The controller polls its node, reads its cluster only while it is moving pods, and when it acts
sends the requests a real drain, fence and release send.

| When | Request | Answer |
| --- | --- | --- |
| Every 10 s | gNMI Get on its node: `/bfd/interfaces/interface/peers/peer/state` and the BGP `neighbors/neighbor/state` (OpenConfig, JSON_IETF) | Each BFD peer's `remote-address` and `session-state`, `UP` or `DOWN`, and each BGP neighbour's `session-state`, `ESTABLISHED` or `IDLE` |
| From a partner's notice to the release, and while pods wait for room | Kubernetes `GET /api/v1/pods` | The pods and where they run |
| Drain | `PATCH /api/v1/nodes/{node}` `{"spec": {"unschedulable": true}}`, then a policy/v1 Eviction per pod, `POST /api/v1/namespaces/share/pods/{pod}/eviction` | 200, then 201 |
| Fence | `DELETE` each pod still on the node with `gracePeriodSeconds=0`; SPIRE `Agent/BanAgent`; `DELETE /api/v1/nodes/{node}`; KMS `TrentService.DisableKey`; Talos `MachineService/Reset` | 200, OK, 200, 200, OK |
| Release | Kilima `POST /v1/leases/{id}/release` | 204 |

A partner's notice comes to the controller; it never asks Kilima for one. Kilima publishes no API,
so the notice and the release call are this design's own. A call that fails is shown with
its status and not retried; every call the controller makes in both scenarios succeeds.

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

A controller only acts on its own site; anything else it refuses, logs and pages once. A drain
never takes a service below its minimum.

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
own output `config-push`, and the AAA stub's line is keyed `aaa_local`. A scenario
plays on a simulated clock in 10 s steps, with no threads and no randomness, so every run of it
gives the same result.

## Stubs and what would replace them

| Interface | Stub | Real |
| --- | --- | --- |
| Kubernetes | Places pods by the rules above, behind a dummy API server that answers the core/v1 and policy/v1 calls a drain and a fence make | Each PoP's Talos cluster, running the manifests Argo CD applies |
| Cloud | Writes the next generation per handoff to its site's stream, stores records and intervals, checks coverage when a site rejoins | The circuit workflow, the desired-state stream, ingestion and the reconciler |
| Chain | The scenario's `agreement-final` events | The contract, with chain indexing following finality |
| Configuration push | Takes the site's stream from the last order it recorded and hands only a higher generation to the SDN controller | Configuration push streaming from any cell |
| SDN controller | Commits a handoff's configuration when it differs from what's running | The SDN controller, each node's only configuration writer, committing under the NETCONF lock with a confirmed commit |
| Site log | Holds records and intervals until a data path has been up for five minutes, by its own measure, then ships them | Tables in the site's Postgres, written by each producer and committed on two servers, and a shipper that deletes a batch only once two providers hold it |
| AAA | Always answers from the site's credential copy, and says so once when the site loses the cloud | FreeRADIUS at the PoP, reading the credential copy that arrives with the site's desired state |
| Telemetry | A counter rising at a fixed rate | SNMP or gNMI polling |
| Fabric | Paths and their links and sessions, behind each node's dummy gNMI server | The node's own BFD and BGP state over gNMI |
| Kilima | Its notice, sent to the controller, and the release call | Kilima's API, whatever its shape |
| SPIRE | BanAgent: the agent cannot call or attest again | The SPIRE server's agent.v1 Agent service |
| KMS | DisableKey on the partner VM's disk key | Share's KMS. The VM's disks are encrypted under that key, so once it is disabled the image cache, logs and volumes can't be read even if the owner copied the disk first |
| Talos | Reset: the node's disks are wiped | The node's Talos machine API, as `talosctl reset` calls it |

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
- Detecting a failed host. A node that goes silent is Kubernetes' to handle; the controller acts on
  a partner's VM only when the partner says so.
- Verification, probation and standby takeover. Every resource starts active, and each PoP runs one
  controller.
- The cloud's own healing, activation, chain availability and settlement.

## Writing a scenario

A scenario is a TOML file in `scenarios/` with a `name`, a `start` and an `end`, optional `phases`
(headings on the screen), the events, and what should happen. The same events can be sent to a
running controller with `sharectl emit <kind> field=value ...`, a list as comma-separated values.
The event kinds:

| Kind | Fields | Effect |
| --- | --- | --- |
| `agreement-final` | `agreement`, `version`, `handoffs` | The cloud writes the next generation for each handoff to its site's stream |
| `paths-down`, `paths-up` | `site`, `to` (site ids or `"*"`), `paths`, `layer` (`link`, the default, or `session`) | `link` takes those paths' links at the site down or up, and every session on them with them; `session` takes only the sessions to the sites in `to` |
| `reclaim-notice` | `resources`, `notice_minutes` | The owner tells the controllers it takes those resources back |
| `resend-desired` | `handoff`, `generation` | The cloud puts desired state on the stream again, as a stale cell would |

`[[expect]]` lists every decision in order (`at`, `controller`, `rule`, `action`, `outcome`,
`reason_contains`). A decision the file doesn't list fails the scenario. `[[expect_config_push]]`
lists what configuration push applies or ignores, in the same way (`at`, `handoff`, `generation`,
`outcome`). `[[expect_coverage]]` and `[expect_summary]` check the reconciliation. Expected values are worked out from the rules, never
copied from the program's output.

## What I'd do next

The decisions and the rules are the part that's close to production. What sits around them is a
harness with real request shapes and dummy answers. Before this runs at a real PoP:

1. Put each endpoint behind an interface, and give the controller real clients for it, a real clock
   and a real logger. Today it builds the dummies itself and borrows the simulator's clock.
2. Handle calls that fail. Retry what can be retried, such as an eviction a disruption budget
   refuses with a 429, and record a decision as failed when its calls don't go through. Today a
   decision counts as applied before its calls are made, and a failed call is only shown.
3. Authenticate every call: a Kubernetes service account, signed requests to KMS, a Talos client
   certificate and a SPIRE identity.
4. Hold the least privilege the fence needs. Talos can't scope `os:admin` to one node and SPIRE
   admin is all or nothing, so the controller should ask a narrow fence service to ban the agent
   and reset the node, not hold those rights itself.
5. Keep the controller's memory across a restart, so a restart mid-reclaim still fences and
   releases on time, and let a standby take over through the Kubernetes lease.
6. Write the decision log to the site's Postgres, where the real site log ships it from.
7. Subscribe to the node over gNMI (`ON_CHANGE`) instead of polling it, so a flap is reported the
   moment it happens.
8. Agree the notice and the release call with Kilima. Their shape here is this design's own.

Then features:

1. Lease replacement compute from a partner, within a budget, when pods can't be placed.
2. Fence a dead Share server through its BMC.
3. Place pods by CPU and memory, not by count.

## Security

- There are no secrets. `.env` holds settings, not secrets, and git ignores it; `.env.example` lists
  them.
- The inventory, rules and scenarios are data. They're parsed and validated, never executed, and
  the controller never starts a shell.
- Files are written only under `out/`, and their names come from values checked against
  `^[a-z0-9-]+$`. The live controller's only other file is its socket, `signal/controller.sock`.
- The socket has no authentication: any local process allowed to write to it can send the
  controller events.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go through [SECURITY.md](SECURITY.md).

## Licence

[MIT](LICENSE)
