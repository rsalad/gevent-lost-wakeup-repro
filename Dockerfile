# Three builds of the same environment, differing only in how gevent got there:
#   stock   gevent 26.9.0 from the PyPI wheel
#   source  gevent 26.9.0 compiled from the PyPI sdist, unchanged (control for the build)
#   fixed   the same sdist with fix.patch applied
FROM python:3.14.8-slim AS base
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
WORKDIR /repro
COPY requirements.txt .
RUN pip install -q -r requirements.txt

FROM base AS stock
ENV BUILD=stock
COPY repro.py runs.py trace.py ./
CMD ["python", "runs.py"]

FROM base AS sdist
RUN apt-get update -q && apt-get install -y -q --no-install-recommends build-essential patch \
    && rm -rf /var/lib/apt/lists/*
RUN pip download -q gevent==26.9.0 --no-binary gevent --no-deps -d /tmp/sdist \
    && tar xzf /tmp/sdist/gevent-26.9.0.tar.gz -C /tmp

FROM sdist AS source
ENV BUILD=source
RUN pip install -q --force-reinstall --no-deps /tmp/gevent-26.9.0
COPY repro.py runs.py trace.py ./
CMD ["python", "runs.py"]

FROM sdist AS fixed
ENV BUILD=fixed
COPY fix.patch .
RUN patch -d /tmp/gevent-26.9.0 -p1 < fix.patch \
    && pip install -q --force-reinstall --no-deps /tmp/gevent-26.9.0
COPY repro.py runs.py trace.py ./
CMD ["python", "runs.py"]
