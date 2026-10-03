# Hetzner deployment

The production workflow tests pull requests and `main` on GitHub-hosted runners.
A passing push to `main` sends a time-limited HMAC-signed request to the HTTPS
deployment hook. The hook accepts only this repository's `main` ref, verifies
the commit against public `main`, keeps releases side by side, checks `/healthz`,
and restores the previous release if restart or health validation fails. The
`production` GitHub environment is restricted to `main`; forks cannot access its
deployment secret.

The server setup uses separate non-root runtime and deployment users,
loopback-only services, systemd sandboxing, UFW, Fail2ban, and Caddy-managed
HTTPS. Store the random hook key in `/etc/globeview/deploy.env` with mode `0640`
and add the same value as the `DEPLOY_WEBHOOK_SECRET` secret in GitHub's
`production` environment.

Install the Ubuntu camera decoder with
`sudo apt-get install -y --no-install-recommends ffmpeg` before starting the
service. The service selects `/usr/bin/ffmpeg` through `IMAGEIO_FFMPEG_EXE`.
The bundled Linux FFmpeg 7.0.2 crashes when reading MPEG-TS camera segments;
CI uses the system decoder and tests conversion of an actual MPEG-TS frame.
Camera decoding retains its time, size, concurrency, and protocol limits.

After DNS for `globeview.app` and `www.globeview.app` points at the server,
Caddy obtains and renews certificates automatically. Keep the DNS records
DNS-only during initial issuance; Cloudflare proxying can be enabled afterward
with SSL/TLS set to Full (strict).
