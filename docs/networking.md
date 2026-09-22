---
title: Networking
description: What the platform needs from the network, and how to provide it with Netbird, WireGuard, OpenVPN or IPSec.
---

# Networking

The documentation and the worked example use [Netbird](https://netbird.io/) or
[Tailscale](https://tailscale.com/) because they are the quickest way to get a
consortium talking. Neither is a requirement. This page states what SwarmMed
actually needs, so that a site can satisfy it with whatever its information
security team already approves.

## What the platform requires

**One reachable address and two TCP ports, at the coordinating site only.**

| Direction | Endpoint | Default port | Purpose |
|---|---|---|---|
| participating site → coordinating site | FL server, "fed learn" port | 8002 | client registration, task exchange, model payloads |
| operator workstation → coordinating site | FL server, admin port | 8003 | job submission and run control |
| clinician/operator browser → their own site | SwarmMedHub web interface | 443 (5085 in the example) | the interface, at the site that hosts it |

Both server ports are configurable with `SWARMMEDHUB_FED_LEARN_PORT` and
`SWARMMEDHUB_ADMIN_PORT`, which is necessary when the host already runs another
FLARE deployment.

What this means for a participating hospital:

* **No inbound connections are needed at a participating site.** Clients dial out
  to the coordinating server and keep the connection open; client-to-client
  messages are relayed by the server rather than sent site-to-site (see
  [Threat model](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration)).
  A site therefore needs an outbound allow-rule to one host and one port, not a
  hole in its perimeter.
* **No static public IP is needed at a participating site.**
* The coordinating site needs an address the others can reach, and must accept
  inbound connections on those two ports.

This is the single most useful fact to put in front of an information security
committee, and it is what makes the choice of overlay network a site decision
rather than a platform constraint.

## Addressing

The server address is written into every startup kit at provisioning. Supply it
explicitly with `--server-ip` (CLI) or the corresponding field in the interface.
If you leave it out, the platform tries to discover the host's overlay address
from a Tailscale status sidecar as a convenience; that path is optional and is
skipped entirely when the address is given. The worked example was provisioned
with an explicit address.

Whatever you use must give the coordinating host a stable address that every
participant can route to for the duration of the study.

## Options, from easiest to most institutional

### Netbird (recommended default)

Open-source, WireGuard-based, and — unlike Tailscale — the control plane can be
**self-hosted**, which removes the objection that a third party mediates
connections between hospitals. Peers authenticate against your own identity
provider; traffic is WireGuard between peers.

```bash
# each participant
curl -fsSL https://pkgs.netbird.io/install.sh | sh
netbird up --management-url https://netbird.your-institution.example
netbird status   # note the peer address of the coordinating host
```

Use the coordinating host's Netbird address as the server address. Where the
consortium is willing to use a managed control plane, Tailscale is equivalent in
capability and slightly simpler to operate; the platform does not distinguish
between them.

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
Only the coordinating site needs a reachable UDP endpoint.

### OpenVPN

Where a hospital already operates an OpenVPN concentrator, join it rather than
introducing another tunnel. Route the coordinating host's address over the
existing profile; no platform change is required. TLS-based authentication with
per-site certificates maps cleanly onto the per-site FLARE certificates.

### Site-to-site IPSec

The usual arrangement between institutions that already exchange traffic. The
network team configures a tunnel between the two sites' gateways; the platform
sees ordinary routed addresses and needs no VPN client on the host at all. This
is the heaviest to set up and the easiest to get approved, because it is what the
institution already does.

### No overlay at all

If the coordinating host is reachable on a routed address that participating
sites can reach — for example within one national research network, or via a
firewalled public address restricted to the participants' source ranges — the
platform works without any tunnel. Transport is protected by mutual TLS in all
cases; the tunnel adds network-level isolation, not confidentiality that is
otherwise missing.

## Validation status

| Option | Status |
|---|---|
| Tailscale | Used for the worked example across three hosts |
| Direct routed addressing with explicit `--server-ip` | Used for the worked example |
| Netbird | Not yet exercised in a SwarmMed deployment; WireGuard-equivalent, no platform change expected |
| Self-hosted WireGuard | Not yet exercised; no platform change expected |
| OpenVPN, site-to-site IPSec | Not yet exercised; no platform change expected |

The platform has no dependency on any of them beyond reachability, so the
untested rows are a matter of local network configuration, not of software
support. Confirm reachability with `nc -z <server-address> 8002` from each site
before provisioning.

## Troubleshooting

* A client that starts but never appears as registered is almost always blocked
  outbound to the fed-learn port, or pointed at an address that changed after
  provisioning. The address is baked into the startup kit; re-provision if it moves.
* Job submission failing while clients are connected points at the admin port
  rather than the fed-learn port.
* Check what the server sees: `docker logs <network>-server | grep "New client"`.
