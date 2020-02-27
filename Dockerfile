FROM docker.io/alpine:3.11

RUN apk add --no-cache \
      py3-pillow \
      py3-aiohttp \
      py3-magic \
      py3-sqlalchemy \
      py3-psycopg2 \
      py3-ruamel.yaml \
      # Indirect dependencies
      #commonmark
        py3-future \
      #alembic
        py3-mako \
        py3-dateutil \
        py3-markupsafe \
        py3-six \
      #hangups
        py3-async-timeout \
        py3-requests \
        #py3-protobuf \
        py3-urwid \
        #mechanicalsoup
          py3-beautifulsoup4 \
      py3-idna \
      # Other dependencies
      ca-certificates \
      su-exec

COPY requirements.txt /opt/mautrix-hangouts/requirements.txt
WORKDIR /opt/mautrix-hangouts
RUN pip3 install -r requirements.txt

COPY . /opt/mautrix-hangouts
RUN apk add --no-cache git && pip3 install . && apk del git

ENV UID=1337 GID=1337
VOLUME /data

CMD ["/opt/mautrix-hangouts/docker-run.sh"]
