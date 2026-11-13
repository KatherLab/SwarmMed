# Overlay status samples for the tests

Inputs for `apps/network/test_overlay.py` and `apps/common/test_overlay_watcher.py`.

| File | Command | Origin |
|---|---|---|
| `netbird_0.82.1_needs_login.json` | `netbird status --json` | Real output of NetBird 0.82.1, daemon running, never logged in (11 Nov 2026, isolated container without network). |
| `netbird_0.82.1_idle.json` | `netbird status --json` after `netbird down` | Real output of NetBird 0.82.1, same container. |
| `netbird_0.82.1_connected_with_peers.json` | `netbird status --json` | Real output of NetBird 0.82.1 (containers-only test, 11 Nov 2026): the FL server's peer, connected to a self-hosted management server, with direct (P2P) links to two peers. |
| `netbird_0.82.1_management_down.json` | `netbird status --json` | Real output, same test: a client's peer 40 s after the management server was stopped. Still `Connected`, address and link kept, `management.connected` false with an error. |
| `netbird_0.82.1_after_logout.json` | `netbird status --json` after `netbird logout` | Real output, same test: `NeedsLogin`, no address. |
| `netbird_0.82.1_restarted_management_down.json` | `netbird status --json` | Real output, same test: a peer whose daemon was restarted while the management server was down, 5 s later: `Connecting`, no address. |
| `overlay_status_netbird_0.82.1_connected.json` | `scripts/overlay_watcher.py --once` | Real output of this helper in the same test, in a fourth peer: connected, one peer. |
| `overlay_status_netbird_0.82.1_management_down.json` | `scripts/overlay_watcher.py --once` | Real output, same peer, management server stopped. |
| `overlay_status_netbird_0.82.1_logged_out.json` | `scripts/overlay_watcher.py --once` | Real output, same peer, before it joined. |
| `overlay_status_netbird_0.82.1_connecting.json` | `scripts/overlay_watcher.py --once` | Real output, same peer, daemon restarted while the management server was down. |
| `netbird_connected_synthetic.json` | `netbird status --json` | Synthetic: built from the `OutputOverview` structs in NetBird's `client/status/status.go` (v0.82.1), connected with three peers. |
| `netbird_connecting_synthetic.json` | `netbird status --json` | Synthetic: management server unreachable at start-up (`daemonStatus` `Connecting`, no address). |
| `netbird_0.83_status_withheld_synthetic.json` | `netbird status --json` | Synthetic: NetBird 0.83.0-rc.1 answering a user who is neither root nor the one who connected (only `daemonStatus` and the versions are filled). |
| `tailscale_running_synthetic.json` | `tailscale status --json` | Synthetic: field names from Tailscale's `ipn/ipnstate/ipnstate.go`; three peers, two of them online. |
| `tailscale_needs_login_synthetic.json` | `tailscale status --json` | Synthetic: logged out. |

The real samples from the containers-only test are unchanged except for their
addresses and keys: overlay addresses were replaced by addresses in 192.0.2.0/24
(prefix lengths kept), Docker network addresses by addresses in 198.51.100.0/24,
and WireGuard public keys by placeholders. The test's host names (`nb-server.test`,
`*.netbird.selfhosted`) are its own. All other addresses are documentation
examples (100.64.0.x as in the docs, 192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24, 2001:db8::/32); names use `example.org` and `tailnet.example`.
The samples contain no single quotes, because the tests embed them in shell
scripts.
