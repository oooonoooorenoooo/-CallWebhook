ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache curl bash python3
COPY run.sh /run.sh
COPY provision.py /provision.py
RUN chmod 755 /run.sh
CMD ["/run.sh"]
