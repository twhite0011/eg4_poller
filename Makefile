# eg4poll — build and deploy.
#
# The point of this file is that the steps stop being remembered. Every target
# is idempotent and safe to re-run.
#
#   make check     validate everything locally, touch nothing
#   make build     buildx --push, tagged from git
#   make deploy    push config + dashboard to the Pi, pull the image, restart
#   make status    what is ACTUALLY running out there
#
# One-time setup: see DEPLOY.md

PI          ?= eg4poller.whitehouse
PI_DIR      ?= /home/nonya/dockers/eg4poll
NGINX_HTML  ?= /home/nonya/dockers/nginx/html
NGINX_CONF  ?= /home/nonya/dockers/nginx/conf.d
IMAGE       ?= twhitedocker/eg4poll
PLATFORM    ?= linux/arm64

VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
SHA     := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

.PHONY: check build deploy deploy-config deploy-dash status logs shell clean

## Validate before anything leaves the laptop.
check:
	@echo "== python =="
	@python3 -m py_compile app/*.py && echo "  compile OK"
	@echo "== yaml =="
	@python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in \
	  ['config/config.yaml','config/eg4_6000xp_registers.yaml','docker-compose.yml']]; \
	  print('  parse OK')"
	@echo "== register map =="
	@python3 tools/validate.py
	@echo "== node-red function nodes =="
	@node -e "const fs=require('fs');for(const f of fs.readdirSync('nodered').filter(f=>f.endsWith('.js'))){new Function(fs.readFileSync('nodered/'+f,'utf8'))};console.log('  parse OK')"
	@echo "== dashboard =="
	@node -e "const fs=require('fs');for(const f of ['dashboard/solar_dash.html','dashboard/solar_settings.html']){const h=fs.readFileSync(f,'utf8');new Function([...h.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1])};console.log('  parse OK')"
	@echo "== git =="
	@git diff --quiet || echo "  WARNING: uncommitted changes; VERSION will be marked -dirty"
	@echo "  version: $(VERSION)  sha: $(SHA)"

## Build and push, stamped with the git version.
build: check
	docker buildx build --platform $(PLATFORM) \
	  --build-arg VERSION=$(VERSION) --build-arg SHA=$(SHA) \
	  -t $(IMAGE):$(VERSION) -t $(IMAGE):latest --push .
	@echo "pushed $(IMAGE):$(VERSION)"

## Config and register map. Bind-mounted, so a restart is enough.
deploy-config:
	rsync -av --delete config/ $(PI):$(PI_DIR)/config/
	rsync -av docker-compose.yml $(PI):$(PI_DIR)/

## Static pages and the nginx site config.
deploy-dash:
	rsync -av dashboard/solar_dash.html dashboard/solar_settings.html $(PI):$(NGINX_HTML)/
	rsync -av dashboard/default.conf $(PI):$(NGINX_CONF)/
	ssh $(PI) 'cd $(dir $(NGINX_CONF)) && docker compose exec -T nginx nginx -t && docker compose restart nginx'

## Everything, in the order that works.
deploy: deploy-config deploy-dash
	ssh $(PI) 'cd $(PI_DIR) && docker compose pull && docker compose up -d'
	@sleep 4
	@$(MAKE) --no-print-directory status

## What is actually running -- version, and the hash of every mounted file.
status:
	@echo "== expected =="
	@echo "  version   $(VERSION)"
	@echo "  config    $$(sha256sum config/config.yaml | cut -c1-12)"
	@echo "  registers $$(sha256sum config/eg4_6000xp_registers.yaml | cut -c1-12)"
	@echo "== running on $(PI) =="
	@ssh $(PI) 'cd $(PI_DIR) && docker compose logs --tail 400 eg4poll' \
	  | grep -E "eg4poll .* starting|config .* sha256|register map .* sha256|writes are" \
	  | tail -5 || echo "  no startup banner found -- container may not have restarted"

logs:
	ssh $(PI) 'cd $(PI_DIR) && docker compose logs -f --tail 50 eg4poll'

shell:
	ssh -t $(PI) 'cd $(PI_DIR) && docker compose exec eg4poll sh'

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
