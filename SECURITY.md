# Security

Remy runs entirely on your own computer. It talks to Google's APIs to do
its work and, once a day, to GitHub to check for updates — nothing else.
The service account key in `~/.config/remy/` is the only secret involved;
it never leaves your machine, and it belongs to a robot account with an
empty Drive of its own.

## Reporting a vulnerability

Please report security issues privately to **remy@dirkpaessler.com** —
not in a public GitHub issue. You will get an answer within a few days, a
fix as fast as the finding deserves, and credit in the changelog if you
want it.
