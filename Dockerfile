# One image holds all three services; the chart picks which module each Deployment runs.
# Health is checked by Kubernetes probes, so there is no Docker HEALTHCHECK.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services ./services
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "services.gateway"]
