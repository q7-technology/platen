# Security

## Reporting something

Email **security@q7technology.com.au** rather than opening an issue. Tell us
what you found and how to see it happen; we will confirm we have it within a
few working days.

## What Platen assumes

Platen prints labels on a site's own network. It expects to sit behind
something that terminates TLS, and it does not try to be an internet-facing
service.

- **Sessions.** A random token in an http-only, same-site cookie. Only its
  hash is stored, so a database backup cannot be replayed as a login. The
  cookie is marked `Secure` when the request arrives over https; set
  `PLATEN_SECURE_COOKIES=true` if a proxy terminates TLS without passing the
  scheme through. Over plain http a session cookie travels in the clear, which
  is a property of plain http and not something Platen can fix for you.
- **Passwords.** scrypt at about 64 MB a hash. Ten wrong guesses locks the
  account for fifteen minutes, and an unknown username fails the same way as a
  wrong password.
- **Taking access away works immediately.** A password reset, a switch-off and
  a delete all end that person's sessions.
- **Keys** for machines are shown once and stored as a hash. An agent's key
  reaches its own agent and nothing else.
- **Credentials never reach the browser.** A connection string's password and
  a REST header's value are masked on the way out and restored when saved back
  unchanged.
- **Queries are read-only.** SQL runs as a prepared statement under a
  read-only role; a REST source only ever issues a GET. An operator's answer
  is bound or encoded, never interpolated.
- **Templates are data.** Bindings are dotted lookups plus a fixed list of
  filters. There is no expression parser, and a lookup refuses underscored
  names, callables, modules and classes, so a template cannot walk out of the
  row it was given.
- **Discovery stays local.** The printer scan takes private, loopback and
  link-local ranges only, at most 1024 addresses, one port.

## What it does not do yet

There is no audit export, no per-site tenancy, and no rate limit on anything
but sign-in. If you put Platen where the public can reach it, put something in
front of it.
