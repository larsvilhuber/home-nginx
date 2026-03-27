# Minimal config for Nginx on home server

## Implementation

- Copy the desired index.(server).html to index.html
- (Optional) Move this to `/home/www/`  and modify `/etc/nginx/sites-enabled/default` to read

```
	# include snippets/snakeoil.conf;

	root /home/www;

```

## Caddy HTTPS proxy

Caddy runs alongside nginx to provide HTTPS. nginx serves on port 80; Caddy terminates
TLS on port 443 (and custom ports) and proxies to the appropriate backend.

Configuration: `/etc/caddy/Caddyfile`

Current port mapping:

| Service        | HTTPS URL              | HTTP backend       |
|----------------|------------------------|--------------------|
| Landing page   | `https://oiseau`       | `localhost:80`     |
| PDF Editing    | `https://oiseau:8445`  | `localhost:8081`   |
| Image Editing  | `https://oiseau:5445`  | `localhost:5173`   |

Caddy uses its **internal CA** (`tls internal`) — browsers will show an untrusted-cert
warning until the CA cert is installed on each client. The cert is served directly
from the landing page at `http://oiseau/caddy-root-ca.crt`.

If Caddy is ever reinstalled or its data directory wiped, a new CA is generated and
the file must be re-copied and re-installed on all clients:

```bash
sudo cp /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt /home/www/caddy-root-ca.crt
sudo chmod 644 /home/www/caddy-root-ca.crt
```

### Adding a new HTTPS-proxied service

1. Pick a free port: `ss -tlnp`
2. Add a site block to `/etc/caddy/Caddyfile`:

```
# My new service (proxies http://oiseau:BACKEND_PORT)
oiseau:NEW_PORT {
	tls internal
	reverse_proxy localhost:BACKEND_PORT
}
```

3. Validate and reload:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

4. Update `index.html` to link to `https://oiseau:NEW_PORT/`.
5. Update the port mapping table in this README.
