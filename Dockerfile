# Auckland live train map — production image.
#
# The rail geometry is baked in at BUILD time (build_static.py downloads AT's
# 31 MB GTFS zip and reduces it to a ~178 KB simplified web file plus the full
# ~12 MB server file). Doing it here rather than at boot means:
#   - startup is fast and needs no network beyond the realtime feed
#   - a bad GTFS fetch breaks the build, not the running site
# The trade-off is that geometry is only as fresh as the last deploy; AT
# changes rail alignments rarely, and a rebuild is one redeploy.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits don't invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/

# Build the geometry into the image.
RUN python src/build_static.py && \
    test -s output/rail_geo.json && \
    test -s web/rail_geo.json && \
    rm -f output/gtfs.zip

# The feedback DB lives on the mounted volume, NOT in output/ — mounting a
# volume over output/ would shadow the rail_geo.json built above.
ENV FEEDBACK_DB=/data/feedback.db \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

CMD ["python", "src/server.py"]
