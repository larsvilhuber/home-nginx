# Web / nginx / Caddy Migration Guide

**Source server:** `oiseau` (Ubuntu 24.x)
**Target server:** Ubuntu 26.04
**Last updated:** 2026-10-09

## Overview

`oiseau` serves a static landing page (`/home/www/`) with nginx on port 80. Caddy is
described in `README.md` as the HTTPS termination layer for the other services, but
**Caddy is not installed** on the machine this guide was last checked against (no
`caddy` binary, no `/etc/caddy`). The landing page links to `https://oiseau:*` ports
that Caddy used to proxy.

Verified on the machine where this guide was last updated (2026-10-09):

- nginx is running with the **stock Ubuntu default site** (`root /var/www/html`), not
  `/home/www`. The server block below is the *intended* configuration and must be
  applied by hand.
- Listening services: Stirling PDF container on `:8980` (the "PDF editing suite" was
  deactivated for RAM reasons per git history, so decide whether you want it),
  Pi-hole on `:3140`/`:3143`, vsftpd on `:21`, Samba on `:139`/`:445`.
- Nothing is listening on `:5173` or `:9000`, so the Image Editing and Docker
  management proxies have no backend here.
- `/home/ftpuser/files/` did not exist on the destination; it was created and the `ftp`
  user's home was moved to `/home/ftpuser` (see "Scanner drop folder"). vsftpd is
  configured for anonymous uploads as on the old system (restart pending).

## This folder contents

| File | Purpose |
|------|---------|
| `index.html` | Active landing page (gitignored; copy of `index.oiseau.html`) |
| `index.oiseau.html` | Landing page for `oiseau` server |
| `index.cerf.html` | Landing page for `cerf` server (Tailscale-accessible) |
| `index.cv-ai.html` | CV/AI variant |
| `caddy-root-ca.crt` | Caddy internal CA certificate for client installs (stale) |
| `icons/` | Icons for landing page |
| `gs/` | Ghostscript-related files (gitignored; belongs to the deactivated PDF suite, can be skipped) |

The folder is a **git repository**. `index.html` and `gs/` are gitignored.

## Migration steps: nginx

Run on the **new** server unless noted.

```bash
# 1. Install nginx
sudo apt install nginx

# 2. Create the document root
sudo mkdir -p /home/www
sudo chown vilhuber:www-data /home/www
```

```bash
# 3. Transfer content: run on the OLD server (or clone the git repo on the new one
#    and skip gs/)
rsync -av --exclude gs/ /home/www/ vilhuber@NEW_SERVER:/home/www/
```

```bash
# 4. Set the active landing page (new server)
cp /home/www/index.oiseau.html /home/www/index.html

# 5. Back up and replace the default site
sudo cp /etc/nginx/sites-available/default /etc/nginx/sites-available/default.bak
sudoedit /etc/nginx/sites-available/default   # sites-enabled/default is a symlink to this
```

Replace the contents with:
```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;

    root /home/www/;
    index index.html;
    server_name _;

    location / {
        try_files $uri $uri/ =404;
    }

    location /scanner/ {
        alias /home/ftpuser/files/;
        autoindex on;
    }
}
```

