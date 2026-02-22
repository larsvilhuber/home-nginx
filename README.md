# Minimal config for Nginx on home server

## Implementation

- Copy the desired index.(server).html to index.html
- (Optional) Move this to `/home/www/`  and modify `/etc/nginx/sites-enabled/default` to read

```
	# include snippets/snakeoil.conf;

	root /home/www;

```
