FROM python:3.12-slim
WORKDIR /app
# Supervisor builds each add-on from its own directory. Fetch only the reviewed
# relay source files from this immutable revision, not a changing main branch.
ARG RELAY_REVISION=e8ac82e73a749e9b231871c1236d215c71c0dcc4
RUN mkdir push_relay
RUN python -c 'import os, pathlib, urllib.request; base="https://raw.githubusercontent.com/oooonoooorenoooo/-CallWebhook/"+os.environ["RELAY_REVISION"]+"/push_relay/"; files=("__init__.py","server.py","comatalarm.py","flight_schedule.py","stand_model.py","ber_package.py","ber_document.py","testflight.py","attestation.py","apple_access.py","apple-app-attest-root.pem","requirements.txt"); [(pathlib.Path("push_relay")/name).write_bytes(urllib.request.urlopen(base+name,timeout=60).read()) for name in files]'
RUN pip install --no-cache-dir -r push_relay/requirements.txt
RUN python -c 'import pyrage; import pypdf'
COPY run.py /app/run.py
# No host ports, host networking, HA credentials, Supervisor access or HA config
# mounts. HA's narrow proxy reaches port 8080 on the internal add-on network.
CMD ["python", "/app/run.py"]