```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

`/home/www` must be traversable and readable by `www-data` (`vilhuber:www-data` with
group read, and `x` on `/home` itself).

### Scanner drop folder

`/scanner/` serves `/home/ftpuser/files/` with a directory listing. Create it:

```bash
sudo mkdir -p /home/ftpuser/files
sudo chown root:www-data /home/ftpuser /home/ftpuser/files
sudo chmod 755 /home/ftpuser
sudo chmod 2775 /home/ftpuser/files     # group-writable, new files inherit www-data
```

Every parent directory needs `x` for `www-data` (`namei -l /home/ftpuser/files` shows where
it breaks). Test: `curl -I localhost/scanner/` should return 200.

The scanner uploads through **anonymous FTP**, which vsftpd maps to the system **`ftp`**
user (uid 104). Anonymous sessions are rooted in that user's home. Ubuntu creates `ftp`
with home `/srv/ftp`, which is wrong here. The old system has **no password set for
`ftp` in `/etc/shadow`**; the new one has `!` (locked), the Ubuntu default. That is fine
for anonymous login, which does not use the password.

Point the account at `/home/ftpuser`. The anonymous root is not writable by `ftp`;
uploads go into `files/`:

```bash
sudo usermod -d /home/ftpuser ftp
sudo chown ftp:www-data /home/ftpuser/files
```

Settings in `/etc/vsftpd.conf` that differ from the Ubuntu 26.04 default (copied from the
old system; back up first with `sudo cp /etc/vsftpd.conf /etc/vsftpd.conf.bak`):

```
listen=YES
listen_ipv6=no
anonymous_enable=YES
write_enable=YES
local_umask=022
anon_upload_enable=YES
anon_umask=022
```

`anon_umask=022` is **not** in the old config. The default (077) would make anonymous
uploads unreadable by nginx, so `/scanner/` could not serve them.

Then restart and test:

```bash
sudo systemctl restart vsftpd
echo hi > /tmp/t.txt
curl -T /tmp/t.txt ftp://anonymous:x@localhost/files/t.txt
curl localhost/scanner/t.txt          # should print: hi
sudo rm /home/ftpuser/files/t.txt
```

**Security:** this allows unauthenticated uploads from anyone who can reach port 21.
Restrict it to the LAN (firewall or router); do not expose it to the internet.

Finally, point the scanner (`192.168.1.231`) at this host's IP.

## Migration steps: Caddy (HTTPS proxy)

Caddy 2.6.2 is **installed and running** on the destination (installed 2026-10-09 from
the Ubuntu `universe` package). The `caddy-root-ca.crt` in this folder was replaced with the
new root CA. It is a modified tracked file; commit it.

Status of the proxied backends here: `:443` (landing page) and `:8445` (Stirling PDF)
return 200; `:5445` and `:9443` return 502 until their services (on `:5173` and `:9000`)
are started.

**Port 80 conflict:** nginx owns `:80`. By default Caddy also binds `:80` to redirect
HTTP to HTTPS, which would collide with nginx and would also redirect the plain-HTTP
download of the CA cert. The Caddyfile below therefore sets `auto_https disable_redirects`
so nginx keeps `:80` and Caddy only serves `:443` and the extra ports.

To install Caddy on Ubuntu 26.04:
```bash
sudo apt install caddy
```

Do **not** use the Caddy Cloudsmith apt repository from older instructions: it returned
`402 Payment Required` on 2026-10-09 and breaks `apt update`. If you added it, remove
`/etc/apt/sources.list.d/caddy-stable.list`.

Create `/etc/caddy/Caddyfile` (all four blocks are active; a block whose backend is not
running just returns 502):
```caddyfile
{
    auto_https disable_redirects
}

# Landing page (HTTPS on 443; plain HTTP stays with nginx on 80)
oiseau {
    tls internal
    reverse_proxy localhost:80
}

# Stirling PDF (container publishes 8980)
oiseau:8445 {
    tls internal
    reverse_proxy localhost:8980
}

# Image Editing (backend on 5173)
oiseau:5445 {
    tls internal
    reverse_proxy localhost:5173
}

# Docker management (Portainer or similar, backend on 9000)
oiseau:9443 {
    tls internal
    reverse_proxy localhost:9000
}
```

If Caddy still fails to bind `:80`, move nginx to another port (e.g. `127.0.0.1:8081`),
update the landing-page `reverse_proxy` target, and serve the CA cert from Caddy too.

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy

# Export the new CA cert for client installation
sudo cp /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt \
    /home/www/caddy-root-ca.crt
sudo chmod 644 /home/www/caddy-root-ca.crt
```

Clients must install the new CA cert (the old one is invalid after a reinstall). It is
served over plain HTTP at `http://oiseau/caddy-root-ca.crt`, which works because nginx
keeps `:80` and nothing redirects it.

`tls internal` issues certificates for the names in the Caddyfile. If the new server is
not called `oiseau`, change the site names here and make local DNS resolve them. This
machine's hostname is `hibou`, and `oiseau` does **not** resolve here; point local DNS
(or Pi-hole) at it, or the `https://oiseau:*` links will not load.

## Updating landing page links

`index.oiseau.html` (and the active `index.html`) contains hardcoded addresses. After
migration, update:

- `https://192.168.1.231` (scanner IP): new IP, or keep if the scanner is unchanged
- `https://oiseau:*` (5445, 8445, 9443): need local DNS for `oiseau` to point at the new
  server, plus the matching Caddy blocks. Remove links whose service is gone (e.g. 8445
  if Stirling stays off).
- `http://oiseau:20211/`, `http://oiseau:8123/lovelace/0`: direct ports, no proxy;
  depend on the services running on the new host
- `https://pihole:3143/admin/`: relies on Pi-hole's hostname in local DNS
- `https://192.168.1.1/`: router/firewall IP, update if changed
