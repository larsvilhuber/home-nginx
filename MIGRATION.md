# Web / nginx / Caddy Migration Guide

**Source server:** `cerf`
**Target server:** `hibou` (Ubuntu 26.04)
**Last updated:** 2026-10-09

## Overview

`hibou` serves a static landing page (`/home/www/`, `index.hibou.html`) with nginx and
terminates HTTPS with Caddy for the other services. `oiseau` is a separate server with
its own page (`index.oiseau.html`); nothing on `hibou` serves or proxies it.

Verified on the machine where this guide was last updated (2026-10-09):

- nginx is running with the **stock Ubuntu default site** (`root /var/www/html`), not
  `/home/www`. The server block below is the *intended* configuration and must be
  applied by hand.
- Listening services: Stirling PDF container on `:8980` (the "PDF editing suite" was
  deactivated for RAM reasons per git history, so decide whether you want it),
  Pi-hole on `:3140`/`:3143`, vsftpd on `:21`, Samba on `:139`/`:445`.
- Nothing is listening on `:9000`, so the Docker management proxy has no backend here.
  Image Editing (`:5445`/`:5173`) has been retired and removed everywhere.
- `/home/ftpuser/files/` did not exist on the destination; it was created and the `ftp`
  user's home was moved to `/home/ftpuser` (see "Scanner drop folder"). vsftpd is
  configured for anonymous uploads as on the old system (restart pending).

## This folder contents

| File | Purpose |
|------|---------|
| `index.html` | Active landing page (gitignored; copy of `index.hibou.html`) |
| `index.hibou.html` | Landing page for `hibou` (this server) |
| `index.oiseau.html` | Landing page for the separate `oiseau` server |
| `index.cerf.html` | Landing page for the `cerf` server (Tailscale-accessible) |
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
cp /home/www/index.hibou.html /home/www/index.html

# 5. Back up and replace the default site
sudo cp /etc/nginx/sites-available/default /etc/nginx/sites-available/default.bak
sudoedit /etc/nginx/sites-available/default   # sites-enabled/default is a symlink to this
```

Replace the contents with. Port 80 only redirects to HTTPS (except the CA cert, which
clients must be able to fetch before they trust Caddy). The real content is served on an
internal loopback port that Caddy proxies to; proxying to :80 would loop forever.

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location = /caddy-root-ca.crt {
        root /home/www/;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 127.0.0.1:8081;
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
new root CA (already committed).

Status of the proxied backends here: `:443` (landing page) and `:8445` (Stirling PDF)
return 200; `:9443` returns 502 until its service (on `:9000`) is started.

**Port 80:** nginx owns `:80` and does the HTTP-to-HTTPS redirect (see the nginx section).
Caddy's own redirects are disabled with `auto_https disable_redirects`, and it proxies the
landing page to nginx's internal `127.0.0.1:8081`, not `:80`.

To install Caddy on Ubuntu 26.04:
```bash
sudo apt install caddy
```

Do **not** use the Caddy Cloudsmith apt repository from older instructions: it returned
`402 Payment Required` on 2026-10-09 and breaks `apt update`. If you added it, remove
`/etc/apt/sources.list.d/caddy-stable.list`.

Create `/etc/caddy/Caddyfile`. A block whose backend is not running just
returns 502:

```caddyfile
{
    auto_https disable_redirects

    # Unique CA names (see "Unique CA names" below)
    pki {
        ca local {
            name "Hibou Local CA"
            root_cn "Hibou Local CA - Root"
            intermediate_cn "Hibou Local CA - Intermediate"
        }
    }
}

# Landing page (HTTPS on 443)
hibou {
    tls internal
    reverse_proxy localhost:8081
}

# Stirling PDF
hibou:8445 {
    tls internal
    reverse_proxy localhost:8980
}

# Docker management
hibou:9443 {
    tls internal
    reverse_proxy localhost:9000
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy

# Export the new CA cert for client installation
sudo cp /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt \
    /home/www/caddy-root-ca.crt
sudo chmod 644 /home/www/caddy-root-ca.crt
```

Clients must install the new CA cert (the old one is invalid after a reinstall). It is
served over plain HTTP at `http://hibou/caddy-root-ca.crt`; nginx exempts that one path
from the HTTPS redirect.

Stirling PDF also publishes plain HTTP on `:8980`. That port is Docker's and is **not**
redirected; use `https://hibou:8445/`. To close it, bind the container to loopback
(`127.0.0.1:8980:8080` in its compose file).

**Unique CA names.** Browsers key certificates by issuer name. Every Caddy install names
its CA "Caddy Local Authority - ECC Intermediate" by default, so a second Caddy server
(or an older install of this one) makes Firefox fail with
`SEC_ERROR_BAD_SIGNATURE`: it builds the chain with the wrong intermediate that has the
same name. The `pki` block above gives this server its own names. Any other Caddy server
you connect to should do the same with different names. Changing the names after the
fact needs a fresh CA:

```bash
sudo systemctl stop caddy
sudo mv /var/lib/caddy/.local/share/caddy/pki /var/lib/caddy/.local/share/caddy/pki.old
sudo mv /var/lib/caddy/.local/share/caddy/certificates /var/lib/caddy/.local/share/caddy/certificates.old
sudo systemctl start caddy
```

then re-export the root cert (below) and re-import it on clients. The leaf certificates
must be regenerated too, otherwise Caddy keeps serving the old ones.

`tls internal` issues certificates for the names in the Caddyfile, so local DNS (or
Pi-hole) must resolve `hibou` to this server for other clients.

## Updating landing page links

`index.hibou.html` (and the active `index.html`) contains hardcoded addresses:

- `https://hibou:8445/` (Stirling PDF), `https://hibou:3143/admin/` (Pi-hole),
  `https://hibou:11443/` (Unifi): served by this host
- `http://cerf:8123/lovelace/0` (Home Assistant) and `http://cerf:9000/` (Squeeze
  server): still point at `cerf`; change them when those services move
- `https://192.168.1.1/`, `https://192.168.1.253/`, `http://192.168.1.248/`: network
  devices; update if their addresses change
- `/scanner/`: the drop folder on this host (see "Scanner drop folder"); the scanner at
  `192.168.1.231` must be pointed at this host's IP
