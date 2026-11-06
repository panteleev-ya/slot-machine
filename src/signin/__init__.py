"""Sign the visa site in from Chrome and hand the session to the Pi.

This package is not part of the Pi zipapp. The Pi only reads an existing session.
`python -m signin` drives the Chrome already running on a Mac, or Google Chrome
on a virtual screen when the process is on Linux. `src/signin/docker/run.sh` is
that Linux Chrome in a container.

Credentials are `config/ais-login.env` (git-ignored). The Pi address and renewal
token come from `config/slot-machine.env`. Neither file is printed.
"""
