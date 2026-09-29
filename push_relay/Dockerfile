FROM python:3.12-slim
WORKDIR /app
COPY push_relay/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && useradd --uid 10001 --create-home relay && mkdir /data && chown relay:relay /data
COPY push_relay /app/push_relay
USER relay
EXPOSE 8080
CMD ["python", "-m", "push_relay.server"]
