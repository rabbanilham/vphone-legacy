# README screenshots — 2026-10-29

Whole `vphone-player` window (title bar, toolbar and the iPhone 5s frame) showing the original iOS 7.1.2 Home and Settings screens. Captured from a normal-speed live run of the `feat/player-bezel` build (`9b98d89`) with the scripted unlock-and-open-Settings touch sequence, using `screencapture -l` on the player window at 2x Retina scale.

The images are unretouched window captures: not cropped, resized, composited or edited. [Provenance](provenance.json) records the machine source, backend binary, player source and frame asset hashes. The guest clock is not the host capture time, and the capture time inside the guest was not recorded. This is one run on a host with other load, not an acceptance run.

Known limits are unchanged: the Extras folder icon, the folder panel corners and the keyboard under Safari still have rendering defects (see [status](../../../docs/status.md)). These screenshots do not establish networking, general application compatibility, or an IPSW-to-UI recipe.
