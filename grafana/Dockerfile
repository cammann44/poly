FROM grafana/grafana:10.2.2

# Copy provisioning configs
COPY provisioning/datasources/datasources.yml /etc/grafana/provisioning/datasources/
COPY provisioning/dashboards/dashboards.yml /etc/grafana/provisioning/dashboards/
COPY provisioning/dashboards/copy-trading.json /var/lib/grafana/dashboards/

# Environment
ENV GF_SECURITY_ADMIN_PASSWORD=admin
ENV GF_AUTH_ANONYMOUS_ENABLED=true
ENV GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
ENV GF_SERVER_ROOT_URL=%(protocol)s://%(domain)s/

EXPOSE 3000
