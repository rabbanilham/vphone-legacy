# Current status

This page describes the main baseline `d3a8020` (2026-10-24). Research records describe particular source revisions and local artifact hashes; they are not a compatibility guarantee for arbitrary firmware or rebuilt profiles.

| Area | Recorded evidence | Limit |
| --- | --- | --- |
| Firmware preparation | Manifest identity, decryption, checksums, HFSX and loader validation | Only 11D257 / n53ap |
| Kernel and storage | Native monitor/XNU handoff, persistent modeled storage and launchd | Software peripheral contracts, not full hardware emulation |
| UI | Original lock screen, Home and Settings in local research runs | Additional prepared keybag/boot inputs required |
| Input | Scripted unlock and Settings taps in three Home acceptance boots | General input reliability is not established |
| Graphics | AGX command replay, clipping, wallpaper, clock hands, slider spans, UISwitch and blur fixes | Partial software renderer; not a complete PowerVR implementation |
| Lifecycle | Owned QEMU process/QMP, force-stop and backing checks | No graceful guest shutdown or machine snapshots |
| Acceptance | Three Home acceptance boots recorded at an earlier revision | Full application/persistence/15-minute acceptance remains open |

See [Home acceptance](../research/reports/H-home-live.md), [clock hands](../research/reports/K-clock-hands.md), [slider](../research/reports/S-slider-glint.md), [UISwitch](../research/reports/U-uiswitch.md), and [blur](../research/reports/B-gaussian-blur.md).

## Latest integration notes retained from internal handovers

Clock/slider fixes were merged at `5df6884`; blur/switch work and follow-up `f5779b1` are included in the baseline. Local integration records reported lock, Home and Settings with no panic, thin clock hands, removal of slider artifacts, and elimination of the switch's grey shadow square. The final switch follow-up adds 9-slice texture sampling and the navigation-bar color matrix. Selected Home/Settings captures from that run are now preserved with [provenance](../research/evidence/public-readme-20261024/README.md); the full live logs are not committed. This is historical evidence, not a newly verified release acceptance run.

Open issues include a thin switch-shadow edge, jagged clock hands, incomplete verification of the glint sweep, and TCG timing that can skip visible Home animation. Time dilation was evaluated on an independent experimental branch and was not merged. At k=2/k=3 the lock screen appeared sooner, but unlock did not complete despite slider movement. The decision recorded in main is to keep the default k=1; the proposed inter-daemon timing explanation remains unproven. Wi-Fi/networking is not accepted as working. Do not infer networking, native SEP execution, full GPU support, or general application compatibility from a rendered Home screen.

The CPU is a Cortex-A57 prototype under TCG with selected Apple register behavior, not Cyclone. Storage uses a modeled ASP interface. iOS 8/9 are not supported. Native shutdown, Notes persistence through a clean shutdown, a complete Settings/Calculator/Notes input acceptance run, and a 15-minute stability session remain release criteria.
