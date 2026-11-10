FROM nousresearch/hermes-agent@sha256:3eadce07c6cc35599ebacfa1f7d8ee06edce6374b9d07e379d6ec825ddaa7534
USER root
COPY src/hermes_sell/runtime_container.py /opt/hermes-sell/runtime_container.py
COPY deployment/hermes/entrypoint.py /opt/hermes-sell/entrypoint.py
RUN /opt/hermes/.venv/bin/python -I -c "import sys; sys.path.insert(0, '/opt/hermes-sell'); from runtime_container import POLICY_BYTES; from pathlib import Path; Path('/opt/hermes-sell/config.yaml').write_bytes(POLICY_BYTES)" \
    && mkdir -p /state /run/secrets /opt/hermes-sell/empty-plugins \
    && chown 10000:10000 /state \
    && chmod -R a-w /opt/hermes-sell
LABEL org.hermes-sell.policy="text-json-no-tools-v1"
ENV HOME=/state/home HERMES_HOME=/state/hermes PYTHONDONTWRITEBYTECODE=1 \
    HERMES_WRITE_SAFE_ROOT=/state \
    HERMES_ENABLE_PROJECT_PLUGINS=0 HERMES_BUNDLED_PLUGINS=/opt/hermes/plugins/model-providers \
    API_SERVER_ENABLED=true API_SERVER_HOST=0.0.0.0 API_SERVER_PORT=8642
WORKDIR /state
USER 10000:10000
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "-I", "-B", "/opt/hermes-sell/entrypoint.py"]
CMD []
