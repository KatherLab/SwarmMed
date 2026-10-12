---
title: Networking
description: What the platform needs from the network, and how to provide it with Tailscale, Netbird, WireGuard, OpenVPN or IPSec.
---

# Networking

The documentation and the worked example use [Tailscale](https://tailscale.com/)
because it is the quickest way to get a consortium talking;
[Netbird](https://netbird.io/) is a self-hostable alternative that has not yet
been exercised with SwarmMed. No overlay network is required for federated
training itself. Network creation in the web interface and the Hub's peer status
do currently rely on Tailscale (see [Tailscale and the Hub](#tailscale-and-the-hub)).
This page states what SwarmMed actually needs, so that a site can satisfy it with
whatever its information security team already approves.

## What the platform requires

**For federated training: one reachable address and one TCP port, at the
coordinating site.** Participating sites that run their own SwarmMedHub also use
a second port, the admin port, and their Hubs can exchange status over HTTPS
(see [Traffic between Hubs](#traffic-between-hubs)).

| Direction | Endpoint | Default port | Purpose |
|---|---|---|---|
| participating site → coordinating site | FL server, fed-learn port | 8002 | client registration, task exchange, model payloads |
| participating site's Hub, if it runs one → coordinating site | FL server, admin port | 8003 | job-status polling every 60 s; job submission if training is started from that Hub |
| every Hub ↔ every other participant's Hub | SwarmMedHub HTTPS | 5085 (fixed in code) | status exchange and training-state mirroring; optional |
| users' browsers → their own site | SwarmMedHub web interface | 5085 (443 serves the same site) | the interface, at the site that hosts it |

The coordinating site's own Hub and the `swarmed` CLI reach the admin port from
the same host, so job submission and run control there need no firewall
opening. A participating Hub starts polling the admin port as soon as the
startup bundle is imported; if the port is blocked, that Hub learns nothing about
jobs from the server and each poll waits for connection timeouts.

Both server ports are configurable with `SWARMMEDHUB_FED_LEARN_PORT` and
`SWARMMEDHUB_ADMIN_PORT`. This is necessary when the host already runs another
FLARE deployment on 8002/8003: the FL server container cannot publish a port that
is already in use, so it would not start. The ports are written into the kits
when the network is provisioned, so set the variables before creating the
network, for both the `app` and the `celery_worker` service: provisioning from
the web interface or the CLI runs in the app container, and the Hub's status
display reads the fed-learn port in both. A third variable,
`SWARMMEDHUB_FLARE_ADMIN_PORT`, only adds a port to the connectivity probe
logged before each job submission. That probe also tries the admin port named
in the kit, and tries 8003 and 8002 only when neither the variable nor the kit
names a port.

Networks provisioned earlier keep their ports, and the Hub keeps polling every
network that is provisioned, starting or running, and every training job
recorded as running. Stopping a network marks its unfinished jobs as stopped,
but networks left running or provisioned continue to be probed. On a shared host,
polls for networks on the default ports reach the other deployment's server and
fail certificate verification, which fills the worker log. Stop or delete networks
you no longer use.

What this means for a participating hospital:

* **No inbound connections are needed at a participating site, provided it
  starts its runtime in client-only mode.** Every site's startup bundle also
  contains the FL server's startup kit. Without client-only mode, starting the
  network at a participating site also starts a local copy of the FL server,
  which publishes the fed-learn and admin ports on that host, and the site's
  client registers with that local server instead of the coordinator. In
  client-only mode the client dials out to the coordinating server and keeps the
  connection open; client-to-client messages are relayed by the server rather
  than sent site-to-site (see
  [Threat model](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration)).
  For training traffic, such a site therefore needs an outbound allow-rule to one
  host and one port, not a hole in its perimeter.
* **The FL client also listens locally.** It opens a TCP port chosen at random
  between 1025 and 65535 on all of the host's IPv4 addresses, without encryption,
  for its own job processes. Nothing outside the host needs to reach it, so keep
  it closed to other hosts. The client runs with host networking, so ordinary
  host firewall rules apply to it.
* **No public IP is needed at a participating site.** Each site does need a
  stable IPv4 address of its own (see [Addressing](#addressing)).
* A participating site that runs its own SwarmMedHub also uses the admin port
  and the Hub traffic in the table above; the Hub traffic needs inbound
  connections (see [Traffic between Hubs](#traffic-between-hubs)).
* The coordinating site needs an address the others can reach, and must accept
  inbound connections on the fed-learn port, and on the admin port and 5085 if
  participating sites run a Hub.

This is the single most useful fact to put in front of an information security
committee. For federated training, it is what makes the choice of overlay
network a site decision rather than a platform constraint; the Hub's
Tailscale-specific conveniences are the exception.

Client-only mode, and the choice of which site's kit to start from the bundle,
are environment variables of the process that starts the network. That is the
Hub's Celery worker: a start from the web interface or the CLI is handed to it.

```yaml
# docker-compose.override.yml at a participating site, next to docker-compose.yml
services:
  celery_worker:
    environment:
      - SWARMMEDHUB_CLIENT_ONLY_MODE=true
      - SWARMMEDHUB_LOCAL_PARTICIPANT=site-a   # the site's name as provisioned
```

Apply it with `docker compose up -d celery_worker`. Without
`SWARMMEDHUB_LOCAL_PARTICIPANT`, the start aborts unless the hostname or the
host's Tailscale address matches a site in the bundle. On the coordinating host,
if it does not also train, set `SWARMMEDHUB_SERVER_ONLY_MODE=true` for the worker
instead; otherwise starting the network there also starts a client for every
site. The port variables above go in the same file, under both `app` and
`celery_worker`. These settings apply to every network the Hub starts. The
worked example set the same variables for the processes that started each
site's client and the coordinating server.

## Listening ports by host role

What a default installation listens on. "All addresses" means every IPv4 and
IPv6 address of the host.

**Coordinating host**

| Port (default) | Bound to | Service | Must be reachable from |
|---|---|---|---|
| 8002/tcp, fed-learn | all addresses | FL server container | every participating site's FL client |
| 8003/tcp, admin | all addresses | FL server container | participating sites' Hubs, if they run one |
| 5085/tcp | all addresses | nginx, HTTPS | users' browsers; other participants' Hubs (optional) |
| 443/tcp | all addresses | nginx, HTTPS, same site as 5085 | nothing in the platform |
| 80/tcp | all addresses | nginx, HTTP | nothing; it redirects to `https://<host>:5085` and serves Let's Encrypt challenges, which are not set up by default |
| 9000, 9001/tcp | `DOCKER_HOST_IP` | MinIO API and console | this host and its containers |
| 5432/tcp | 127.0.0.1 | PostgreSQL | this host |
| 6379/tcp | 127.0.0.1 | Redis, TLS only | this host |

On Linux, `make env` sets `DOCKER_HOST_IP` to the gateway address of Docker's
default bridge network as Docker reports it (usually 172.17.0.1, which is also
the fallback when Docker cannot be queried); on macOS and Windows it sets
127.0.0.1. A value already in `.env` is kept. `POSTGRES_BIND_ADDR` and
`POSTGRES_HOST_PORT` change the PostgreSQL binding. If the coordinating host is
also a training site, it runs an FL client as well, with the listener below.

**Participating site**

| Port (default) | Bound to | Service | Must be reachable from |
|---|---|---|---|
| random, 1025–65535/tcp | all IPv4 addresses | FL client's internal listener, unencrypted | this host only (the client's own job processes) |

A participating site that runs its own Hub also has the nginx, MinIO, PostgreSQL
and Redis ports listed for the coordinating host. Local test networks publish no
ports unless `SWARMMEDHUB_LOCAL_TEST_PUBLISH_PORTS` is set. `make docs-serve`, a
documentation preview, listens on 0.0.0.0:9999 while it runs.

Ports published by Docker (nginx's and the FL server's) are opened in Docker's
own packet-filter rules and are not filtered by host firewalls such as UFW.
Restrict them with rules in Docker's `DOCKER-USER` chain on the external
interface, or at a network firewall. SwarmMed has no setting that binds the FL
server's ports to a single address.

Besides the flows above, a host that starts a network builds the runtime image
at that point, downloading a base image from Docker Hub and Python packages from
PyPI. It does not if a prebuilt image is already on the host and configured with
`SWARMMEDHUB_FLARE_IMAGE` together with `SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true`;
without the second variable the image is built anyway (see
[Runtime backend and portability](runtime-backend.md#nvflare-version)).
Installing the Hub also downloads software from public registries, and building
its images fetches MinIO's source code from GitHub, so `github.com` must be
reachable then.

## Traffic between Hubs

When participating sites run their own SwarmMedHub, the Hubs exchange status
over HTTPS on port 5085, which is fixed in the code. Training does not need any
of it: in the worked example the training hosts ran no Hub worker and sent none
of it.

* **Status exchange.** Every 30 s, and whenever someone opens the Network page,
  a Hub posts its status to
  `https://<participant address>:5085/network/api/gossip/<network>/` for every
  other participant of each running network. The coordinating Hub also forwards
  every client's status as its FL server's log shows it.
* **Training-state mirroring.** Every few seconds while someone has the Training
  page or the home page open, a Hub asks the coordinating Hub, then up to three
  participants' Hubs, for the current job state (`/training/api/state/<network>/`),
  waiting up to 2 s for each.

Both need inbound connections on port 5085:

* A participating Hub shows the other participants' status only if it accepts
  these connections, at least from the coordinating host. Otherwise its Network
  page shows them as "Offline" (never heard from) or "Offline (Stale)" (nothing
  for three minutes).
* Participating Hubs get the job state from the coordinating Hub only if it
  accepts their connections. Otherwise their Training page falls back to other
  participants' Hubs, their own client container's log and their admin-port
  polling.
* On any Hub, each peer that silently drops these connections delays every
  status refresh of the Training and home pages by up to 2 s.
* The coordinating Hub's Network page does not use this channel; it reads its
  own FL server's log.
* Training, model exchange and results are not affected.

!!! note "Known limitation"
    In the worked example the coordinating Hub showed connected remote clients
    as offline. A defect in that display has been fixed since (its log patterns
    were compared with the wrong letter case, so join and heartbeat messages
    never matched), but it has not been checked again in a multi-site run. Check
    connectivity in the FL server's log instead (see
    [Troubleshooting](#troubleshooting)).

A Hub accepts these requests only if the address it was provisioned with is
listed in its `DJANGO_ALLOWED_HOSTS`; the same applies to every name or address
users type into their browsers. A Hub sends status only if it finds its own
Tailscale address among the participants (see
[Tailscale and the Hub](#tailscale-and-the-hub)).

The channel uses HTTPS without certificate verification by default
(`SWARMMEDHUB_SKIP_PEER_SSL_VERIFY` defaults to `true`). Turning verification on
does not work with the generated certificates: each site's web certificate is
issued by that site's own internal CA and names only `localhost` and
`127.0.0.1`, which is also why browsers on other machines warn about it.
Requests are authenticated with one token per network, carried in every site's
startup bundle, so anyone holding a bundle can post status for any participant.
Restrict port 5085 to the participants' addresses.

Training containers that read their data through the platform's data adapter
(the bundled examples do; the worked example read a local directory) first ask
the coordinating Hub on port 5085 for their data manifest, then their own Hub.
Nothing needs to answer that first request.

## Addressing

The coordinating host's address is fixed when the network is provisioned. It is
written into every startup kit (`startup/server_host.txt`) and becomes a hosts
entry for the name `server` in the client containers at participating sites. It
must be an IPv4 address that every participant can reach and that stays the same
for the study.

Give it with `--server-ip` when creating the network with the CLI, run inside
the Hub's app container (see [CLI](cli.md)):

```bash
docker exec -w /app swarmmedhub python3 -m swarmed_cli.main network create \
  --user alice --project <PROJECT_UUID> --name "Hospital Swarm" \
  --server-ip 10.90.0.1 \
  --participant site-a=10.90.0.12 \
  --participant site-b=10.90.0.13 \
  --no-local-client
```

* `--server-ip` accepts only a dotted IPv4 address. A hostname or an IPv6
  address is silently ignored, and the host's Tailscale address is used instead
  if it has one.
* The web interface has no field for the server address. Networks created there
  always get the host's Tailscale address; on a host without Tailscale their
  kits contain no server address, and remote sites cannot reach the server. A
  consortium that does not use Tailscale must create its networks with the CLI.
* The web interface also always adds the coordinating host as a training site;
  the CLI does so unless `--no-local-client` is given.

Each participant also needs an IPv4 address of its own (`--participant NAME=IPv4`).
It does not have to be public or reachable from other sites, but it must stay
assigned to that site's host for the study: it is written into the site's kit,
the FL client's job processes connect to their parent process at that address,
and other Hubs use it to reach that site's Hub. A participant entered without a
valid IPv4 address gets no startup kit; provisioning only logs a warning.

### Tailscale and the Hub

SwarmMed does not install, configure or join Tailscale; `tailscale up` is a
manual step. `make start` launches a helper on the host,
`scripts/tailscale_watcher.sh`, which runs `tailscale ip -4` and
`tailscale status --json` every 30 s and writes the result to
`tmp/tailscale_status.json`. The Hub reads that file; there is no Tailscale
container. The helper is not a service and does not survive a reboot (start it
again with `make tailscale`), and the Hub keeps using the last address in the
file without checking its age.

The Hub uses that address:

* as the server address of networks created in the web interface (always), and
  in the CLI when `--server-ip` is missing or is not an IPv4 address;
* to recognise which participant it is. A Hub sends status and mirrors training
  state ([Traffic between Hubs](#traffic-between-hubs)), and the coordinating Hub
  shows client status from its FL server's log, only if the host's Tailscale
  address is the address it was provisioned with;
* as one of the ways, with `SWARMMEDHUB_LOCAL_PARTICIPANT` and the hostname, to
  pick the local client kit from a bundle;
* for the Tailscale status shown on the Network page.

FL traffic does not use it: the FL server and clients use the provisioned
addresses. On any other network, or none, create the network with the CLI and
`--server-ip`, set `SWARMMEDHUB_LOCAL_PARTICIPANT` at each site, and expect the
Hub to show the other participants as offline.

## Options, from easiest to most institutional

For every option other than Tailscale, create the network with the CLI's
`--server-ip`, set `SWARMMEDHUB_LOCAL_PARTICIPANT` at each site, and do not rely
on the Hub's peer status (see [Tailscale and the Hub](#tailscale-and-the-hub)).
Only Tailscale has been exercised with SwarmMed so far
([Validation status](#validation-status)).

### Tailscale (used in the worked example)

WireGuard-based, with a managed control plane, and the only overlay the Hub
reads. Install it on every host, join the same tailnet with `sudo tailscale up`,
and use the coordinating host's address (`tailscale ip -4`) as the server
address. Tailscale's own coordination service cannot be self-hosted. Its clients
can be pointed at a self-hosted, Tailscale-compatible control server from a
third-party project such as Headscale (`tailscale up --login-server <url>`);
that combination has not been exercised with SwarmMed.

### Netbird (self-hosted control plane)

Open source and WireGuard-based. Its control plane is supported for
**self-hosting**, which removes the objection that a third party mediates
connections between hospitals. Peers are enrolled through your own identity
provider or with setup keys; traffic is WireGuard between peers.

```bash
# each participant
curl -fsSL https://pkgs.netbird.io/install.sh | sh
netbird up --management-url https://netbird.your-institution.example
netbird status -d   # lists peers with their NetBird IPs; note the coordinating host's
```

Use the coordinating host's NetBird address as `--server-ip`. FLARE traffic does
not distinguish Netbird from Tailscale; the Hub's conveniences do (see
[Tailscale and the Hub](#tailscale-and-the-hub)).

### Self-hosted WireGuard

No control plane at all: a small set of static peers, which is often the easiest
thing for a hospital network team to reason about and audit.

```ini
# /etc/wireguard/wg0.conf at a participating site
[Interface]
PrivateKey = <site private key>
Address    = 10.90.0.12/24

[Peer]                       # the coordinating site
PublicKey  = <coordinator public key>
Endpoint   = coordinator.example.org:51820
AllowedIPs = 10.90.0.1/32    # only the FL server, nothing else
PersistentKeepalive = 25
```

`AllowedIPs` restricted to the coordinator's address is worth pointing out in a
security review: the tunnel cannot reach anything else on the consortium network.
Only the coordinating site needs a reachable UDP endpoint. Provision each site
with its tunnel address (`site-a=10.90.0.12`) and the server with
`--server-ip 10.90.0.1`. In this layout a participating site's Hub can exchange
status with the coordinating Hub, which forwards every client's status, but not
with other participating sites' Hubs.

### OpenVPN

Where a hospital already operates an OpenVPN concentrator, join it rather than
introducing another tunnel. Route the coordinating host's address over the
existing profile; FLARE traffic needs no platform change. Where OpenVPN
authenticates each site with its own certificate, that authenticates the hosts
independently of the FLARE study certificates.

### Site-to-site IPSec

The usual arrangement between institutions that already exchange traffic. The
network teams configure a tunnel between each participating site's gateway and
the coordinating site's; the platform sees ordinary routed addresses and needs
no VPN client on the host at all. This is the heaviest to set up and often the
easiest to get approved, because it is what the institution already does.

### No overlay at all

If the coordinating host is reachable on a routed address that participating
sites can reach — for example within one national research network, or via a
firewalled public address restricted to the participants' source ranges — FLARE
traffic does not need a tunnel.

Federated-learning traffic (the fed-learn and admin ports) is protected by mutual
TLS in all cases. Two things limit what that TLS provides on its own:

* The startup bundle that every site imports contains the FL server's private
  key and every site's client and admin keys. TLS therefore does not stop one
  consortium member from impersonating the server, or another site, on a network
  path it can influence.
* The Hubs' status traffic on port 5085 is HTTPS without certificate
  verification by default (see [Traffic between Hubs](#traffic-between-hubs)).

An overlay with per-host keys therefore adds host authentication that TLS alone
does not provide here, on top of network-level isolation: traffic to the
coordinator's overlay address reaches only the host that holds the coordinator's
overlay key. Source-address filtering on the server's ports limits who can
connect to the server.

## Validation status

| Option | Status |
|---|---|
| Tailscale | Used for the worked example: the coordinating host and three training hosts on one tailnet, with the coordinating host's Tailscale address as the server address |
| Direct routed addressing, no overlay | Not yet exercised; no platform change expected for FLARE traffic |
| Netbird | Not yet exercised; no platform change expected for FLARE traffic; provision with the CLI's `--server-ip` |
| Self-hosted WireGuard | Not yet exercised; no platform change expected for FLARE traffic; provision with the CLI's `--server-ip` |
| OpenVPN, site-to-site IPSec | Not yet exercised; no platform change expected for FLARE traffic; provision with the CLI's `--server-ip` |

Federated training has no dependency on any of them beyond reachability and a
stable IPv4 address for every participant, so for FLARE traffic the untested
rows are a matter of local network configuration. The Hub's conveniences are
Tailscale-specific (see [Tailscale and the Hub](#tailscale-and-the-hub)).

Once the coordinating site has started the network, confirm reachability from
each site with `nc -zv <server-address> <fed-learn port>` (8002 by default)
before starting that site's client. Before the server runs nothing listens on
the port, so a refused connection is expected; to test the path in advance, run
a temporary listener on the coordinating host (`nc -l 8002` with the OpenBSD
netcat that Ubuntu ships).

## Troubleshooting

* A client that starts but never appears as registered is usually blocked
  outbound to the fed-learn port, pointed at an address that changed after
  provisioning, or started without client-only mode. In the last case the site
  also runs a `swarm-…-server` container, and its client joined that.
* The coordinator's address is stored in each kit's `startup/server_host.txt` and
  applied as a hosts entry for the name `server` when the client container
  starts. If the address changes, set `SWARMMEDHUB_SERVER_HOST=<new IPv4>` for
  the process that starts the client (normally the site's Celery worker) and
  restart the client in client-only mode; the variable takes precedence over
  `server_host.txt`. Re-provisioning also works, but it issues new certificates
  that must be redistributed to every site.
* Job submission uses only the admin port, which the coordinating Hub reaches
  from the same host, so a submission that fails while clients are connected is
  rarely a firewall problem. The training log lists the hosts and ports the
  check before submission tried.
* Check what the server sees, on the coordinating host:
  `docker logs swarm-<first 12 characters of the network ID>-server | grep "New client"`
  (for example `swarm-1a7f19db-dd2-server`). The address after `@` is the one
  the client reports for itself (normally the address of its default route), not
  the address its connection came from. The same lines are in the server's
  `log.txt` under
  `workspaces/<project ID>/<network ID>/workspace/<project name>/prod_00/server/`.
