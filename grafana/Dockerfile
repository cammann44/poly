FROM grafana/grafana:10.2.2

# Install Infinity datasource plugin for JSON endpoints
RUN grafana-cli plugins install yesoreyeram-infinity-datasource

# Copy provisioning configs
COPY provisioning/datasources/datasources.yml /etc/grafana/provisioning/datasources/
COPY provisioning/dashboards/dashboards.yml /etc/grafana/provisioning/dashboards/
COPY provisioning/dashboards/copy-trading.json /var/lib/grafana/dashboards/

# Environment - Authentication
ENV GF_SECURITY_ADMIN_USER=admin
ENV GF_SECURITY_ADMIN_PASSWORD=cammann
ENV GF_AUTH_ANONYMOUS_ENABLED=false
ENV GF_AUTH_DISABLE_LOGIN_FORM=false
ENV GF_SERVER_ROOT_URL=%(protocol)s://%(domain)s/

EXPOSE 3000
