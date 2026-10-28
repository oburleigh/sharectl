# sharectl

[![Verify](https://github.com/oburleigh/sharectl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/oburleigh/sharectl/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/github/oburleigh/sharectl/graph/badge.svg)](https://app.codecov.io/github/oburleigh/sharectl)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)
![Python 3.15](https://img.shields.io/badge/python-3.15-blue)

sharectl is the controller that runs at each Share PoP. Kubernetes keeps the pods running from the
manifests in Git. The controller handles what Kubernetes can't: the site losing its link to the
cloud, getting it back, and a partner taking back a VM we were running on. Every decision is logged
with what triggered it, the rule it applied and why.

Version 1 runs against a simulated estate: three PoPs (Barcelona, Marseille and Mombasa), three cloud
regions, Cloudflare at the edge and one Kilima Fibre VM at Mombasa. The cloud, the chain, the SDN,
AAA and telemetry are stubs.

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
   uv sync
   ```

4. Check it runs:

   ```sh
   uv run sharectl --version
   ```

## Run the scenarios

From the repository root:

```sh
./scripts/fibre-cut.sh         # about 50 s: six hours of cut at 20 ms per 10 s tick
./scripts/partner-reclaim.sh   # about 2 s
```

Each script hands its scenario to the controller, starting one if none is running, and prints the
controller's lines as they happen. It ends with `<scenario>: pass` or `<scenario>: FAIL` and exits
0 only on a pass. `--tick-ms` sets the pace, for example `./scripts/fibre-cut.sh --tick-ms 5`.

To watch the controller on its own, start it in one terminal and run the scripts in another:

```sh
uv run sharectl run            # waits on signal/ and plays each file handed to it
```

`uv run sharectl demo` plays both scenarios straight through with no pause, which is what CI uses.
`uv run sharectl scenario scenarios/fibre-cut.toml` plays one.

This is the partner reclaim (boot lines for the other five sites left out):

```
boot mombasa: mba-kilima-01 5, mba-srv-1 7, mba-srv-2 7, mba-srv-3 6
10:00:01 [phase] Kilima gives ten minutes' notice
10:00:01 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: cordon mba-kilima-01 and evict 5 of its 5 pods, each service at or above its min; fence at 10:05:01, release at 10:08:01 -> applied
10:00:11 kubernetes recreated sdn-controller on mba-srv-3
10:00:11 kubernetes recreated aaa on mba-srv-1
10:00:11 kubernetes recreated config-push on mba-srv-2
10:00:11 kubernetes recreated telemetry-collector on mba-srv-3
10:00:11 kubernetes recreated local-log on mba-srv-2
10:05:01 [phase] the fence time
10:05:01 site/mombasa fence@v1 [reclaim.fence-time] fence: mba-kilima-01 holds 0 pods of Share's, deleted; identity revoked; node deleted; disk key revoked; disks wiped -> applied
10:08:01 [phase] the release time
10:08:01 site/mombasa release@v1 [reclaim.release-time] release-lease: release mba-kilima-01: its owner reclaimed it -> applied
summary: generations_applied 0, replays_ignored 0, decisions_merged 3, pods_recreated 5, pods_pending 0, pods_left_on_released 0
partner-reclaim: pass
```

And this is the end of the fibre cut, when Mombasa rejoins and reconciles:

```
09:17:11 site/mombasa site-rejoin@v1 [cloud.reachable] exit-isolated-mode: data path stable for 300 s; ship 4 records, then 73 intervals -> applied
09:17:11 site/mombasa desired-state@v1 [desired-state.received] apply-desired-state: gen 1 for mba-node-1/eth-1-13 above applied 0; publish circuit.config.confirmed -> applied
09:17:11 site/mombasa resume@v1 [unblocked] resume: the site rejoined the cloud -> applied
09:17:31 site/mombasa desired-state@v1 [desired-state.received] apply-desired-state: gen 1 for mba-node-1/eth-1-13 again: 1 <= applied 1 -> ignored
coverage mba-node-1/eth-1-12 ifHCInOctets: 73 held, 3 resent, 76 expected, 76 unique, 3 duplicates dropped, 0 missing
summary: generations_applied 5, replays_ignored 1, decisions_merged 10, pods_recreated 0, pods_pending 0, pods_left_on_released 0
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
| `controller.py` | One PoP's controller: looks at its site, decides, acts, logs, and ships its log to the cloud |
| `log.py` | Turns decisions into log records and screen lines |
| `world.py` | The simulated estate: links between sites, notices, counters |
| `kube.py` | Stands in for Kubernetes: places, recreates and deletes pods by the placement rules |
| `stubs.py` | Stand-ins for the cloud, SDN, AAA and telemetry, including the coverage check |
| `play.py` | Plays a scenario file and checks what happened against what it expects |
| `cli.py` | The `run`, `play`, `demo` and `scenario` commands |

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

Network config is applied once. Desired state carries a generation per handoff, and a site only
applies a generation higher than the one it has, whatever agreement it belongs to. While cut off, the
site queues what arrives and applies the newest generation per handoff when it rejoins. Counters work
the same way: each reading is stored against the interval it was scheduled for, so a late poll lands
in the right interval and a resent one is dropped instead of counted twice.

The rules are data. `inventory/rules.toml` holds every threshold and every reason the log prints, so
changing either needs no code. No site, node or service name appears in the code, and a test checks
that. The clock is simulated in 10 s ticks with no threads and no randomness, so every run of a
scenario gives the same result.

## Stubs and what would replace them

| Interface | Stub | Real |
| --- | --- | --- |
| Kubernetes | Places pods by the rules above | Each PoP's Talos cluster, running the manifests Argo CD applies |
| Cloud | Writes the next generation per handoff, stores records and intervals, checks coverage when a site rejoins | The circuit workflow, the desired-state stream, ingestion and the reconciler |
| Chain | The scenario's `agreement-final` events | The contract, with chain indexing following finality |
| SDN | Records the generation applied to each handoff | Configuration push over NETCONF with confirmed commit, so the node has one writer |
| AAA | Answers from the cloud, or from the local credential copy when the site is cut off | RADIUS at the PoP |
| Telemetry | A counter rising at a fixed rate | SNMP or gNMI polling |
| Fabric | Paths, the link, reclaim notices, revoking identities and disk keys, wiping disks | Probes and provider APIs. A partner VM's disks are encrypted under a key Share's KMS holds. The fence revokes the key, then runs `talosctl reset`, so the image cache, logs and volumes can't be read even if the owner copied the disk first |

## Why run anything on a partner VM

Version 1 places pods by count rather than CPU and memory. So after Kilima takes its VM back, all 25
of Mombasa's pods fit on Share's three servers again. If the servers really had that room, there'd be
no point running anything on a VM we don't trust and can lose at ten minutes' notice.

There are two good reasons to use it, and the scenario shows neither:

- **Cover for a dead server.** One copy per node means a three-copy service needs three nodes. With a
  Share server down, the VM is the third node for the SDN controller and the local log.
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
- The site log (NATS JetStream) and its shipper. The controller holds and ships its own records.
- Verification, probation and standby takeover. Every resource starts active, and each PoP runs one
  controller.
- The cloud's own healing, activation, chain availability and settlement.

## Writing a scenario

A scenario is a TOML file in `scenarios/` with a `name`, a `start` and an `end`, optional `phases`
(headings on the screen), the events, and what should happen. The event kinds:

| Kind | Fields | Effect |
| --- | --- | --- |
| `agreement-final` | `agreement`, `version`, `handoffs` | The cloud writes the next generation for each handoff and sends it to the site |
| `paths-down`, `paths-up` | `site`, `to` (site ids or `"*"`), `paths` | Takes paths between sites down or brings them back |
| `reclaim-notice` | `resources`, `notice_minutes` | The owner reclaims those resources |
| `resource-silent` | `resource` | The resource stops answering, with no notice |
| `resend-desired` | `handoff`, `generation` | The cloud sends desired state again, as a stale cell would |

`[[expect]]` lists every decision in order (`at`, `controller`, `rule`, `action`, `outcome`,
`reason_contains`). A decision the file doesn't list fails the scenario. `[[expect_coverage]]` and
`[expect_summary]` check the reconciliation. Expected values are worked out from the rules, never
copied from the program's output.

## What I'd do next

1. Lease replacement compute from a partner, within a budget, when pods can't be placed.
2. Fence a dead Share server through its BMC.
3. Real probes behind the health checks.
4. The site log and its shipper as their own components.

## Security

- There are no secrets, so there's no `.env` or `.env.example`.
- The inventory, rules and scenarios are data. They're parsed and validated, never executed, and
  the controller never starts a shell.
- Files are written only under `out/` and `signal/`, and their names come from values checked
  against `^[a-z0-9-]+$`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go through [SECURITY.md](SECURITY.md).

## Licence

[MIT](LICENSE)
