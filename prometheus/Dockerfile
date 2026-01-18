FROM prom/prometheus:v2.48.0

COPY prometheus.yml /etc/prometheus/prometheus.yml

EXPOSE 9090

CMD ["--config.file=/etc/prometheus/prometheus.yml", "--web.enable-lifecycle"]
