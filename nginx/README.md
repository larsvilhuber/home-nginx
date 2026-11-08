# MusicBrainz caching proxy

Why: the browser apps (`cdcheck.html`, `review.html`) were hitting musicbrainz.org directly, with
no User-Agent and no rate control, so MusicBrainz often answered 503. Now they call `/mb/...` on
this server, which caches answers (30 days), sends a proper User-Agent and spaces cache misses
1 second apart.

## Install

```bash
sudo mkdir -p /var/cache/nginx-mb && sudo chown www-data: /var/cache/nginx-mb
sudo cp nginx/musicbrainz-proxy.conf    /etc/nginx/conf.d/
sudo cp nginx/musicbrainz-location.conf /etc/nginx/snippets/
# in /etc/nginx/sites-available/default, inside the `listen 127.0.0.1:8081` server block add:
#     include /etc/nginx/snippets/musicbrainz-location.conf;
sudo nginx -t && sudo systemctl reload nginx
```

## Check

```bash
curl -sI 'http://127.0.0.1:8081/mb/release/?fmt=json&limit=1&query=barcode:5099902894225' | grep -i x-cache   # MISS, then HIT
```

Clear the cache: `sudo rm -rf /var/cache/nginx-mb/*` (then reload nginx).
