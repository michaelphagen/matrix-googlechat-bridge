FROM docker.io/alpine:3.13

ARG TARGETARCH=amd64

RUN apk add --no-cache \
      python3 py3-pip py3-setuptools py3-wheel \
      py3-pillow \
      py3-aiohttp \
      py3-magic \
      py3-sqlalchemy \
      py3-psycopg2 \
      py3-ruamel.yaml \
      py3-commonmark \
      py3-alembic \
      #hangups
        py3-async-timeout \
        py3-requests \
        py3-appdirs \
        py3-configargparse \
        py3-protobuf \
        py3-urwid \
        py3-lxml \
        #mechanicalsoup
          py3-beautifulsoup4 \
      py3-idna \
      # encryption
      olm-dev \
      py3-cffi \
      py3-pycryptodome \
      py3-unpaddedbase64 \
      py3-future \
      # proxy support
      py3-pysocks \
      # Other dependencies
      ca-certificates \
      su-exec \
      bash \
      curl \
      jq \
      yq

COPY requirements.txt /opt/mautrix-hangouts/requirements.txt
COPY optional-requirements.txt /opt/mautrix-hangouts/optional-requirements.txt
WORKDIR /opt/mautrix-hangouts
RUN apk add --virtual .build-deps python3-dev libffi-dev build-base \
 && sed -Ei 's/psycopg2-binary.+//' optional-requirements.txt \
 && pip3 install -r requirements.txt -r optional-requirements.txt \
 && apk del .build-deps

COPY . /opt/mautrix-hangouts
RUN apk add --no-cache git && pip3 install .[e2be] && apk del git \
  # This doesn't make the image smaller, but it's needed so that the `version` command works properly
  && cp mautrix_hangouts/example-config.yaml . && rm -rf mautrix_hangouts

ENV UID=1337 GID=1337
VOLUME /data

CMD ["/opt/mautrix-hangouts/docker-run.sh"]
