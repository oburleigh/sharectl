# sharectl

[![Verify](https://github.com/oburleigh/sharectl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/oburleigh/sharectl/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/github/oburleigh/sharectl/graph/badge.svg)](https://app.codecov.io/github/oburleigh/sharectl)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)
![Python 3.15](https://img.shields.io/badge/python-3.15-blue)

The first version of the self-healing control loop from the Share system design: one controller at
each PoP. Kubernetes places, restarts and recreates pods from the manifests in Git. The PoP
controller acts where Kubernetes cannot: it decides when its site is cut off and when it may rejoin,
applies the desired state that reaches it exactly once, and drains, fences and releases a partner
machine its owner takes back. Every decision is logged with its trigger, the rule it applied and the
reason. The cloud side, the chain, the SDN, AAA and telemetry are stubs.

## Install

You need [uv](https://docs.astral.sh/uv/) 0.13.0 or later, the first release that installs CPython
3.15.0, and network access on the first run. Nothing else: uv downloads Python 3.15 and the
dependencies pinned in the committed `uv.lock`.

1. Install uv, or update an older one:

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh   # or, on an older uv: uv self update
   ```

2. Clone the repository:

   ```sh
   git clone https://github.com/oburleigh/sharectl.git
   cd sharectl
   ```

3. Install Python 3.15 and the locked dependencies:

   ```sh
   uv sync
   ```

4. Check it runs:

   ```sh
   uv run sharectl --version
   ```

## Run it

From the repository root, the two scenario scripts from the brief:

```sh
./scripts/fibre-cut.sh         # about 50 s: six hours of cut on a 10 s tick, 20 ms per tick
./scripts/partner-reclaim.sh   # about 2 s
```

Each prints the controller's lines as they happen and ends with `<scenario>: pass` or
`<scenario>: FAIL`, exiting 0 only on a pass. Set the pace with `--tick-ms`, for example
`./scripts/fibre-cut.sh --tick-ms 5`.

Each script hands its scenario file to the running controller (`sharectl play`), starting one if
none runs, and prints the controller's lines until the verdict. To watch the controller itself, run
it in one terminal and the scripts in another:

```sh
uv run sharectl run            # waits on signal/ and plays each file handed to it
```

Both scenarios at once, with no pause, for CI: `uv run sharectl demo`. One file:
`uv run sharectl scenario scenarios/fibre-cut.toml`. Each command exits 0 only if every scenario's
expected decisions, coverage and summary match.

The partner reclaim (the boot lines of the other five sites left out):

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
10:05:01 site/mombasa fence@v1 [reclaim.fence-time] fence: mba-kilima-01 holds 0 pods of Share's, deleted; identity revoked; node deleted; disks wiped -> applied
10:08:01 [phase] the release time
10:08:01 site/mombasa release@v1 [reclaim.release-time] release-lease: release mba-kilima-01: its owner reclaimed it -> applied
summary: generations_applied 0, replays_ignored 0, decisions_merged 3, pods_recreated 5, pods_pending 0, pods_left_on_released 0
partner-reclaim: pass
```

The fibre cut ends with the reconciliation:

```
09:17:11 site/mombasa site-rejoin@v1 [cloud.reachable] exit-isolated-mode: data path stable for 300 s; ship 4 records, then 73 intervals -> applied
09:17:11 site/mombasa desired-state@v1 [desired-state.received] apply-desired-state: gen 1 for mba-node-1/eth-1-13 above applied 0; publish circuit.config.confirmed -> applied
09:17:11 site/mombasa resume@v1 [unblocked] resume: the site rejoined the cloud -> applied
09:17:31 site/mombasa desired-state@v1 [desired-state.received] apply-desired-state: gen 1 for mba-node-1/eth-1-13 again: 1 <= applied 1 -> ignored
coverage mba-node-1/eth-1-12 ifHCInOctets: 73 held, 3 resent, 76 expected, 76 unique, 3 duplicates dropped, 0 missing
summary: generations_applied 5, replays_ignored 1, decisions_merged 10, pods_recreated 0, pods_pending 0, pods_left_on_released 0
fibre-cut: pass
```

Each run writes `out/<scenario>/`: `site-<code>.jsonl` (each PoP's own decision log),
`decisions.jsonl` (the cloud's store after the merge), `events.jsonl`, `kubernetes.jsonl`,
`coverage.json` and `screen.txt`. Each decision is two `platform.decision` CloudEvents 1.0 records,
`proposed` then `outcome`, sharing a `decision_id`; the store merges by id, so a resend never
duplicates a decision.

## Test it

```sh
uv run pytest
```

Most tests build a snapshot and call `decide` directly. Both scenarios also run end to end, and
every output file is compared across two runs.

## What each module does

| Module | Does |
| --- | --- |
| `inventory.py` | Loads `inventory/*.toml` and validates it once with pydantic |
| `model.py` | The frozen domain types: estate, snapshot, memory, decision |
| `decide.py` | The rules, the guards and `authorise`: pure functions from a snapshot to decisions |
| `controller.py` | Observe, decide, act on its own site, remember, ship its log |
| `log.py` | Decisions as CloudEvents, and their screen lines |
| `world.py` | The simulated fabric: paths, the link, notices, telemetry polling |
| `kube.py` | The Kubernetes fake: places, recreates and deletes pods by the placement rules |
| `stubs.py` | The outside systems as protocols, their fakes, and the coverage check |
| `play.py` | The generic scenario player and its verdict |
| `cli.py` | `demo`, `scenario`, `run` and `play` |

## Choices

- **Kubernetes places pods; the controller never does.** The placement rules are what Argo CD would
  apply from Git: trust as a required node affinity, the partner VM's `reclaimable` taint, spread
  across providers, one copy per node, and each service's `min` as a PodDisruptionBudget. Re-home is
  a drain, after which Kubernetes recreates the pods; restart is Kubernetes' own.
- **One controller per PoP.** The PoP is what must heal on its own through a cut. The cloud side
  heals with Kubernetes and Cloudflare, so it gets no controller here.
- **A pure `decide` with explicit memory.** Rules are functions from a frozen snapshot to decisions.
  What a controller remembers between ticks is a frozen value, replaced each tick. All decision
  logic is tested without a world.
- **A synchronous virtual clock.** A 10 s tick, no threads, no randomness, no wall clock: every run
  writes byte-identical files.
- **Rules as data, never evaluated.** `rules.toml` holds the parameters and every reason text, so
  tuning a threshold or rewording a reason needs no code change. The code names no site, node,
  service, handoff or path, and a test checks that.
- **Limits.** A controller acts only on its own site. A drain never takes a service below its
  `min`. Unforced actions are capped per hour; past the cap the controller refuses, pages once and
  leaves the call to a human. An owner's notice forces the drain, fence and release, so they pass
  the cap.
- **Exactly once.** Desired state carries a generation per handoff, and a site applies only a higher
  one, whatever agreement version it carries. While isolated, a site queues desired state and
  applies the newest generation per handoff on rejoin. Intervals are keyed by the interval they
  were scheduled for, so a late poll stays in its own interval and a resent one is dropped as a
  duplicate.

## Stubs and what would replace them

| Interface | Fake | Real |
| --- | --- | --- |
| Kubernetes | `kube.py`: placement by the rules above | Each PoP's Talos cluster: kube-scheduler and the workload controllers, from the manifests Argo CD applies |
| `Cloud` | Writes the next generation per handoff, stores records and intervals by key, checks coverage on rejoin | The cells: the circuit workflow writing desired state, the desired-state stream, ingestion and the reconciler |
| Chain | The scenario's `agreement-final` events | The contract and chain indexing following finality |
| `Sdn` | Records when each handoff was configured | Configuration push over NETCONF with confirmed commit, so the node keeps one writer |
| `Aaa` | Answers from the cloud, or from the local credential copy when the site is cut off | RADIUS at the PoP |
| `Telemetry` | A counter rising at a fixed rate | SNMP or gNMI polling |
| Fabric (`world.py`) | Paths, the link, notices, identity revocation and disk wipes | Probes and provider APIs; a wipe is `talosctl reset` on disks encrypted under a key Share's KMS seals |

## Why run anything on a partner VM

The partner reclaim has a weak point worth saying out loud. Version 1 places pods by count, not by
CPU and memory, so after Kilima takes its VM back all 25 of Mombasa's pods fit again on Share's
three servers. If the servers really had that room, there would be no reason to put Share's
services on a VM Share does not trust and can lose at ten minutes' notice.

The VM earns its place for two reasons the scenario does not show:

- **N+1 for a failed server.** Each copy of a service runs on its own node, so a three-copy service
  needs three nodes. With one Share server down, the VM is the third node that keeps three copies of
  the services allowed on it (the SDN controller and the local log) running.
- **Capacity the cluster does not have.** The brief calls Share's own cluster "small, finite and
  slow to grow". When the VM holds pods the servers cannot take back, the reclaim leaves those pods
  pending, and the controller logs `wait` and pages instead of moving anything. That path is built
  and tested, but no scenario plays it.

Running short is a scale-down in effect, with less redundancy accepted for a while. The drain never
takes a service below its `min`, so pods with nowhere to go stay pending and each service runs below
its desired copies until Share leases another VM or adds a server. The desired copies stay as declared
in Git, so Kubernetes places the pending pods by itself when capacity returns; nobody has to scale
back up. Only the owner's deadline overrides `min`: at the fence time anything still on the VM is
deleted, and the fence reason says what was left.

Only services that tolerate reclaimable capacity and need no more than attested trust run there, at
most one copy each. The verifier probe, the site controller, site Postgres, SPIRE, Argo CD and the
two mirrors never do.

## What version 1 leaves out

- Capacity (CPU and memory): pods are placed by count. When pods cannot be placed the controller
  waits and pages; it does not lease capacity.
- Attestation and the Talos join: Kilima's VM is declared attested.
- Fencing a dead Share server through its BMC.
- The site log (NATS JetStream) and its shipper: the controller holds and ships its own records.
- Verification, probation and standby takeover: every resource boots active, and each PoP runs one
  controller process.
- The cloud's own healing, the activation flow, chain availability and settlement.
- `frozendict`: pyright's bundled typeshed declares `frozendict.__init__` with no arguments, so
  `frozendict(x)` fails strict checking. The frozen dataclasses hold `Mapping` fields instead.

## Writing a scenario

A scenario is a TOML file in `scenarios/`: a `name`, `start` and `end`, optional `phases` (headings
on the screen), the `event`s, and what to expect. Event kinds:

| Kind | Fields | Effect |
| --- | --- | --- |
| `agreement-final` | `agreement`, `version`, `handoffs` | The cloud writes the next generation for each handoff and sends it to the site |
| `paths-down`, `paths-up` | `site`, `to` (site ids or `"*"`), `paths` | Take paths between sites down or up |
| `reclaim-notice` | `resources`, `notice_minutes` | The owner reclaims those resources |
| `resource-silent` | `resource` | The resource stops answering, with no notice |
| `resend-desired` | `handoff`, `generation` | The cloud resends desired state already sent, as a stale cell would |

`[[expect]]` lists every decision in order (`at`, `controller`, `rule`, `action`, `outcome`,
`reason_contains`); a decision the file does not list fails the scenario. `[[expect_coverage]]` and
`[expect_summary]` check the reconciliation. Expected values come from the rules, never from the
program's own output.

## Size

Counted as code lines, without blanks, comments or docstrings: the decision logic (`decide.py`) is
188 lines, and the package, fakes and CLI included, 1,156.

## What I would improve next

1. Lease replacement capacity in Share's own marketplace, within a budget, when pods cannot be
   placed.
2. Fence a dead Share server through its BMC.
3. Real probes behind the health interface.
4. The site log and its shipper as their own components.

## Security

- No secrets, so there is no `.env` and no `.env.example`.
- The inventory, rules and scenarios are data: parsed with `tomllib`, validated with pydantic, never
  evaluated. Reasons are `string.Template` substitutions. Nothing calls `eval`, `exec`, `pickle` or
  a shell.
- Files are written only under the output directory (`out/` by default) and the signal directory
  (`signal/`). File names come from the scenario's `name` and the site codes, both validated
  against `^[a-z0-9-]+$`.
- `play` starts the controller as `python -m sharectl run` with the parsed settings, never through a
  shell.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go through [SECURITY.md](SECURITY.md).

## Licence

[MIT](LICENSE)
