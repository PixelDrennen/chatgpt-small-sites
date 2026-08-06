# Perforce Workspace Monitor

A local-only dashboard for one configured Perforce workspace. It uses the `p4`
command available in the terminal that starts it; it does not store credentials
or expose the server on your network.

## Run it on Fedora

1. Put this folder anywhere convenient, such as `~/Projects/perforce-monitor`.
2. Run `./start-monitor.sh`. It opens the correct local address automatically.
3. In the page's **Connect this dashboard** panel, copy `P4PORT`, `P4USER`, and
   `P4CLIENT` from P4V's connection/workspace settings. The dashboard saves
   those non-secret values in `.p4-monitor.json` next to the app so it can work
   even though it was launched outside the Perforce workspace.
4. If Perforce asks for authentication, run `p4 login` once in a terminal with
   those same values. The app does not save a password.

Do **not** double-click or open `index.html` directly. It is a web interface,
not a standalone HTML file, and needs the local Python server to call `p4`.

Use `P4_DASHBOARD_PORT=9000 python3 app.py` to choose another local port.

## What the two columns mean

- **Incoming**: submitted depot changelists that are newer than the revisions
  currently synced in the workspace (`//client/...@have,@now`).
- **Outgoing**: pending numbered changelists assigned to the current client,
  plus files in the default changelist.

The page refreshes every minute and has a manual Refresh button. Each
changelist expands into a folder tree. Perforce adds are green, edits are
yellow, and deletes are red.

## Notes

- This first version shows local pending work as outgoing. It does not attempt
  to infer stream integration history. That is intentional: pending workspace
  work and "not yet synced" depot work are stable, useful definitions even when
  a client is not stream-based.
- It needs the Helix Core command-line client available as `p4`, not P4V alone.
