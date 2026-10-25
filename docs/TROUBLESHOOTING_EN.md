# Troubleshooting

Save diagnostics first if needed. Successful cleanup and Disconnect Both remove disk logs. Do not manually delete live SQLite files.

| Symptom | Possible cause | Action |
|---|---|---|
| Failed to fetch / browser loses application | Python ended, is unresponsive, port changed or stale URL | Check the Python window, printed URL and duplicate instances |
| Python not found | No runtime or command absent from PATH | Check py -3 --version or python3 --version |
| Unknown developer on macOS | Downloaded launcher has no Developer ID signature | Follow the specific-file Gatekeeper procedure in installation section 4 |
| Module not found | Incomplete extraction or removed .py/JSON files | Restore the complete package in one folder |
| Can't assign requested address / Errno 49 / WinError 10049 | Local address missing from OS | Check interface, address, mask and cable; a Mirror field does not assign the address |
| TCP Connection refused | Wrong address/port or service still booting | Check device address and TCP 51321, and wait for startup |
| Surface UDP Connection refused | Temporary ICMP refusal during startup | Build 0.6.4+ retries UDP; inspect udp_transient_errors and TCP state |
| MixRack connected but Surface not ready | Surface unreachable or subscription incomplete | Check its independent address, UDP/TCP and firmware |
| Ping works but control does not | Ping is not AH-Net readiness | Inspect health, phase, errors and real destination values |
| Returning console is not master | Expected active/standby behavior | The surviving system remains master |
| Queue remains after return | Destination session/object not ready or replay blocked | Export JSON before disconnecting; check error, destination_ready and health |
| Queue is blocked | Opaque addresses, uncertain delivery or another error | Preserve diagnostics and queued data before resetting |
| Both systems unavailable | No automatic conflict resolution | Restore equipment and explicitly start forwarding again |
| FX tempo differs | Effect does not follow Global Tap | Compare overall BPM, then the effect's tempo-source configuration |
| Strip Assign differs | Different bank geometry or out-of-range positions | Inspect layout_geometry and actual layers |
| No log files | Automatic cleanup or normal memory mode | Normal behavior; JSON is still available, disk traffic is empty after cleanup |
| Logs do not delete | Pending/blocked queue or file in use | Check queue, errors and log_cleanup; close external log viewers |
| Scene/Show not copied | Store transfer intentionally disabled | Use normal dLive methods |
| 48V change not mirrored | Intentional local control | Configure phantom separately on each system |
| Control failover works but audio does not switch | Audio path is separate | Design and test output redundancy separately |

## Reporting an incident

Include application build, firmware, both Surface/MixRack models, OS and Python version, initial direction, exact disconnect/change/return sequence, timing, current master and queue size.

Useful diagnostics fields include config, redundancy.phase/master/health/error/counts, surfaces.sides, sides.*.probe and log_cleanup. Check exported data for addresses, local paths and show contents before posting publicly. Use an agreed channel for raw PCAP or JSONL rather than automatically attaching private traffic to a public issue.
