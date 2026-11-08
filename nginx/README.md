# MusicBrainz caching proxy

Why: the browser apps (`cdcheck.html`, `review.html`) were hitting musicbrainz.org directly, with
no User-Agent and no rate control, so MusicBrainz often answered 503. Now they call `/mb/...` on
this server, which caches answers (30 days), sends a proper User-Agent and spaces cache misses
1 second apart. The pages also retry 502/503/504 with backoff.

## Where things live

| What | Where |
|------|-------|
| Config (source of truth) | `/home/www/nginx/musicbrainz-proxy.conf`, `musicbrainz-location.conf` (this repo) |
| Symlinks nginx reads | `/etc/nginx/conf.d/musicbrainz-proxy.conf`, `/etc/nginx/snippets/musicbrainz-location.conf` |
| Hook into the site | one `include /etc/nginx/snippets/musicbrainz-location.conf;` line in the `127.0.0.1:8081` server block of `/etc/nginx/sites-available/default` |
| Cache data | `/home/nginx-mb/cache` (owned by `www-data`, capped at 2 GB, entries expire after 90 days unused) |
| Outbound queue | internal server on `127.0.0.1:8082` (1 request/second to musicbrainz.org) |

Editing the repo files changes the live config after `sudo nginx -t && sudo systemctl reload nginx`.

## Install from scratch

```bash
sudo mkdir -p /home/nginx-mb/cache && sudo chown -R www-data: /home/nginx-mb
sudo ln -sf /home/www/nginx/musicbrainz-proxy.conf    /etc/nginx/conf.d/musicbrainz-proxy.conf
sudo ln -sf /home/www/nginx/musicbrainz-location.conf /etc/nginx/snippets/musicbrainz-location.conf
# add to the 127.0.0.1:8081 server block in /etc/nginx/sites-available/default:
#     include /etc/nginx/snippets/musicbrainz-location.conf;
sudo nginx -t && sudo systemctl reload nginx
```

## Check

```bash
curl -sI 'http://127.0.0.1:8081/mb/release/?fmt=json&limit=1&query=barcode:5099902894225' | grep -i x-cache   # MISS, then HIT
du -sh /home/nginx-mb/cache
```

Clear the cache: `sudo rm -rf /home/nginx-mb/cache/*` (then reload nginx).
