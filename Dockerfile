FROM python:3.12-slim AS build
WORKDIR /build
COPY build_site.py build_map.py prep_seed.py map.html ./
COPY assets/ ./assets/
COPY data_app/ ./data_app/
# Les liens symboliques n'ont pas leur place dans les fichiers servis.
RUN python -c "from pathlib import Path; assert not any(p.is_symlink() for p in Path('data_app').rglob('*'))" \
    && python build_site.py --base /atlas/data --suggestion /atlas/suggestion --sortie /site

FROM caddy:2.7-alpine
# Port 8080 : aucune capacité réseau privilégiée nécessaire.
RUN setcap -r /usr/bin/caddy
LABEL org.opencontainers.image.source="https://github.com/lfi-pee/devoirs_maison"
COPY docker/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /build/data_app/ /srv/data/
COPY --from=build /site/index.html /srv/index.html
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
