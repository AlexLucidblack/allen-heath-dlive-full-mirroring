# Sources and validation

This guide describes application build 0.6.6. Features and limits were checked against allen_heath_dlive_full_mirroring.py, redundancy.py, captured_rx.py, surface_link.py, safety_filter.py, recovery_queue.py and the JSON catalogues.

The network diagram and cover were created for this project. The interface picture schematically illustrates the English UI of build 0.6.6; it is not a browser screenshot or a verified hardware session. The application supports Russian and English through its header selector.

Official installation and distribution references:

- Python on macOS: https://docs.python.org/3/using/mac.html
- Python on Windows: https://docs.python.org/3/using/windows.html
- macOS TCP/IP settings: https://support.apple.com/en-in/guide/mac-help/mh14129/mac
- macOS manual addresses: https://support.apple.com/en-il/guide/mac-help/mchlp2718/mac
- Windows network settings: https://support.microsoft.com/en-gb/windows/experience/connectivity-networking/essential-network-settings-and-tasks-in-windows
- Gatekeeper and opening downloaded applications: https://support.apple.com/en-us/102445
- Uploading local code to GitHub: https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github
- Repository licensing: https://docs.github.com/articles/licensing-a-repository

English documentation added on 25 October 2026. OS labels can vary between releases. No open-source distribution license has been selected by the owner; see LICENSE_STATUS.md.

The 110 loopback tests passed in the preparation environment. Prepared GitHub Actions jobs have not yet been run remotely. Simulated networking is not a substitute for physical equipment tests.
