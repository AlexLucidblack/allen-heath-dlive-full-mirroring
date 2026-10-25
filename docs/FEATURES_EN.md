# Complete feature reference and limitations

Application build 0.6.6. Windows and macOS use the same implementation. “Implemented” describes code behavior, not certification of every hardware combination.

## Connections and control

| Feature | Implementation | Limitation |
|---|---|---|
| No Director dependency | Direct AH-Net TCP/UDP | The Mirror computer must remain running |
| Two MixRacks | Separate identity, registration, TCP and UDP | No complete initial state copy |
| Two Surfaces | Separate op=1 sessions and named discovery | Each Surface must be reachable by IP |
| Two local IPs | Socket binding to OS-assigned addresses | Addresses are configured in the OS first |
| Surface models | S7000, S5000, S3000, C3500, C2500, C1500, CTi1500 | Not every model has been tested physically |
| MixRack models | DM32/48/64 and CDM32/48/64 | Model selection does not equal matching I/O configuration |
| Direction | Initial 1 → 2 or 2 → 1 | No simultaneous user-selectable 1 ⇄ 2 |
| Settings | connection.json after connecting | Does not store the full mixing state |
| Browser UI | Local HTTP on 127.0.0.1, default port 8780 | Not an Internet remote-control panel |
| UI language | Russian and English; header selector; preference saved in the browser | Diagnostic codes remain unchanged; blocked browser storage disables persistence |

## Mirrored changes

| Group | Behavior |
|---|---|
| Faders and Mute | Recognized state writes, echo suppression and offline repeat compaction |
| DSP/channel processing | Received properties of subscribed PEQ, filters, Gate/SCF, Compressor, Dynamics Unit, delay, digital attenuator, preamp model and insert objects |
| Routing and sends | Changes within subscribed mixer/source-select objects using available descriptors |
| FX | Properties of subscribed FX Manager objects; not local library or Show transfer |
| Names and colors | Name/color manager objects are part of the catalogue |
| Global Tempo/Tap | FX Global Tap Tempo period, separate subscription and master read approximately every 250 ms |
| Strip Assign | Position changes across six layers; left/middle/right bank widths read on both Surfaces |
| Other Surface properties | Received properties of the base subscribed objects outside local storage and the 48V filter |
| Additional Surface objects | Surface Soft Controls, South Processing Config Manager and Soft Rotaries Control Manager are observation-only |
| Known notifications | Converted to writes using fresh destination addresses |
| Unknown properties of known objects | Forwarded through name-based address mapping |
| Fully opaque data | Existing raw-byte forwarding rules; may block replay after a session changes |

The catalogue contains a fixed 2857 objects, not 2857 individually verified features. Objects may be absent on a particular system. A successful subscription does not establish the write semantics of every unknown property. Other firmware requires separate testing.

## Local data and loop protection

| Behavior | Status |
|---|---|
| Write echo suppression | Enabled |
| Recognized duplicate suppression | Enabled under existing rules |
| Fresh reconnect addresses | Resolved by name; capture IDs are not fixed runtime addresses |
| Phantom 48V | Forwarding prohibited in live control and the queue |
| Heartbeat/registration/service replies | Remain local to their sessions |
| Known telemetry and system journal | Not mirrored |
| Metering Sources | Level-stream subscription is not requested as part of mirroring |
| Surface Scene Manager / Scene Manager Intermediate / Library File Manager | Local storage; not forwarded as Store operations |
| Scene/Show saving | Disabled; development of this feature was discontinued |

## Automatic redundancy

| Feature | Behavior |
|---|---|
| Four-device monitoring | TCP/UDP state, session freshness, subscription and geometry; ping is supplementary |
| Monitor interval | Approximately 250 ms; ICMP jobs approximately once per second |
| Session freshness | Approximately 4 s checks; not a guaranteed failover time |
| Reconnect | Attempts approximately every 3 s, accounting for connection duration |
| Master failure | A fully ready surviving side becomes master |
| Return | Automatic reconnection and queued replay |
| Only Surface or rack fails | The other healthy session is retained |
| Priority discovery | Objects required by the queue first |
| Partial destination readiness | Known writes can begin before the full catalogue completes |
| Old master returns | Does not reclaim control automatically |
| Both systems lost | Explicit new start required after restoration |
| Transient Surface UDP refusal | UDP retries without destroying a live TCP connection |

## Queue

| Property | Value |
|---|---|
| Outage storage | SQLite, WAL, synchronous=FULL |
| Healthy state after cleanup | Working queue in memory |
| Limit | 250000 unfinished records or 128 MiB |
| Replay | Up to 32 commands / 32 KiB per batch; 1000 commands/s rate limit |
| Compaction | Only proven absolute input fader/Mute values between other actions |
| FX/tempo/layout/unknown actions | Not compacted by that rule |
| Ordering | Observed arrival order; no shared TCP/UDP sequence counter |
| Delivery confirmation | Socket acceptance; no universal ordinary-command readback |
| Opaque or uncertain delivery | May stop replay while retaining pending data |
| Process restart | Previous-queue continuation not implemented |

## Logs and diagnostics

Both platforms provide current status, event history, counters, diagnostics JSON and disk traffic JSONL while that file exists. Diagnostics is more detailed than the user screen: it includes ping, subscription phases, transient Surface UDP errors, master/health and log_cleanup.

Cleanup closes disk SQLite storage and keeps working state in memory. Application-owned direct/session/mirror logs, recovery SQLite/WAL/SHM and service backup JSON files are removed from log directories. Configuration and previously exported user copies are retained.

Automatic cleanup requires both ready sides, no blocking error and no unfinished queue in the current process. Disconnect Both explicitly discards unfinished data too. Deletion bypasses Trash/Recycle Bin.

## Exclusions and tested scope

No audio transport/output switching, complete Show cloning, MIDI/OSC bridge, Director plugin, cloud service, mobile app, full DSP editor, remote user accounts, signed installer or guaranteed exactly-once delivery is included.

The user confirmed physical operation on S5000 + DM64 and C3500 + DM64 with dLive 2.12 Rev. 94091. Other model selections have not each been certified on hardware. The 110 local loopback tests and prepared CI do not replace equipment testing.

Full Mirroring is the project name. Actual scope is defined by the implementation, fixed catalogue and limitations above.
