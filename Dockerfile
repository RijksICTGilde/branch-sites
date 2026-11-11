FROM alpine:3.22

RUN apk add --no-cache git nginx python3 tini

COPY nginx.conf /app/nginx.conf
COPY www/ /app/www/
COPY branch_sites.py /app/branch_sites.py

ENV HOME=/tmp
USER 1001
EXPOSE 8080
# tini reaps the helpers of a git process that was killed after a timeout; Python as PID 1 would not.
ENTRYPOINT ["/sbin/tini", "--", "python3", "/app/branch_sites.py"]
